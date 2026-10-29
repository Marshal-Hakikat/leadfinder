#!/usr/bin/env python3
"""
Lead Finder - finds public conversations that show people need YOUR product.

Each product has its own settings file in the folder  projects/
(see projects/cleanslate.json). To add a product, add a file. To pause one,
rename it so it starts with an underscore (for example _paused.json).

How a run works:
  1. Collect posts from the communities named in the settings files (once,
     shared by all products).
  2. Score every post separately for each product (0-10, with a reason).
  3. Write a dashboard file for each product into the --out folder.

Usage:
    python leadfinder.py demo                    # offline test with sample posts
    python leadfinder.py run                     # fetch live, score, write files
    python leadfinder.py run --project cleanslate
    python leadfinder.py report --min-score 5    # rebuild files, no fetching

Optional: set ANTHROPIC_API_KEY to have an AI re-score the promising posts.
"""
import argparse
import csv
import glob
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
# GENERAL SETTINGS
# --------------------------------------------------------------------------
USER_AGENT = os.environ.get(
    "LEADFINDER_USER_AGENT",
    "leadfinder/0.2 (contact: marshal.hakikat@gmail.com)",  # put your email here
) or "leadfinder/0.2 (contact: marshal.hakikat@gmail.com)"   # an empty secret falls back too
NOTIFY_MIN_SCORE = 7   # webhook alert only for new leads at or above this score
MAX_AGE_DAYS = 90      # posts older than about 3 months are dropped
MAX_KEYWORDS = 15      # keywords used per product
LLM_MIN_SCORE = 3      # only posts scoring this much get the optional AI check

# Words that show someone is asking for help / a tool (same for every product)
INTENT = [
    r"looking for", r"recommend", r"any (tool|app|package|solution|suggestions)",
    r"appexchange", r"alternative", r"best (way|tool|app|practice)",
    r"how (do|does|are) (you|everyone|your team|people)", r"anyone (use|using|tried|have)",
    r"what do you use", r"third.party", r"worth (it|the)", r"which (tool|app)", r"\?",
]
# Signals of a small company / startup (the target customer)
STARTUP = [
    r"\bstartup", r"small (team|company|business|org)", r"\bsmb\b",
    r"(solo|only|one|sole|accidental) (sf |salesforce )?admin", r"one.person",
    r"(we're|we are|i'm|i am) (a )?(small|tiny|early)", r"early.stage", r"seed|series a",
    r"\b(\d|1\d|2\d|3\d|4\d)\s*(reps|users|people|employees|licenses)\b",
]
BUDGET = [
    r"\bcheap", r"affordable", r"budget", r"\bfree\b", r"too (expensive|much)",
    r"pricing", r"can't afford|cannot afford", r"low.cost", r"open.source",
]
ENTERPRISE = [
    r"enterprise", r"fortune \d+", r"global (company|org)", r"\b\d{4,}\s*(users|employees)\b",
    r"multi.?org", r"\bmdm\b", r"data governance", r"large (company|org)",
]


# --------------------------------------------------------------------------
# PRODUCT SETTINGS (projects/*.json)
# --------------------------------------------------------------------------
def kw_regex(keyword):
    """Turn a keyword like 'duplicate leads' into a flexible search pattern."""
    tokens = [t for t in re.split(r"[\s_\-]+", keyword.strip()) if t]
    return r"\b" + r"[\s_\-]*".join(re.escape(t) for t in tokens)


