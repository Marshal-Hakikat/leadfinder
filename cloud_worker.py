"""Daily job for the customer website (Supabase version).

Reads every active product from the database, collects public posts once,
scores them for each product, and saves the matches back. The website then
shows each customer only their own leads.

Needs these environment variables (GitHub Secrets):
  SUPABASE_URL          https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  the service_role key (secret! never put it in the website)
Optional: REDDITAPIS_KEY (Reddit via redditapis.com), SE_KEY (free Stack Exchange key),
  ANTHROPIC_API_KEY (reply angles)
"""
import datetime as dt
import os
import sys
import time

import requests

import leadfinder as lf

MAX_QUERIES = 8          # search words per product per source (keeps the run short)
MAX_ANGLES = 30          # AI reply-angle calls per run (keeps the cost small)
ANGLE_MIN_SCORE = 7
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
            "stackexchange_sites": list(row.get("se_sites") or [])[:4],
            "hn_queries": hn,
        },
    }


# --------------------------------------------------------------------------
# Optional AI: a short, helpful reply idea for the best leads
# --------------------------------------------------------------------------
def llm_angle(proj, title, body, good_titles=()):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    liked = ""
    if good_titles:
        liked = "\nThe customer liked leads like:\n" + "\n".join(f"- {t[:120]}" for t in good_titles[:3]) + "\n"
    prompt = f"""Product: {proj['description']}
{liked}
Public post:
TITLE: {title}
BODY: {(body or '')[:1800]}

In ONE or TWO plain sentences, suggest how a person from this product's team could
reply in a genuinely helpful way (answer the question first; mention the product
only if it truly fits). Do not write the full reply. No hype."""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": os.environ.get("LEADFINDER_MODEL", "claude-haiku-5-5"), "max_tokens": 200,
                  "messages": [{"role": "user", "content": prompt}]}, timeout=60)
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()[:400]
    except Exception as e:
        print(f"  ! AI angle failed: {e}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------
def run(db, fetchers=None, now=None):
    now = now or time.time()
    rows = db.select("projects", {"select": "*", "active": "eq.true"})
    projects = [p for p in (normalize_db_project(r) for r in rows) if p]
    print(f"{len(projects)} active product(s).")

    if projects:
        fetchers = fetchers or [lf.reddit_source(), ("Stack Exchange", lf.fetch_stackexchange),
                                ("Hacker News", lf.fetch_hn), ("Lemmy", lf.fetch_lemmy)]
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
        existing = {(m["project_id"], m["url"]): m["scored_by"]
                    for m in db.select("matches", {"select": "project_id,url,scored_by", "order": "url.asc"})}
        feedback = {}
        for f in db.select("lead_status", {"select": "project_id,title,feedback", "feedback": "eq.good"}):
            feedback.setdefault(f["project_id"], []).append(f.get("title") or "")
        angles_left, total_new = MAX_ANGLES, 0
        for p in projects:
            out, strong = [], []
            for r in recent:
                if existing.get((p["slug"], r["url"])) == "llm":
                    continue
                score, kind, reason = lf.rule_score(p, r["title"], r["body"], to_ts(r["created_at"]))
                is_new = (p["slug"], r["url"]) not in existing
                if score <= 0:
                    continue
                row = {"project_id": p["slug"], "url": r["url"], "score": score, "intent": kind,
                       "reason": reason, "scored_by": "rules"}
                out.append(row)
                if is_new and score >= ANGLE_MIN_SCORE and angles_left > 0:
                    a = llm_angle(p, r["title"], r["body"], feedback.get(p["slug"], []))
                    if a:
                        row["angle"] = a
                        angles_left -= 1
                total_new += is_new
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
