"""Daily job for the customer website (Supabase version).

Reads every active product from the database, collects public posts once,
scores them for each product, and saves the matches back. The website then
shows each customer only their own leads.

Needs these environment variables (GitHub Secrets):
  SUPABASE_URL          https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  the service_role key (secret! never put it in the website)
Optional: REDDITAPIS_KEY (Reddit via redditapis.com), SE_KEY (free Stack Exchange key),
  GITHUB_MODELS_TOKEN (free first-message drafts), LEADFINDER_LOCAL_MODEL (free AI lead check),
  ANTHROPIC_API_KEY (Claude instead of the free models)
"""
import datetime as dt
import os
import sys
import time

import requests

import leadfinder as lf

MAX_QUERIES = 8          # search words per product per source (keeps the run short)
MAX_DRAFTS = int(os.environ.get("MAX_DRAFTS", "25"))   # AI first-message drafts per run
ANGLE_MIN_SCORE = 7
AI_MIN_RULE_SCORE = 3    # only leads with at least this keyword score get the AI check
AI_MAX_POSTS = int(os.environ.get("AI_MAX_POSTS", "120"))       # AI checks per run
AI_MAX_MINUTES = float(os.environ.get("AI_MAX_MINUTES", "20"))  # stop AI checks after this
BATCH = 200


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat() if ts else None


def to_ts(value):
    if not value:
        return 0
    return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


# --------------------------------------------------------------------------
# Small Supabase (PostgREST) client
# --------------------------------------------------------------------------
class Supa:
    def __init__(self, url, key, session=None):
        self.base = url.rstrip("/") + "/rest/v1/"
        self.h = {"apikey": key, "Authorization": f"Bearer {key}"}
        self.s = session or requests

    def _check(self, r):
        if r.status_code >= 300:
            raise RuntimeError(f"database error {r.status_code}: {r.text[:300]}")
        return r

    def select(self, table, params=None, page=1000):
        rows, offset = [], 0
        while True:
            p = dict(params or {})
            p.update({"limit": page, "offset": offset})
            r = self._check(self.s.get(self.base + table, headers=self.h, params=p, timeout=60))
            chunk = r.json()
            rows += chunk
            if len(chunk) < page:
                return rows
            offset += page

    def upsert(self, table, rows, conflict, ignore=False):
        prefer = "resolution=" + ("ignore-duplicates" if ignore else "merge-duplicates") + ",return=minimal"
        for i in range(0, len(rows), BATCH):
            self._check(self.s.post(
                self.base + table, headers={**self.h, "Prefer": prefer, "Content-Type": "application/json"},
                params={"on_conflict": conflict}, json=rows[i:i + BATCH], timeout=60))

    def update(self, table, params, values):
        self._check(self.s.patch(self.base + table, headers={**self.h, "Content-Type": "application/json",
                                                              "Prefer": "return=minimal"},
                                 params=params, json=values, timeout=60))

    def delete(self, table, params):
        self._check(self.s.delete(self.base + table, headers=self.h, params=params, timeout=60))


# --------------------------------------------------------------------------
# Turn a database row into the settings the scoring code understands.
# Customers type plain words only - never patterns - so nothing they type can
# break or slow the job.
# --------------------------------------------------------------------------
def normalize_db_project(row):
    keywords = [k for k in (row.get("keywords") or []) if k.strip()][:lf.MAX_KEYWORDS]
    if not keywords:
        return None
    competitors = [c for c in (row.get("competitors") or []) if c.strip()][:10]
    context = (row.get("context") or "").strip()
    queries = (keywords + competitors)[:MAX_QUERIES]
    hn = [f"{context} {k}".strip() for k in keywords][:5]
    return {
        "slug": row["id"],
        "context": context,
        "name": row["name"],
        "description": row.get("description") or row["name"],
        "keywords": keywords,
        "competitors": competitors,
        "pain": [lf.kw_regex(k) for k in keywords],
        "noise": [lf.kw_regex(w) for w in (row.get("exclude_words") or []) if w.strip()][:10],
        "role": [], "exclude": [], "small_extra": [],
        "target_small": bool(row.get("small_companies", True)),
        "listed": False,
        "examples": list(row.get("examples") or [])[:3],
        "sources": {
            "reddit_subreddits": list(row.get("subreddits") or [])[:8],
            "reddit_general_subreddits": [],
            "queries": queries,
            "general_queries": queries,
            "stackexchange_sites": list(row.get("se_sites") or [])[:4]
                                   + (["stackoverflow"] if context.lower() == "salesforce" else []),
            "hn_queries": hn,
        },
    }