def normalize_project(cfg, slug):
    name = cfg.get("name") or slug
    keywords = [str(k).strip() for k in cfg.get("keywords", []) if str(k).strip()]
    if not keywords:
        print(f"  ! {slug}: no keywords in the settings file - skipped", file=sys.stderr)
        return None
    if len(keywords) > MAX_KEYWORDS:
        print(f"  ! {name}: using the first {MAX_KEYWORDS} of {len(keywords)} keywords",
              file=sys.stderr)
        keywords = keywords[:MAX_KEYWORDS]
    competitors = [str(c).strip() for c in cfg.get("competitors", []) if str(c).strip()]
    src = cfg.get("sources", {})
    queries = src.get("queries") or (keywords + competitors)
    return {
        "slug": slug,
        "name": name,
        "description": cfg.get("description", name),
        "keywords": keywords,
        "competitors": competitors,
        "pain": [kw_regex(k) for k in keywords] + list(cfg.get("pain_patterns", [])),
        "noise": list(cfg.get("noise_patterns", [])),
        "role": list(cfg.get("role_patterns", [])),
        "exclude": list(cfg.get("exclude_patterns", [])),
        "small_extra": list(cfg.get("small_company_patterns", [])),
        "target_small": bool(cfg.get("target_small_companies", True)),
        "listed": bool(cfg.get("listed", True)),   # false = only reachable by its own link
        "examples": [str(x) for x in cfg.get("examples", [])][:5],
        "sources": {
            "reddit_subreddits": list(src.get("reddit_subreddits", [])),
            "reddit_general_subreddits": list(src.get("reddit_general_subreddits", [])),
            "queries": queries,
            "general_queries": src.get("general_queries") or queries,
            "stackexchange_sites": list(src.get("stackexchange_sites", [])),
            "hn_queries": list(src.get("hn_queries", [])),
        },
    }


def load_projects(directory="projects", only=None, include_examples=False):
    projects = []
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        base = os.path.basename(path)
        if base.startswith("_") and not include_examples:
            continue
        slug = re.sub(r"^_+", "", os.path.splitext(base)[0])
        if only and slug != only:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"  ! cannot read {path}: {e}", file=sys.stderr)
            continue
        proj = normalize_project(cfg, slug)
        if proj:
            projects.append(proj)
    return projects


# --------------------------------------------------------------------------
# SCORING (rule-based, works with no API key)
# --------------------------------------------------------------------------
def _hits(patterns, text):
    out = []
    for p in patterns:
        try:
            if re.search(p, text, re.I):
                out.append(p)
        except re.error:
            pass  # ignore a broken pattern instead of crashing
    return out


def rule_score(proj, title, body, created_at):
    """Return (score 0-10, lead type, reason text) for one product."""
    title = title or ""
    text = f"{title}\n{body or ''}"
    reasons = []
    score = 0

    pain_title = _hits(proj["pain"], title)
    pain_all = _hits(proj["pain"], text)
    comp = [c for c in proj["competitors"]
            if re.search(r"\b" + re.escape(c) + r"\b", text, re.I)]
    intent = _hits(INTENT, text)

    if not pain_all and not comp:
        return 0, "discussion", "no keyword or competitor match"

    if pain_title:
        score += 3
        reasons.append("problem keywords in title")
    elif pain_all:
        score += 2
        reasons.append("problem keywords in body")
    if len(pain_all) > 2:
        score += 1
        reasons.append("several keyword matches")
    if comp:
        score += 3
        reasons.append("mentions competitor: " + ", ".join(comp[:3]))
    if intent:
        score += 2
        reasons.append("asking/seeking")
        if comp:
            score += 1
    if _hits(proj["role"], text):
        score += 1
        reasons.append("target role")
    if _hits(proj["exclude"], text):
        score -= 3
        reasons.append("learning/exam noise")
    if proj["target_small"]:
        if _hits(STARTUP + proj["small_extra"], text):
            score += 2
            reasons.append("small company/startup signal")
        if _hits(BUDGET, text):
            score += 1
            reasons.append("price-sensitive")
        if _hits(ENTERPRISE, text):
            score -= 2
            reasons.append("enterprise signal (not target)")
    if _hits(proj["noise"], text):
        score -= 3
        reasons.append("off-topic noise (not a buyer)")

    if created_at:
        age_days = (time.time() - created_at) / 86400
        if age_days > 60:
            score -= 4
            reasons.append("old post (2+ months)")
        elif age_days > 30:
            score -= 2
            reasons.append("older post (1+ month)")
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
# OPTIONAL AI SCORING (needs ANTHROPIC_API_KEY; skipped if not set)
# --------------------------------------------------------------------------
def llm_score(proj, title, body):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    model = os.environ.get("LEADFINDER_MODEL", "claude-haiku-5-5")
    examples = ""
    if proj["examples"]:
        examples = "\nExamples of posts that ARE good leads for this product:\n" + \
                   "\n".join(f"- {e[:300]}" for e in proj["examples"]) + "\n"
    prompt = f"""You are qualifying sales leads. Product: {proj['description']}
{examples}
Public post:
TITLE: {title}
BODY: {(body or '')[:2500]}

Score 1-10 how likely the author (or their company) would buy this product.
9-10 = actively asking for a tool or unhappy with a competitor;
6-8 = clear problem, no tool request yet; 3-5 = vague/related;
1-2 = unrelated, learning/exam question, or already solved.
Reply with ONLY JSON: {{"score": int, "intent": "tool_request|competitor|pain|discussion", "reason": "one sentence"}}"""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 250,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"]
        data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        return int(data["score"]), str(data.get("intent", "")), str(data.get("reason", ""))
    except Exception as e:  # never let one bad call stop the run
        print(f"  ! AI scoring failed: {e}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------
# SOURCES  (each returns a list of dicts: source,url,title,body,author,created_at)
# --------------------------------------------------------------------------
def _get(url, **kw):
    headers = kw.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)
    r = requests.get(url, headers=headers, timeout=30, **kw)
    r.raise_for_status()
    return r


