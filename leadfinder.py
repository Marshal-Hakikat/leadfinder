#!/usr/bin/env python3
"""
CleanSlate Lead Finder - phase 1 (duplicate lead management)

Finds public conversations where Salesforce people struggle with duplicate
leads/records, ask for dedupe tools, or complain about competitors, scores them
for buying intent, and writes a ranked report you can work through each day.

Usage:
    python leadfinder.py demo              # try it offline with sample posts
    python leadfinder.py run               # fetch live, score, write report
    python leadfinder.py report --min-score 5

Optional: set ANTHROPIC_API_KEY to have an LLM re-score the promising posts.
"""
import argparse
import csv
import html
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests

# --------------------------------------------------------------------------
# CONFIG - edit these
# --------------------------------------------------------------------------
USER_AGENT = os.environ.get(
    "LEADFINDER_USER_AGENT",
    "cleanslate-leadfinder/0.1 (contact: marshal.hakikat@gmail.com)",  # put your email here
)
NOTIFY_MIN_SCORE = 7  # webhook alert only for new leads at or above this score

SUBREDDITS = ["salesforce", "salesforceadmin"]

# Startup / small-business communities (searched with "salesforce" added)
SUBREDDITS_GENERAL = ["startups", "smallbusiness", "CRM", "sales", "SaaS"]
GENERAL_QUERIES = [
    "salesforce duplicate leads",
    "salesforce duplicates",
    "salesforce dedupe",
    "crm duplicate leads",
]

QUERIES = [
    "duplicate leads",
    "duplicate records",
    "dedupe",
    "merge duplicates",
    "duplicate rules",
    "duplicate management",
    "Cloudingo",
    "DemandTools",
    "Duplicate Check",
    "RingLead",
]

HN_QUERIES = ["salesforce duplicate leads", "salesforce dedupe", "salesforce duplicate records"]

PRODUCT_DESCRIPTION = (
    "CleanSlate is a Salesforce app for duplicate lead management: it finds and "
    "merges duplicate leads/contacts/accounts and prevents new duplicates. "
    "Target customers are small companies and startups (small teams, often one admin, "
    "price-sensitive); large enterprises are NOT the target."
)

# --------------------------------------------------------------------------
# SCORING (rule-based, works with no API key)
# --------------------------------------------------------------------------
PAIN = [
    r"duplicate[sd]? (lead|leads|record|records|contact|contacts|account|accounts)",
    r"\bdedup",
    r"de-dup",
    r"merge (lead|leads|duplicate|duplicates|record|records|contact|contacts|account|accounts)",
    r"duplicate (rule|rules|management|matching|check)",
    r"matching rule",
    r"same (lead|person|contact) (twice|multiple)",
    r"duplicat\w* (on|during|after) (conversion|convert|import|upload|web.?to.?lead)",
    r"duplicate",
    r"dirty data|data quality|data clean",
]
INTENT = [
    r"looking for",
    r"recommend",
    r"any (tool|app|package|solution|suggestions)",
    r"appexchange",
    r"alternative",
    r"best (way|tool|app|practice)",
    r"how (do|does|are) (you|everyone|your team|people)",
    r"anyone (use|using|tried|have)",
    r"what do you use",
    r"third.party",
    r"worth (it|the)",
    r"which (tool|app)",
    r"\?",
]
COMPETITORS = [
    "cloudingo", "demandtools", "duplicate check", "plauti", "ringlead",
    "dupeblocker", "dedupely", "insycle", "validity",
]
NEGATIVE = [r"\bexam\b", r"certification", r"interview question", r"superbadge", r"trailhead"]
ROLE = [r"(i'm|i am|we are|our|as) (a |an |the )?(sf |salesforce )?(admin|revops|rev ops|sales ops|operations)"]

# Target customer: small companies and startups
STARTUP = [
    r"\bstartup", r"small (team|company|business|org)", r"\bsmb\b",
    r"(solo|only|one|sole|accidental) (sf |salesforce )?admin", r"one.person",
    r"(we're|we are|i'm|i am) (a )?(small|tiny|early)", r"early.stage", r"seed|series a",
    r"\b(\d|1\d|2\d|3\d|4\d)\s*(reps|users|people|employees|licenses)\b",
    r"professional edition|essentials|starter suite|free edition|developer edition",
]
BUDGET = [
    r"\bcheap", r"affordable", r"budget", r"\bfree\b", r"too (expensive|much)",
    r"pricing", r"can't afford|cannot afford", r"low.cost", r"open.source",
]
ENTERPRISE = [
    r"enterprise", r"fortune \d+", r"global (company|org)", r"\b\d{4,}\s*(users|employees)\b",
    r"multi.?org", r"\bmdm\b", r"data governance", r"large (company|org)",
]