# --------------------------------------------------------------------------
# AI draft of a first message for the best leads.
# Uses GitHub Models (free, with the job's own GitHub token). If that is not
# available it uses Claude when ANTHROPIC_API_KEY is set. The person always
# reads, edits and sends the message themselves - nothing is posted.
# --------------------------------------------------------------------------
GH_MODELS_URL = "https://models.github.ai/inference/chat/completions"


def draft_prompt(proj, title, body, good_titles=()):
    liked = ""
    if good_titles:
        liked = "\nThe product team liked leads like:\n" + "\n".join(f"- {t[:120]}" for t in good_titles[:3]) + "\n"
    return f"""You help a small product team answer people on public forums.
Product name: {proj['name']}
What it does: {proj['description']}
{liked}
Public post:
TITLE: {title}
BODY: {(body or '')[:1800]}

Write the first reply this team could post under that post.
Rules:
- First, actually help: answer the question or give one useful tip.
- Mention {proj['name']} only if it truly fits the need, in one sentence, and say openly "I work on {proj['name']}".
- Use only facts from "What it does". Never invent features, prices or results.
- 40 to 90 words. Plain, friendly, like a real person. No greeting line, no emojis, no links, no hashtags, no hype.
Reply with ONLY the message text."""


def draft_message(proj, title, body, good_titles=()):
    prompt = draft_prompt(proj, title, body, good_titles)
    gh = os.environ.get("GITHUB_MODELS_TOKEN")
    try:
        if gh:
            r = requests.post(GH_MODELS_URL,
                              headers={"Authorization": f"Bearer {gh}", "Content-Type": "application/json",
                                       "Accept": "application/json", "X-GitHub-Api-Version": "2022-11-28"},
                              json={"model": os.environ.get("DRAFT_MODEL", "openai/gpt-4o-mini"),
                                    "max_tokens": 250, "temperature": 0.4,
                                    "messages": [{"role": "user", "content": prompt}]}, timeout=60)
            if r.status_code == 429:
                print("  ! GitHub Models: daily free limit reached; more drafts tomorrow.", file=sys.stderr)
                return "STOP"
            try:
                text = r.json()["choices"][0]["message"]["content"]
            except Exception:
                print(f"  ! GitHub Models answered HTTP {r.status_code} ({r.headers.get('content-type')}): "
                      f"{r.text[:200]!r}", file=sys.stderr)
                return "FAIL"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            text = lf.ai_ask(prompt, max_tokens=250)
        else:
            return None
        text = (text or "").strip().strip('"').strip()
        return text[:900] or None
    except Exception as e:
        print(f"  ! AI draft failed: {e}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------
def run(db, fetchers=None, now=None):
    now = now or time.time()
    rows = db.select("projects", {"select": "*", "active": "eq.true"})
    projects = [p for p in (normalize_db_project(r) for r in rows) if p]
    print(f"{len(projects)} active product(s).")

    if projects:
        fetchers = fetchers or lf.all_sources()
        cutoff = now - lf.MAX_AGE_DAYS * 86400
        posts = []
        for name, fn in fetchers:
            print(f"Fetching {name}...")
            got = fn(projects)
            print(f"  {len(got)} posts")
            posts += got
        fresh, seen = [], set()
        for p in posts:
            ts = p.get("created_at") or 0
            if p["url"] in seen or (ts and ts < cutoff):
                continue
            seen.add(p["url"])
            fresh.append({"url": p["url"], "source": p["source"][:120], "title": (p["title"] or "")[:300],
                          "body": (p["body"] or "")[:4000], "author": (p["author"] or "")[:80],
                          "created_at": iso(ts)})
        db.upsert("raw_posts", fresh, "url", ignore=True)
        print(f"  {len(fresh)} recent posts saved (already-known ones are skipped).")

    db.delete("raw_posts", {"created_at": "lt." + iso(now - lf.MAX_AGE_DAYS * 86400)})

    if projects:
        recent = db.select("raw_posts", {"select": "url,source,title,body,created_at", "order": "url.asc"})
        old_rows = db.select("matches", {"select": "project_id,url,scored_by,angle,score,intent,reason", "order": "url.asc"})
        existing = {(m["project_id"], m["url"]): m["scored_by"] for m in old_rows}
        has_draft = {(m["project_id"], m["url"]) for m in old_rows if m.get("angle")}
        judged = {(m["project_id"], m["url"]): m for m in old_rows if m["scored_by"] == "llm"}
        feedback = {}
        for f in db.select("lead_status", {"select": "project_id,title,feedback", "feedback": "eq.good"}):
            feedback.setdefault(f["project_id"], []).append(f.get("title") or "")
        # 1) keyword rules for every product
        per_project = {}
        for p in projects:
            out = []
            for r in recent:
                old = judged.get((p["slug"], r["url"]))
                if old:                 # the AI already judged this one; keep its answer
                    out.append({"project_id": p["slug"], "url": r["url"], "score": old["score"],
                                "intent": old["intent"], "reason": old["reason"], "scored_by": "llm",
                                "_post": r, "_judged": True})
                    continue
                score, kind, reason = lf.rule_score(p, r["title"], r["body"], to_ts(r["created_at"]))
                if score <= 0:
                    continue
                out.append({"project_id": p["slug"], "url": r["url"], "score": score, "intent": kind,
                            "reason": reason, "scored_by": "rules", "_post": r})
            per_project[p["slug"]] = out

        # 2) AI check: the best leads first, until the time or count limit
        if lf.ai_available():
            by_slug = {p["slug"]: p for p in projects}
            todo = [x for rows in per_project.values() for x in rows
                    if x["score"] >= AI_MIN_RULE_SCORE and not x.get("_judged")]
            todo.sort(key=lambda x: (-x["score"], -to_ts(x["_post"]["created_at"])))
            todo = todo[:AI_MAX_POSTS]
            started, done, skipped = time.time(), 0, 0
            print(f"AI check: {len(todo)} lead(s) to check.")
            for x in todo:
                if time.time() - started > AI_MAX_MINUTES * 60:
                    print(f"  time limit reached; the rest will be checked in the next run.")
                    break
                res = lf.llm_score(by_slug[x["project_id"]], x["_post"]["title"], x["_post"]["body"],
                                   feedback.get(x["project_id"], []))
                if not res:
                    skipped += 1
                    continue
                score, kind, reason = res
                x.update({"score": score, "intent": kind or x["intent"],
                          "reason": "AI: " + reason if reason else x["reason"], "scored_by": "llm"})
                done += 1
            print(f"  AI checked {done} lead(s) in {int(time.time() - started)}s ({skipped} failed).")

        # 3) first-message drafts for the best leads, then save
        drafts_left, total_new, made, fails = MAX_DRAFTS, 0, 0, 0
        wants = sorted((x for rows in per_project.values() for x in rows
                        if x["score"] >= ANGLE_MIN_SCORE and (x["project_id"], x["url"]) not in has_draft),
                       key=lambda x: -x["score"])
        by_slug = {p["slug"]: p for p in projects}
        for x in wants:
            if drafts_left <= 0:
                break
            d = draft_message(by_slug[x["project_id"]], x["_post"]["title"], x["_post"]["body"],
                              feedback.get(x["project_id"], []))
            if d == "STOP":
                break
            if d == "FAIL":
                fails += 1
                if fails >= 3:
                    print("  ! drafts: 3 failures in a row, stopping drafts for today.", file=sys.stderr)
                    break
                continue
            drafts_left -= 1
            if d:
                x["angle"] = d
                made += 1
        if wants:
            print(f"Drafts: wrote {made} first message(s) for {len(wants)} strong lead(s).")
        for p in projects:
            out = per_project[p["slug"]]
            for row in out:
                r = row.pop("_post")
                row.pop("_judged", None)
                total_new += (p["slug"], r["url"]) not in existing
            # PostgREST needs the same columns in every row of one request
            with_angle = [x for x in out if "angle" in x]
            without = [x for x in out if "angle" not in x]
            db.upsert("matches", without, "project_id,url")
            db.upsert("matches", with_angle, "project_id,url")
            print(f"{p['name']}: {len(out)} matches ({sum(1 for x in out if (p['slug'], x['url']) not in existing)} new).")

    db.update("app_status", {"id": "eq.1"}, {"last_run": iso(now), "note": f"{len(projects)} products"})


if __name__ == "__main__":
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_KEY (GitHub Secrets).")
    run(Supa(url, key))