def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def fetch_reddit(projects):
    jobs, seen, out = [], set(), []
    for p in projects:
        s = p["sources"]
        for sub in s["reddit_subreddits"]:
            jobs += [(sub, q, 50) for q in s["queries"]]
        for sub in s["reddit_general_subreddits"]:
            jobs += [(sub, q, 25) for q in s["general_queries"]]
    blocked = 0
    for sub, q, limit in jobs:
        if (sub, q) in seen:
            continue
        seen.add((sub, q))
        try:
            r = _get(f"https://www.reddit.com/r/{sub}/search.json",
                     params={"q": q, "restrict_sr": 1, "sort": "new", "t": "month", "limit": limit})
            blocked = 0
            for c in r.json()["data"]["children"]:
                d = c["data"]
                out.append({
                    "source": f"reddit/r/{d.get('subreddit', sub)}",
                    "url": "https://www.reddit.com" + d["permalink"],
                    "title": d.get("title", ""), "body": d.get("selftext", ""),
                    "author": d.get("author", ""), "created_at": int(d.get("created_utc", 0)),
                })
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code in (403, 429):
                blocked += 1
                if blocked >= 3:
                    print("  ! Reddit is blocking this computer (403/429) - skipping the rest of Reddit.",
                          file=sys.stderr)
                    break
            print(f"  ! reddit r/{sub} '{q}': HTTP {code}", file=sys.stderr)
        except Exception as e:
            print(f"  ! reddit r/{sub} '{q}': {e}", file=sys.stderr)
        time.sleep(1.5)
    return out


