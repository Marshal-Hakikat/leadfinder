# CleanSlate Leads

A free tool that finds people asking about duplicate leads in Salesforce
(Reddit, Salesforce Stack Exchange, Hacker News), scores them, and shows them
on a simple website. Built to find customers for CleanSlate, with a focus on
small companies and startups.

**How it works**
1. GitHub Actions runs `leadfinder.py` every day (free).
2. The script saves new posts in `data/leads.db` and writes `docs/leads.json`.
3. GitHub Pages shows `docs/index.html` as your dashboard (free).

No server, no database, no monthly cost.

---

## Setup (about 15 minutes)

### 1. Create the repository
1. Make a free account at github.com.
2. Click **New repository**. Name it `cleanslate-leads`. Choose **Public**
   (free GitHub Pages needs a public repo on a free account).
3. Upload all files from this folder (**Add file > Upload files**). Keep the
   folder names: `.github/workflows`, `docs`, `data` (if it is missing, it
   is created automatically).

> Public means anyone can see your repo and the lead data. The posts are
> already public, but never put passwords or keys in the files. Use Secrets
> (step 5).

### 2. Add your email
Open `leadfinder.py` and change `you@example.com` in `USER_AGENT` to your real
email. Reddit asks every tool to say who it is.

### 3. Turn on the website
**Settings > Pages > Build and deployment**: Source = **Deploy from a branch**,
Branch = **main**, Folder = **/docs**. Save.
Your site will be at `https://YOUR-USERNAME.github.io/cleanslate-leads/`

### 4. Run it the first time
Open the **Actions** tab. If asked, click to enable workflows. Choose
**Find leads** > **Run workflow**. Wait 1 to 3 minutes, then open your site and
refresh. After this it runs by itself every day.

### 5. (Optional) Get alerts in Slack or Discord
Make an incoming webhook in Slack or Discord (both are free). Then go to
**Settings > Secrets and variables > Actions > New repository secret**:
name `NOTIFY_WEBHOOK_URL`, value = the webhook URL.
You will get a message when new leads score 7 or more.

### 6. (Optional) Smarter scoring with AI
Add a secret named `ANTHROPIC_API_KEY`. The AI re-scores only the promising
posts. This is not free (small usage cost), so start without it.

---

## Using the dashboard
- Filter by score, type, source, or search words.
- Open a post, write a helpful reply, then press **replied**, **interested**,
  **won**, or **ignored**. Your statuses are saved in your browser only
  (not shared between devices).
- **Export CSV** downloads what you see.
- Rule of thumb: help first. Mention CleanSlate only if it truly fits.
  Never post the same pitch many times, because communities ban that.

## Run it on your own computer instead
```
pip install -r requirements.txt
python leadfinder.py demo        # test with fake posts
python leadfinder.py run         # real search (writes leads.csv, leads.html, leads.json)
```
To test the dashboard locally, run `python leadfinder.py run --db data/leads.db --out docs`,
then `python -m http.server -d docs 8000` and open http://localhost:8000

## Change what it searches
Open `leadfinder.py`. At the top you can edit `QUERIES`, `SUBREDDITS`, and the
word lists for scoring (`PAIN`, `INTENT`, `COMPETITORS`, `STARTUP`, `BUDGET`,
`ENTERPRISE`). After a week, look at which posts it got wrong and adjust.

## Known limits
- **Reddit may block GitHub's servers.** If the Actions log shows 403 errors
  for Reddit, you still get Stack Exchange and Hacker News. You can also run the
  script on your own computer for Reddit, or move to Reddit's official API
  (free key; check their commercial terms).
- Keyword scoring is simple. Expect some wrong matches at first.
- Check each site's terms before you turn this into a paid product for others.