def _hits(patterns, text):
    return [p for p in patterns if re.search(p, text, re.I)]


def rule_score(title, body, created_at):
    """Return (score 0-10, intent_type, reason string)."""
    title = title or ""
    text = f"{title}\n{body or ''}"
    reasons = []
    score = 0

    pain_title = _hits(PAIN, title)
    pain_all = _hits(PAIN, text)
    comp = [c for c in COMPETITORS if c in text.lower()]
    intent = _hits(INTENT, text)
    neg = _hits(NEGATIVE, text)

    if not pain_all and not comp:
        return 0, "discussion", "no duplicate/competitor signal"

    if pain_title:
        score += 3
        reasons.append("duplicate pain in title")
    elif pain_all:
        score += 2
        reasons.append("duplicate pain in body")
    if len(pain_all) > 2:
        score += 1
        reasons.append("multiple pain phrases")
    if comp:
        score += 3
        reasons.append("mentions competitor: " + ", ".join(comp[:3]))
    if intent:
        score += 2
        reasons.append("asking/seeking")
        if comp:
            score += 1
    if _hits(ROLE, text):
        score += 1
        reasons.append("admin/ops role")
    if neg:
        score -= 3
        reasons.append("exam/learning noise")
    if _hits(STARTUP, text):
        score += 2
        reasons.append("small company/startup signal")
    if _hits(BUDGET, text):
        score += 1
        reasons.append("price-sensitive")
    if _hits(ENTERPRISE, text):
        score -= 2
        reasons.append("enterprise signal (not target)")

    if created_at:
        age_days = (time.time() - created_at) / 86400
        if age_days > 180:
            score -= 2
            reasons.append("old post")
        elif age_days < 14:
            score += 1
            reasons.append("recent")

    score = max(0, min(10, score))

    if comp:
        kind = "competitor"
    elif intent and pain_all:
        kind = "tool_request"
    elif pain_all:
        kind = "pain"
    else:
        kind = "discussion"
    return score, kind, "; ".join(reasons)


# --------------------------------------------------------------------------
# OPTIONAL LLM SCORING
# --------------------------------------------------------------------------
def llm_score(title, body):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    model = os.environ.get("LEADFINDER_MODEL", "claude-haiku-5-5")
    prompt = f"""You are qualifying sales leads. Product: {PRODUCT_DESCRIPTION}

Public post:
TITLE: {title}
BODY: {(body or '')[:2500]}

Score 1-10 how likely the author (or their company) would buy a duplicate-lead
management app. 9-10 = actively asking for a tool or unhappy with a competitor;
6-8 = clear duplicate pain, no tool request yet; 3-5 = vague/related;
1-2 = unrelated, learning/exam question, or already solved.
Reply with ONLY JSON: {{"score": int, "intent": "tool_request|competitor|pain|discussion", "reason": "one sentence"}}"""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": model, "max_tokens": 250,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"]
        data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        return int(data["score"]), str(data.get("intent", "")), str(data.get("reason", ""))
    except Exception as e:  # never let one bad call stop the run
        print(f"  ! LLM scoring failed: {e}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------
# SOURCES  (each returns list of dicts: source,url,title,body,author,created_at)
# --------------------------------------------------------------------------
def _get(url, **kw):
    headers = kw.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)
    r = requests.get(url, headers=headers, timeout=30, **kw)
    r.raise_for_status()
    return r


