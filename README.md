# Lead Finder

A free tool that finds public conversations showing that people need a product,
scores them, and shows them on a website. It supports **more than one product**:
each product has its own settings file in `projects/`.

First product: CleanSlate (duplicate lead management in Salesforce), aimed at
small companies and startups.

**How it works**
1. GitHub Actions runs `leadfinder.py` every day (free).
2. It reads every settings file in `projects/`, collects posts once from the
   communities they name (Reddit, Stack Exchange, Hacker News), and scores each
   post separately for each product.
3. It writes the website data into `docs/`, and GitHub Pages shows it.

## Add a product (about 5 minutes)
1. Open `projects/_example_territory.json` and copy its contents.
2. Create a new file `projects/yourproduct.json` (no underscore at the start)
   and paste. The file name becomes the product's web address name.
3. Edit it:
   - `name` and `description`: what the product does and who it is for.
   - `keywords`: up to **15** words or short phrases people use when they have
     the problem (extra ones are ignored).
   - `competitors`: names of rival products (a post that mentions one scores high).
   - `examples`: 1 to 3 real posts you would have loved to find (used by the
     optional AI scoring).
   - `sources`: which subreddits, Stack Exchange sites and Hacker News searches to use.
4. Commit the file. The next run (or **Actions > Find leads > Run workflow**)
   includes it.

To pause a product, rename its file so it starts with an underscore.

### Give a customer their own page
Add `"listed": false` to their settings file. Their leads then live at
`https://YOUR-NAME.github.io/leadfinder/?p=their-file-name`
and they do not appear in the product list on the main page. Use a hard-to-guess
file name, for example `acme-7f3k9`.

> **Important: this repository is public.** Anyone who finds the link can see that
> page, and the settings files (keywords and descriptions) are visible in the
> repository. That is fine for your own product and for friendly test users who
> agree to it. For paying customers you need logins and private data. See
> "Next version" below.

## Setup (first time)
1. Make a public GitHub repository and upload all files (keep the folders:
   `.github/workflows`, `docs`, `projects`, `data`).
2. In `leadfinder.py`, replace `you@example.com` in `USER_AGENT` with your email
   (sites ask every tool to say who runs it).
3. **Settings > Actions > General > Workflow permissions**: Read and write.
4. **Settings > Pages**: Deploy from a branch, `main`, folder `/docs`.
5. **Actions > Find leads > Run workflow**. Then open your site.

Optional alerts: add a GitHub Secret `NOTIFY_WEBHOOK_URL` (Slack or Discord
webhook) to get a message when new leads score 7 or more. Optional AI scoring:
add a secret `ANTHROPIC_API_KEY` (this costs a little).

## Using the dashboard
- Filter by score, type, source, or search words. Switch product at the top (if
  more than one is listed).
- Open a post, reply helpfully, then press **replied**, **interested**, **won**
  or **ignored**. Statuses are saved in your browser only.
- **Export CSV** downloads what you see.
- Rule: help first. Mention your product only if it fits. Never copy-paste the
  same pitch into many posts.

## What the scoring does
Each post gets 0 to 10 for each product. It goes up for: your keywords in the
title, someone asking for a tool, mentioning a competitor, small-company words,
price words, and recent posts. It goes down for: exam or learning posts, code
questions, enterprise words, and age (older than 1 month loses points, older
than about 3 months is dropped).

## Run it on your own computer
```
pip install -r requirements.txt
python leadfinder.py demo          # test with sample posts for every product
python leadfinder.py run           # real search, writes into the current folder
python leadfinder.py run --project cleanslate --out docs --db data/leads.db
```

## Known limits
- **Reddit blocks GitHub's servers** (403). The tool stops Reddit after 3 blocked
  requests and keeps going with the other sources. Reddit works from a home
  computer, or with Reddit's official API (needs Reddit's approval).
- Stack Exchange and Hacker News have few recent posts on many topics.
- Keyword scoring is simple. Adjust `keywords`, `pain_patterns` and
  `noise_patterns` after you see real results.
- Check each site's terms before you sell this as a service.

## Customer website (built)
See `SETUP_CUSTOMER_SITE.md`: Supabase logins + database, `web/` site, `cloud_worker.py` daily job.
The older `docs/` dashboard and `projects/*.json` files still work for your own products.

## Old note: what a customer version needs
Customers sign up, type their words (up to 15) into a form, and see only their own
leads behind a login. That needs a small database with logins (for example
Supabase's free tier) and a website host that allows commercial use, and the daily
job reads each customer's settings from the database instead of files.