def _to_epoch(v):
    """Accept seconds, milliseconds or an ISO date; return epoch seconds."""
    if v in (None, ""):
        return 0
    try:
        n = float(v)
        return int(n / 1000) if n > 1e11 else int(n)
    except (TypeError, ValueError):
        try:
            import datetime as _dt
            return int(_dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0


def fetch_redditapis(projects):
    """Reddit through the paid/free-tier service redditapis.com (needs REDDITAPIS_KEY).
    Capped by REDDITAPIS_MAX_CALLS per run (default 30 = about 900 a month, inside
    the free 1000 reads a month)."""
    key = os.environ.get("REDDITAPIS_KEY")
    if not key:
        return []
    cap = int(os.environ.get("REDDITAPIS_MAX_CALLS") or 30)
    jobs, seen, out = [], set(), []
    for p in projects:
        s = p["sources"]
        for sub in s["reddit_subreddits"]:
            jobs += [(sub, q) for q in s["queries"]]
    calls = 0
    for sub, q in jobs:
        if (sub.lower(), q.lower()) in seen:
            continue
        seen.add((sub.lower(), q.lower()))
        if calls >= cap:
            print(f"  ! redditapis: stopped at {cap} calls (REDDITAPIS_MAX_CALLS).", file=sys.stderr)
            break
        calls += 1
        try:
            r = requests.get("https://api.redditapis.com/api/reddit/search",
                             headers={"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT},
                             params={"q": q, "subreddit": sub, "sort": "new", "limit": 25}, timeout=30)
            if r.status_code in (401, 402, 403, 429):
                print(f"  ! redditapis: HTTP {r.status_code} (key, credits or limit) - stopping Reddit.",
                      file=sys.stderr)
                break
            r.raise_for_status()
            for d in r.json().get("posts", []):
                link = d.get("permalink") or ""
                if link.startswith("/"):
                    link = "https://www.reddit.com" + link
                if not link.startswith("http"):
                    continue
                out.append({
                    "source": f"reddit/r/{d.get('subreddit') or sub}", "url": link,
                    "title": d.get("title", ""), "body": d.get("text") or d.get("selftext") or "",
                    "author": d.get("author", ""),
                    "created_at": _to_epoch(d.get("created_utc") or d.get("created")),
                })
        except Exception as e:
            print(f"  ! redditapis r/{sub} '{q}': {e}", file=sys.stderr)
        time.sleep(0.5)
    print(f"  redditapis: {calls} calls used.")
    return out


def fetch_lemmy(projects):
    """Lemmy (open Reddit-like network). No key. Searches all communities on one server."""
    server = os.environ.get("LEMMY_SERVER", "lemmy.world")
    queries, out = [], []
    for p in projects:
        for q in p["sources"]["hn_queries"][:3]:
            if q not in queries:
                queries.append(q)
    for q in queries[:10]:
        try:
            r = _get(f"https://{server}/api/v3/search",
                     params={"q": q, "type_": "Posts", "sort": "New", "limit": 20})
            for it in r.json().get("posts", []):
                post = it.get("post", {})
                url = post.get("ap_id")
                if not url:
                    continue
                out.append({
                    "source": f"lemmy/{(it.get('community') or {}).get('name', '')}", "url": url,
                    "title": post.get("name", ""), "body": post.get("body") or "",
                    "author": (it.get("creator") or {}).get("name", ""),
                    "created_at": _to_epoch(post.get("published")),
                })
        except Exception as e:
            print(f"  ! lemmy '{q}': {e}", file=sys.stderr)
        time.sleep(1)
    return out


def reddit_source():
    """Use the paid service when its key is set, otherwise Reddit directly."""
    return ("Reddit (redditapis.com)", fetch_redditapis) if os.environ.get("REDDITAPIS_KEY") \
        else ("Reddit", fetch_reddit)


def fetch_stackexchange(projects):
    jobs, seen, out = [], set(), []
    for p in projects:
        for site in p["sources"]["stackexchange_sites"]:
            jobs += [(site, q) for q in p["sources"]["queries"]]
    for site, q in jobs:
        if (site, q) in seen:
            continue
        seen.add((site, q))
        try:
            r = _get("https://api.stackexchange.com/2.3/search/advanced",
                     params={"order": "desc", "sort": "creation", "q": q, "site": site,
                             "pagesize": 30, "filter": "withbody",
                             **({"key": os.environ["SE_KEY"]} if os.environ.get("SE_KEY") else {})})
            for it in r.json().get("items", []):
                out.append({
                    "source": f"{site}.stackexchange", "url": it["link"],
                    "title": html.unescape(it.get("title", "")),
                    "body": strip_html(it.get("body", "")),
                    "author": (it.get("owner") or {}).get("display_name", ""),
                    "created_at": int(it.get("creation_date", 0)),
                })
        except Exception as e:
            print(f"  ! stackexchange {site} '{q}': {e}", file=sys.stderr)
        time.sleep(1)
    return out


def fetch_hn(projects):
    queries, out = [], []
    for p in projects:
        for q in p["sources"]["hn_queries"]:
            if q not in queries:
                queries.append(q)
    for q in queries:
        try:
            r = _get("https://hn.algolia.com/api/v1/search_by_date",
                     params={"query": q, "tags": "(story,comment)", "hitsPerPage": 30})
            for h in r.json().get("hits", []):
                body = h.get("comment_text") or h.get("story_text") or ""
                title = h.get("title") or h.get("story_title") or ""
                out.append({
                    "source": "hackernews",
                    "url": f"https://news.ycombinator.com/item?id={h['objectID']}",
                    "title": title, "body": strip_html(body), "author": h.get("author", ""),
                    "created_at": int(h.get("created_at_i", 0)),
                })
        except Exception as e:
            print(f"  ! hackernews '{q}': {e}", file=sys.stderr)
        time.sleep(1)
    return out


# --------------------------------------------------------------------------
# STORAGE  (raw_posts are shared; matches are one row per product + post)
# --------------------------------------------------------------------------
def db_connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS raw_posts(
        url TEXT PRIMARY KEY, source TEXT, title TEXT, body TEXT, author TEXT,
        created_at INTEGER, first_seen INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS matches(
        project TEXT, url TEXT, score INTEGER, intent TEXT, reason TEXT,
        scored_by TEXT, first_seen INTEGER, PRIMARY KEY(project, url))""")
    return con


def store_posts(con, posts):
    """Save new posts (shared by all products). Returns (new, too_old, repeated)."""
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    new = too_old = repeated = 0
    for p in posts:
        ts = p.get("created_at") or 0
        if ts and ts < cutoff:
            too_old += 1
            continue
        cur = con.execute(
            "INSERT OR IGNORE INTO raw_posts(url,source,title,body,author,created_at,first_seen)"
            " VALUES(?,?,?,?,?,?,?)",
            (p["url"], p["source"], p["title"], p["body"], p["author"], ts, int(time.time())))
        if cur.rowcount:
            new += 1
        else:
            repeated += 1
    con.commit()
    return new, too_old, repeated


def refresh(con, projects):
    """Drop old posts, re-score stored matches with the current settings, and
    score new posts for every product. Returns {slug: {"new": n, "strong": [...]}}"""
    cutoff = int(time.time() - MAX_AGE_DAYS * 86400)
    removed = con.execute(
        "DELETE FROM raw_posts WHERE created_at>0 AND created_at<?", (cutoff,)).rowcount
    con.execute("DELETE FROM matches WHERE url NOT IN (SELECT url FROM raw_posts)")

    rescored = 0
    for p in projects:  # settings may have changed since the last run
        rows = con.execute(
            "SELECT m.url, r.title, r.body, r.created_at FROM matches m "
            "JOIN raw_posts r ON r.url=m.url WHERE m.project=? AND m.scored_by='rules'",
            (p["slug"],)).fetchall()
        for url, title, body, ts in rows:
            score, kind, reason = rule_score(p, title, body, ts)
            con.execute("UPDATE matches SET score=?,intent=?,reason=? WHERE project=? AND url=?",
                        (score, kind, reason, p["slug"], url))
            rescored += 1

    result = {}
    for p in projects:
        new, strong = 0, []
        rows = con.execute(
            "SELECT url,source,title,body,created_at FROM raw_posts "
            "WHERE url NOT IN (SELECT url FROM matches WHERE project=?)", (p["slug"],)).fetchall()
        for url, source, title, body, ts in rows:
            score, kind, reason = rule_score(p, title, body, ts)
            if score <= 0:
                continue
            by = "rules"
            if score >= LLM_MIN_SCORE:
                res = llm_score(p, title, body)
                if res:
                    score, kind, reason = res[0], res[1] or kind, res[2] or reason
                    by = "llm"
            con.execute(
                "INSERT INTO matches(project,url,score,intent,reason,scored_by,first_seen)"
                " VALUES(?,?,?,?,?,?,?)", (p["slug"], url, score, kind, reason, by, int(time.time())))
            new += 1
            if score >= NOTIFY_MIN_SCORE:
                strong.append({"score": score, "intent": kind, "title": title,
                               "url": url, "source": source})
        result[p["slug"]] = {"new": new, "strong": strong}
    con.commit()
    print(f"Cleanup: removed {removed} old posts, re-scored {rescored} stored matches.")
    return result


def notify(project_name, strong):
    """Optional Slack/Discord alert. Set env NOTIFY_WEBHOOK_URL to turn on."""
    url = os.environ.get("NOTIFY_WEBHOOK_URL")
    if not url or not strong:
        return
    strong = sorted(strong, key=lambda s: -s["score"])[:10]
    lines = [f"{len(strong)} new lead(s) for {project_name}:"]
    for s in strong:
        lines.append(f"[{s['score']}/10 {s['intent']}] {s['title'][:90]} - {s['url']}")
    msg = "\n".join(lines)
    try:
        # Slack reads "text", Discord reads "content"; each ignores the other.
        requests.post(url, json={"text": msg, "content": msg}, timeout=20).raise_for_status()
        print(f"Webhook alert sent for {project_name}.")
    except Exception as e:
        print(f"  ! webhook failed: {e}", file=sys.stderr)


# --------------------------------------------------------------------------
# OUTPUT FILES
# --------------------------------------------------------------------------
def fmt_date(ts):
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def project_rows(con, slug, min_score):
    return con.execute(
        "SELECT m.score,m.intent,r.source,r.title,r.url,r.author,r.created_at,m.reason,"
        "r.body,m.scored_by,m.first_seen FROM matches m JOIN raw_posts r ON r.url=m.url "
        "WHERE m.project=? AND m.score>=? ORDER BY m.score DESC, r.created_at DESC",
        (slug, min_score)).fetchall()


def write_report(rows, name, out_prefix):
    """leads.csv and leads.html (handy for running on your own computer)."""
    with open(f"{out_prefix}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["score", "intent", "source", "title", "url", "author", "date", "reason", "scored_by"])
        for r in rows:
            w.writerow([r[0], r[1], r[2], r[3], r[4], r[5], fmt_date(r[6]), r[7], r[9]])

    cards = []
    for score, intent, source, title, url, author, ts, reason, body, by, _seen in rows:
        snippet = html.escape((body or "")[:300]) + ("…" if body and len(body) > 300 else "")
        cards.append(f"""<div class="card s{min(score,10)}">
  <div class="top"><span class="score">{score}</span>
  <span class="tag">{html.escape(intent or '')}</span>
  <span class="meta">{html.escape(source)} · {html.escape(author or '?')} · {fmt_date(ts)}</span></div>
  <a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(title or '(no title)')}</a>
  <p class="snip">{snippet}</p>
  <p class="why">Why: {html.escape(reason or '')} <em>({by})</em></p></div>""")
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(name)} leads</title>
<style>
body{{font:15px/1.5 system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}}
h1{{font-size:1.4rem}} .card{{border:1px solid #ddd;border-left:5px solid #bbb;border-radius:8px;padding:.8rem 1rem;margin:.8rem 0}}
.s9,.s10{{border-left-color:#16a34a}} .s7,.s8{{border-left-color:#65a30d}} .s5,.s6{{border-left-color:#d97706}}
.score{{font-weight:700;font-size:1.1rem;margin-right:.6rem}} .tag{{background:#eef;border-radius:4px;padding:1px 7px;font-size:.8rem}}
.meta{{color:#666;font-size:.8rem;margin-left:.6rem}} a{{font-weight:600;color:#0b57d0;text-decoration:none}}
.snip{{color:#444;margin:.4rem 0}} .why{{color:#666;font-size:.85rem;margin:0}}
</style></head><body><h1>{html.escape(name)} leads — {len(rows)} matches</h1>
<p>Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}. Reply helpfully first; mention your product only if it fits.</p>
{''.join(cards) or '<p>No matches yet.</p>'}</body></html>"""
    with open(f"{out_prefix}.html", "w", encoding="utf-8") as f:
        f.write(page)


def write_json(rows, name, slug, path):
    """The data file the dashboard website reads."""
    leads = []
    for score, intent, source, title, url, author, ts, reason, body, by, seen in rows:
        leads.append({
            "url": url, "source": source, "title": title or "", "author": author or "",
            "date": fmt_date(ts), "created_at": ts or 0, "score": score, "intent": intent or "",
            "reason": reason or "", "snippet": (body or "")[:400], "scored_by": by,
            "first_seen": seen or 0,
        })
    data = {"project": slug, "name": name,
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "count": len(leads), "leads": leads}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def export_all(con, projects, out_dir, min_score):
    os.makedirs(out_dir, exist_ok=True)
    index = []
    for p in projects:
        folder = os.path.join(out_dir, p["slug"])
        os.makedirs(folder, exist_ok=True)
        json_rows = project_rows(con, p["slug"], 3)
        write_json(json_rows, p["name"], p["slug"], os.path.join(folder, "leads.json"))
        report_rows = [r for r in json_rows if r[0] >= min_score]
        write_report(report_rows, p["name"], os.path.join(folder, "leads"))
        if p["listed"]:   # unlisted products are not shown in the website's product list
            index.append({"slug": p["slug"], "name": p["name"], "count": len(json_rows)})
        print(f"{p['name']}: {len(report_rows)} leads (score >= {min_score}), "
              f"{len(json_rows)} in dashboard data -> {folder}/")
    with open(os.path.join(out_dir, "projects.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                   "projects": index}, f, ensure_ascii=False, indent=1)


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
        {"source": "reddit/r/startups", "url": "https://example.com/7", "author": "founder_fay",
         "title": "Salesforce duplicate leads are killing our small team - cheap dedupe tool?",
         "body": "We're a startup with 12 reps and I'm the only admin. Cloudingo is too expensive. "
                 "Any affordable way to merge duplicate leads?",
         "created_at": now - 1 * d},
        {"source": "reddit/r/salesforce", "url": "https://example.com/8", "author": "bigco_ben",
         "title": "Duplicate management strategy for enterprise multi-org with 20000 users",
         "body": "Looking for MDM and data governance approach to merge duplicate accounts globally.",
         "created_at": now - 2 * d},
        {"source": "salesforce.stackexchange", "url": "https://example.com/3", "author": "dev_dan",
         "title": "Lead conversion creates duplicate contacts",
         "body": "When converting leads we get duplicate contacts even with matching rules enabled.",
         "created_at": now - 20 * d},
        {"source": "reddit/r/salesforce", "url": "https://example.com/4", "author": "student_sam",
         "title": "Admin certification exam: how do duplicate rules work?",
         "body": "Studying for the exam, can someone explain duplicate rules?",
         "created_at": now - 3 * d},
        {"source": "reddit/r/salesforceadmin", "url": "https://example.com/9", "author": "sales_sue",
         "title": "Need a better way to do lead routing and territory assignment - any tool?",
         "body": "We're a small team of 8 reps. Round robin is not enough, we need territory assignment by "
                 "state and company size. Can anyone recommend an app?",
         "created_at": now - 3 * d},
        {"source": "reddit/r/salesforce", "url": "https://example.com/10", "author": "ops_omar",
         "title": "LeanData alternative for lead routing? Too expensive for us",
         "body": "We use LeanData for lead routing but the price is too much for our startup.",
         "created_at": now - 6 * d},
        {"source": "reddit/r/salesforce", "url": "https://example.com/6", "author": "random",
         "title": "Best flow for approval processes?", "body": "Quick question on flows.",
         "created_at": now - 1 * d},
    ]


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Lead finder (one settings file per product)")
    ap.add_argument("command", choices=["run", "report", "demo"])
    ap.add_argument("--db", default="leads.db")
    ap.add_argument("--out", default=".", help="folder for the output files")
    ap.add_argument("--min-score", type=int, default=4)
    ap.add_argument("--project", default=None, help="only this product (file name without .json)")
    ap.add_argument("--projects-dir", default="projects")
    args = ap.parse_args()

    if args.command == "demo":
        projects = load_projects(args.projects_dir, args.project, include_examples=True)
        if not projects:
            sys.exit(f"No settings files found in {args.projects_dir}/")
        if os.path.exists("demo.db"):
            os.remove("demo.db")
        con = db_connect("demo.db")
        new, _, _ = store_posts(con, demo_posts())
        refresh(con, projects)
        print(f"Demo: {new} sample posts scored for {len(projects)} product(s).")
        export_all(con, projects, "demo_out", args.min_score)
        return

    projects = load_projects(args.projects_dir, args.project)
    if not projects:
        sys.exit(f"No settings files found in {args.projects_dir}/ (see projects/cleanslate.json)")
    print("Products: " + ", ".join(f"{p['name']} ({len(p['keywords'])} keywords)" for p in projects))
    con = db_connect(args.db)

    if args.command == "run":
        posts = []
        for name, fn in [reddit_source(), ("Stack Exchange", fetch_stackexchange),
                         ("Hacker News", fetch_hn), ("Lemmy", fetch_lemmy)]:
            print(f"Fetching {name}...")
            got = fn(projects)
            newest = max((g["created_at"] for g in got if g.get("created_at")), default=0)
            print(f"  {len(got)} posts; newest post date: {fmt_date(newest)}")
            posts += got
        new, too_old, repeated = store_posts(con, posts)
        print(f"  Of {len(posts)} posts found: {too_old} older than {MAX_AGE_DAYS} days (skipped), "
              f"{repeated} already stored or repeated, {new} new.")

    result = refresh(con, projects)
    for p in projects:
        r = result[p["slug"]]
        print(f"{p['name']}: {r['new']} new matches.")
        if args.command == "run":
            notify(p["name"], r["strong"])
    export_all(con, projects, args.out, args.min_score)


if __name__ == "__main__":
    main()