def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def fetch_reddit():
    out = []
    for sub in SUBREDDITS:
        for q in QUERIES:
            try:
                r = _get(
                    f"https://www.reddit.com/r/{sub}/search.json",
                    params={"q": q, "restrict_sr": 1, "sort": "new", "t": "month", "limit": 50},
                )
                for c in r.json()["data"]["children"]:
                    d = c["data"]
                    out.append({
                        "source": f"reddit/r/{d.get('subreddit', sub)}",
                        "url": "https://www.reddit.com" + d["permalink"],
                        "title": d.get("title", ""),
                        "body": d.get("selftext", ""),
                        "author": d.get("author", ""),
                        "created_at": int(d.get("created_utc", 0)),
                    })
            except Exception as e:
                print(f"  ! reddit r/{sub} '{q}': {e}", file=sys.stderr)
            time.sleep(1.5)
    for sub in SUBREDDITS_GENERAL:
        for q in GENERAL_QUERIES:
            try:
                r = _get(
                    f"https://www.reddit.com/r/{sub}/search.json",
                    params={"q": q, "restrict_sr": 1, "sort": "new", "t": "month", "limit": 25},
                )
                for c in r.json()["data"]["children"]:
                    d = c["data"]
                    out.append({
                        "source": f"reddit/r/{d.get('subreddit', sub)}",
                        "url": "https://www.reddit.com" + d["permalink"],
                        "title": d.get("title", ""),
                        "body": d.get("selftext", ""),
                        "author": d.get("author", ""),
                        "created_at": int(d.get("created_utc", 0)),
                    })
            except Exception as e:
                print(f"  ! reddit r/{sub} '{q}': {e}", file=sys.stderr)
            time.sleep(1.5)
    return out


def fetch_stackexchange():
    out = []
    for q in QUERIES:
        try:
            r = _get(
                "https://api.stackexchange.com/2.3/search/advanced",
                params={"order": "desc", "sort": "creation", "q": q, "site": "salesforce",
                        "pagesize": 30, "filter": "withbody"},
            )
            for it in r.json().get("items", []):
                out.append({
                    "source": "salesforce.stackexchange",
                    "url": it["link"],
                    "title": html.unescape(it.get("title", "")),
                    "body": strip_html(it.get("body", "")),
                    "author": (it.get("owner") or {}).get("display_name", ""),
                    "created_at": int(it.get("creation_date", 0)),
                })
        except Exception as e:
            print(f"  ! stackexchange '{q}': {e}", file=sys.stderr)
        time.sleep(1)
    return out


def fetch_hn():
    out = []
    for q in HN_QUERIES:
        try:
            r = _get(
                "https://hn.algolia.com/api/v1/search_by_date",
                params={"query": q, "tags": "(story,comment)", "hitsPerPage": 30},
            )
            for h in r.json().get("hits", []):
                body = h.get("comment_text") or h.get("story_text") or ""
                title = h.get("title") or h.get("story_title") or ""
                out.append({
                    "source": "hackernews",
                    "url": f"https://news.ycombinator.com/item?id={h['objectID']}",
                    "title": title,
                    "body": strip_html(body),
                    "author": h.get("author", ""),
                    "created_at": int(h.get("created_at_i", 0)),
                })
        except Exception as e:
            print(f"  ! hackernews '{q}': {e}", file=sys.stderr)
        time.sleep(1)
    return out


