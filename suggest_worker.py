"""AI keyword helper.

Reads waiting requests (table keyword_suggestions, status 'pending'), asks the AI
for keywords, competitors and communities, cleans the answer, and saves it.
Runs every 10 minutes from .github/workflows/suggest.yml.

Needs: SUPABASE_URL, SUPABASE_SERVICE_KEY, and an AI (LEADFINDER_LOCAL_MODEL
for the free model, or ANTHROPIC_API_KEY).
"""
import datetime as dt
import json
import os
import re
import sys
import time

import leadfinder as lf
from cloud_worker import Supa

GIVE_UP_HOURS = 3          # a request still waiting after this is marked failed
SUB_RE = re.compile(r"^[A-Za-z0-9_]{2,30}$")
SITE_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,40}$")
# Stack Exchange sites people usually mean; anything else must look like a site name
SE_NAMES = {"stack overflow": "stackoverflow", "stackoverflow": "stackoverflow",
            "salesforce": "salesforce", "super user": "superuser", "server fault": "serverfault",
            "webmasters": "webmasters", "startups": "startups"}


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def clean(items, limit, max_len=50, words=4):
    """Short, unique phrases in lower case."""
    out, seen = [], set()
    for x in items or []:
        if not isinstance(x, str):
            continue
        x = re.sub(r"\s+", " ", x.strip().strip("\"'#.,;:")).strip()
        if not x or len(x) > max_len or len(x.split()) > words:
            continue
        k = x.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
        if len(out) >= limit:
            break
    return out


def clean_result(data, context=""):
    ctx = (context or "").strip().lower()
    kws = [k.lower() for k in clean(data.get("keywords"), 20)]
    if ctx:  # the context word is added to searches already; drop it from each keyword
        kws = [re.sub(r"\b%s\b" % re.escape(ctx), "", k).strip() or k for k in kws]
    kws = clean(kws, 12)
    subs = []
    for s in data.get("subreddits") or []:
        if isinstance(s, str):
            s = re.sub(r"^/?r/", "", s.strip(), flags=re.I)
            if SUB_RE.match(s) and s.lower() not in [x.lower() for x in subs]:
                subs.append(s)
    sites = []
    for s in data.get("stackexchange_sites") or []:
        if isinstance(s, str):
            s = s.strip().lower().replace(".stackexchange.com", "").replace(".com", "")
            s = SE_NAMES.get(s, s)
            if SITE_RE.match(s) and s not in sites:
                sites.append(s)
    return {
        "keywords": kws,
        "competitors": clean(data.get("competitors"), 10, max_len=40),
        "subreddits": subs[:8],
        "se_sites": sites[:4],
        "exclude_words": [w.lower() for w in clean(data.get("avoid_words"), 10, max_len=30, words=2)],
    }


def prompt_for(row):
    return f"""You help a small software company find people online who need its product.
Product name: {row['name']}
What it does: {row['description']}
Topic word (optional): {row.get('context') or '-'}

Suggest search words for finding forum posts by people who have the problem this product solves.
Rules:
- keywords: 8 to 12 short phrases (1 to 4 words) that a person with the problem would actually write,
  for example "duplicate leads" or "merge contacts". Lower case. Do not repeat the topic word in every phrase.
- competitors: up to 8 real products that solve the same problem. Only names you are sure exist.
- subreddits: up to 6 real Reddit communities where these people ask questions (name only, no r/).
- stackexchange_sites: up to 3 Stack Exchange sites (for example "salesforce" or "stackoverflow"), or none.
- avoid_words: up to 6 words that mark posts that are NOT buyers (for example "exam", "homework", "hiring").
Reply with ONLY JSON:
{{"keywords": [], "competitors": [], "subreddits": [], "stackexchange_sites": [], "avoid_words": []}}"""


def suggest(row):
    text = lf.ai_ask(prompt_for(row), max_tokens=600, want_json=True)
    if not text:
        return None
    try:
        data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    except Exception as e:
        print(f"  ! AI answer not understood: {e}", file=sys.stderr)
        return None
    result = clean_result(data, row.get("context"))
    return result if result["keywords"] else None


def run(db):
    rows = db.select("keyword_suggestions", {"select": "*", "status": "eq.pending", "order": "created_at.asc"})
    print(f"{len(rows)} request(s) waiting.")
    for row in rows:
        age_h = (time.time() - lf._to_epoch(row["created_at"])) / 3600
        res = suggest(row) if lf.ai_available() else None
        if res:
            db.update("keyword_suggestions", {"id": f"eq.{row['id']}"},
                      {"status": "done", "result": res, "done_at": now_iso()})
            print(f"  done: {row['name']} -> {len(res['keywords'])} keywords, {len(res['competitors'])} competitors, "
                  f"{len(res['subreddits'])} communities")
        elif age_h > GIVE_UP_HOURS:
            db.update("keyword_suggestions", {"id": f"eq.{row['id']}"},
                      {"status": "failed", "done_at": now_iso()})
            print(f"  ! gave up on {row['name']} after {GIVE_UP_HOURS} hours")
        else:
            print(f"  ! no answer for {row['name']} yet; trying again next time")


if __name__ == "__main__":
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_KEY (GitHub Secrets).")
    run(Supa(url, key))
