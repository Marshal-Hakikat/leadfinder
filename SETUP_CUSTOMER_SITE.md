# Customer website: setup (about 45 minutes, all free to start)

What you get: a website where people sign up, add a product + keywords (max 15),
and see only their own leads. A daily job finds the posts.

Parts: **Supabase** (logins + database), **Cloudflare Pages** (the website),
**GitHub Actions** (the daily search). Prices and free limits change, so check
each site's pricing page before you sell.

## 1. Supabase (database and logins)
1. Go to supabase.com, sign up, click **New project**. Choose a name, a strong
   database password (save it), and a region near your customers.
2. Left menu: **SQL Editor > New query**. Open `supabase/schema.sql`, copy everything,
   paste, click **Run**. You should see "Success".
3. Left menu: **Project Settings > API**. You will see three things:
   - **Project URL**
   - **anon public key** (safe to put in the website)
   - **service_role key** (SECRET: only for GitHub Secrets, never in the website)

## 2. The website (Cloudflare Pages)
1. Open `web/config.js`. Replace `SUPABASE_URL` and `SUPABASE_ANON_KEY` with the
   Project URL and the **anon** key. Save.
2. Go to cloudflare.com, sign up. **Workers & Pages > Create > Pages > Upload assets**.
   Give a name, drag the whole `web` folder in, click **Deploy**.
3. Copy your site address (for example `https://leadfinder.pages.dev`).
4. Back in Supabase: **Authentication > URL Configuration**. Set **Site URL** to
   your site address. Add the same address under **Redirect URLs**.
(To update the website later, upload the `web` folder again.)

## 3. The daily search (GitHub)
1. In your repository: upload `cloud_worker.py`, the updated `leadfinder.py`,
   and `.github/workflows/cloud.yml` (create the file by typing the path, like before).
2. **Settings > Secrets and variables > Actions > New repository secret**. Add:
   - `SUPABASE_URL` = the Project URL
   - `SUPABASE_SERVICE_KEY` = the **service_role** key
   - `LEADFINDER_USER_AGENT` = `LeadFinder/1.0 (contact: your email)`
   - optional `REDDITAPIS_KEY` = your key from redditapis.com (free: 1,000 reads a month).
     The job uses at most 30 calls per run, about 900 a month. Change with a secret
     `REDDITAPIS_MAX_CALLS`. If the key is missing, it tries Reddit directly (blocked on GitHub).
   - optional `SE_KEY` = free key from stackapps.com (more Stack Exchange searches)
   - optional `ANTHROPIC_API_KEY` = turns on "Reply idea" text (small cost)
3. **Actions > Find leads for customers > Run workflow**.

## 4. Test
1. Open your site, **Create an account**, confirm the email, sign in.
2. **My products > Add a product**, choose a ready-made set (for example
   "Restaurants" for Tiphaus), Save.
3. Run the workflow again (it only searches products that exist).
4. Open **Leads**. Press 👍/👎 and the status buttons. **What's working** fills up.

## Things to know before real customers
- **Reddit:** GitHub's servers are blocked by Reddit, so use the `REDDITAPIS_KEY` above. Check redditapis.com's terms before you charge customers.
- **Lemmy** is searched too (free, no key).
- (Old note) GitHub's servers are blocked by Reddit. Restaurants and many other
  topics live mostly on Reddit, so those products will have few leads until you
  get Reddit's API approval or another way to read Reddit. Stack Exchange and
  Hacker News work.
- **Sign-up emails:** Supabase's built-in email sender is limited. Before
  customers arrive, add your own sender in **Authentication > SMTP** (for example Resend).
- **Free database sleeps** if nobody uses it for a while; the daily job counts as use.
- **Rules to publish:** a privacy page and terms; tell customers you read only
  public posts and never post for them. A human writes every reply.
- **Not built yet:** payments (Stripe or Lemon Squeezy), email alerts, plans.
  The `plan` and `active` columns are ready for them, and customers cannot edit them.
- **Free AI (Hugging Face / Hetzner):** not needed now. Add later if Reply ideas
  become popular.