# --------------------------------------------------------------------------
# STORAGE
# --------------------------------------------------------------------------
def db_connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS posts(
        url TEXT PRIMARY KEY, source TEXT, title TEXT, body TEXT, author TEXT,
        created_at INTEGER, score INTEGER, intent TEXT, reason TEXT,
        scored_by TEXT, first_seen INTEGER, status TEXT DEFAULT 'new')""")
    return con


def ingest(con, posts):
    """Store new posts. Returns (count_new, list_of_new_strong_leads)."""
    new = 0
    strong = []
    for p in posts:
        if con.execute("SELECT 1 FROM posts WHERE url=?", (p["url"],)).fetchone():
            continue
        score, intent, reason = rule_score(p["title"], p["body"], p["created_at"])
        by = "rules"
        if score >= 3:
            res = llm_score(p["title"], p["body"])
            if res:
                score, intent, reason = res[0], res[1] or intent, res[2] or reason
                by = "llm"
        con.execute(
            "INSERT INTO posts(url,source,title,body,author,created_at,score,intent,reason,scored_by,first_seen)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (p["url"], p["source"], p["title"], p["body"], p["author"], p["created_at"],
             score, intent, reason, by, int(time.time())),
        )
        new += 1
        if score >= NOTIFY_MIN_SCORE:
            strong.append({"score": score, "intent": intent, "title": p["title"],
                           "url": p["url"], "source": p["source"]})
    con.commit()
    return new, strong


def notify(strong):
    """Optional Slack/Discord alert. Set env NOTIFY_WEBHOOK_URL to turn on."""
    url = os.environ.get("NOTIFY_WEBHOOK_URL")
    if not url or not strong:
        return
    strong = sorted(strong, key=lambda s: -s["score"])[:10]
    lines = [f"{len(strong)} new CleanSlate lead(s):"]
    for s in strong:
        lines.append(f"[{s['score']}/10 {s['intent']}] {s['title'][:90]} - {s['url']}")
    msg = "\n".join(lines)
    try:
        # Slack reads "text", Discord reads "content"; each ignores the other.
        requests.post(url, json={"text": msg, "content": msg}, timeout=20).raise_for_status()
        print("Webhook alert sent.")
    except Exception as e:
        print(f"  ! webhook failed: {e}", file=sys.stderr)


def export_json(con, path, min_score=3):
    """Write the data file the dashboard website reads."""
    rows = con.execute(
        "SELECT url,source,title,author,created_at,score,intent,reason,body,scored_by,first_seen"
        " FROM posts WHERE score>=? ORDER BY score DESC, created_at DESC", (min_score,)).fetchall()
    leads = []
    for url, source, title, author, ts, score, intent, reason, body, by, seen in rows:
        leads.append({
            "url": url, "source": source, "title": title or "", "author": author or "",
            "date": fmt_date(ts), "created_at": ts or 0, "score": score, "intent": intent or "",
            "reason": reason or "", "snippet": (body or "")[:400], "scored_by": by,
            "first_seen": seen or 0,
        })
    data = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "count": len(leads), "leads": leads}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return len(leads)


# --------------------------------------------------------------------------
# REPORT
# --------------------------------------------------------------------------
def write_report(con, min_score, out_prefix="leads"):
    rows = con.execute(
        "SELECT score,intent,source,title,url,author,created_at,reason,body,scored_by FROM posts"
        " WHERE score>=? ORDER BY score DESC, created_at DESC", (min_score,)).fetchall()

    with open(f"{out_prefix}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["score", "intent", "source", "title", "url", "author", "date", "reason", "scored_by"])
        for r in rows:
            w.writerow([r[0], r[1], r[2], r[3], r[4], r[5], fmt_date(r[6]), r[7], r[9]])

    cards = []
    for score, intent, source, title, url, author, ts, reason, body, by in rows:
        snippet = html.escape((body or "")[:300]) + ("…" if body and len(body) > 300 else "")
        cards.append(f"""<div class="card s{min(score,10)}">
  <div class="top"><span class="score">{score}</span>
  <span class="tag">{html.escape(intent or '')}</span>
  <span class="meta">{html.escape(source)} · {html.escape(author or '?')} · {fmt_date(ts)}</span></div>
  <a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(title or '(no title)')}</a>
  <p class="snip">{snippet}</p>
  <p class="why">Why: {html.escape(reason or '')} <em>({by})</em></p></div>""")

    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>CleanSlate leads</title>
<style>
body{{font:15px/1.5 system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}}
h1{{font-size:1.4rem}} .card{{border:1px solid #ddd;border-left:5px solid #bbb;border-radius:8px;padding:.8rem 1rem;margin:.8rem 0}}
.s9,.s10{{border-left-color:#16a34a}} .s7,.s8{{border-left-color:#65a30d}} .s5,.s6{{border-left-color:#d97706}}
.score{{font-weight:700;font-size:1.1rem;margin-right:.6rem}} .tag{{background:#eef;border-radius:4px;padding:1px 7px;font-size:.8rem}}
.meta{{color:#666;font-size:.8rem;margin-left:.6rem}} a{{font-weight:600;color:#0b57d0;text-decoration:none}}
.snip{{color:#444;margin:.4rem 0}} .why{{color:#666;font-size:.85rem;margin:0}}
</style></head><body><h1>CleanSlate leads — {len(rows)} matches (score ≥ {min_score})</h1>
<p>Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}. Reply helpfully first; mention CleanSlate only if it fits.</p>
{''.join(cards) or '<p>No matches yet.</p>'}</body></html>"""
    with open(f"{out_prefix}.html", "w", encoding="utf-8") as f:
        f.write(page)
    return len(rows)


def fmt_date(ts):
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# DEMO DATA
# --------------------------------------------------------------------------
def demo_posts():
    now = int(time.time())
    d = 86400
    return [
        {"source": "reddit/r/salesforceadmin", "url": "https://example.com/1", "author": "ops_amy",
         "title": "Looking for a dedupe tool for duplicate leads - anything better than native duplicate rules?",
         "body": "I'm a Salesforce admin and our web-to-lead form creates duplicate leads every day. "
                 "Duplicate rules miss fuzzy company names. Any app on AppExchange you'd recommend?",
         "created_at": now - 2 * d},
        {"source": "reddit/r/salesforce", "url": "https://example.com/2", "author": "revops_raj",
         "title": "Cloudingo pricing is getting out of hand, alternatives?",
         "body": "We use Cloudingo to merge duplicate records but renewal doubled. Anyone switched?",
         "created_at": now - 5 * d},
        {"source": "salesforce.stackexchange", "url": "https://example.com/3", "author": "dev_dan",
         "title": "Lead conversion creates duplicate contacts",
         "body": "When converting leads we get duplicate contacts even with matching rules enabled.",
         "created_at": now - 20 * d},
        {"source": "reddit/r/salesforce", "url": "https://example.com/4", "author": "student_sam",
         "title": "Admin certification exam: how do duplicate rules work?",
         "body": "Studying for the exam, can someone explain duplicate rules?",
         "created_at": now - 3 * d},
        {"source": "salesforce.stackexchange", "url": "https://example.com/5", "author": "old_user",
         "title": "Merge duplicate accounts with Apex",
         "body": "Need to merge accounts in a trigger.", "created_at": now - 900 * d},
        {"source": "reddit/r/startups", "url": "https://example.com/7", "author": "founder_fay",
         "title": "Salesforce duplicate leads are killing our small team - cheap dedupe tool?",
         "body": "We're a startup with 12 reps and I'm the only admin. Cloudingo is too expensive. "
                 "Any affordable way to merge duplicate leads?",
         "created_at": now - 1 * d},
        {"source": "reddit/r/salesforce", "url": "https://example.com/8", "author": "bigco_ben",
         "title": "Duplicate management strategy for enterprise multi-org with 20000 users",
         "body": "Looking for MDM and data governance approach to merge duplicate accounts globally.",
         "created_at": now - 2 * d},
        {"source": "reddit/r/salesforce", "url": "https://example.com/6", "author": "random",
         "title": "Best flow for approval processes?", "body": "Quick question on flows.",
         "created_at": now - 1 * d},
    ]


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="CleanSlate lead finder")
    ap.add_argument("command", choices=["run", "report", "demo"])
    ap.add_argument("--db", default="leads.db")
    ap.add_argument("--out", default=".", help="folder for leads.csv / leads.html / leads.json")
    ap.add_argument("--min-score", type=int, default=4)
    args = ap.parse_args()

    if args.command == "demo":
        args.db = "demo.db"
        if os.path.exists(args.db):
            os.remove(args.db)
        con = db_connect(args.db)
        n, _ = ingest(con, demo_posts())
        shown = write_report(con, args.min_score, out_prefix="demo_leads")
        export_json(con, "demo_leads.json")
        print(f"Demo: {n} posts scored, {shown} shown -> demo_leads.html / .csv / .json")
        return

    con = db_connect(args.db)
    os.makedirs(args.out, exist_ok=True)
    if args.command == "run":
        posts = []
        for name, fn in [("Reddit", fetch_reddit), ("Salesforce Stack Exchange", fetch_stackexchange),
                         ("Hacker News", fetch_hn)]:
            print(f"Fetching {name}...")
            got = fn()
            print(f"  {len(got)} posts")
            posts += got
        n, strong = ingest(con, posts)
        print(f"New posts stored: {n}")
        notify(strong)
    shown = write_report(con, args.min_score, out_prefix=os.path.join(args.out, "leads"))
    total = export_json(con, os.path.join(args.out, "leads.json"))
    print(f"{shown} leads (score >= {args.min_score}) in report; {total} in leads.json -> {args.out}/")


if __name__ == "__main__":
    main()
