-- ==========================================================================
-- Lead Finder database for Supabase
-- Paste this whole file into  Supabase > SQL Editor > New query  and click Run.
-- Run it once. It is safe to look at: every table has privacy rules (Row Level
-- Security) so a customer can only see their own products and leads.
-- ==========================================================================

-- --------------------------------------------------------------------------
-- 1. Products (one row per product a customer wants leads for)
-- --------------------------------------------------------------------------
create table public.projects (
  id              uuid primary key default gen_random_uuid(),
  owner_id        uuid not null default auth.uid() references auth.users(id) on delete cascade,
  name            text not null,
  description     text not null default '',      -- the problem it solves, in plain words
  context         text not null default '',      -- extra search word, for example: salesforce
  keywords        text[] not null,               -- 1 to 15 words or short phrases
  competitors     text[] not null default '{}',  -- up to 10
  exclude_words   text[] not null default '{}',  -- up to 10
  examples        text[] not null default '{}',  -- up to 3 posts the customer wishes they had found
  subreddits      text[] not null default '{}',  -- communities to search, up to 8, for example: restaurateur
  se_sites        text[] not null default '{}',  -- Stack Exchange sites, up to 4, for example: salesforce
  small_companies boolean not null default true, -- favour small companies and startups
  pack            text,                          -- starter pack used (information only)
  active          boolean not null default true, -- set by you (billing), not by the customer
  plan            text not null default 'free',  -- set by you, not by the customer
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index projects_owner_idx on public.projects(owner_id);

-- Checks that a normal CHECK constraint cannot do (they look inside lists)
create or replace function public.projects_validate() returns trigger
language plpgsql as $$
declare
  k text;
  n int;
begin
  new.name := btrim(new.name);
  if char_length(new.name) < 1 or char_length(new.name) > 80 then
    raise exception 'The product name must be 1 to 80 characters';
  end if;
  if char_length(new.description) > 600 then
    raise exception 'The description must be 600 characters or fewer';
  end if;
  if char_length(new.context) > 60 then
    raise exception 'The context word must be 60 characters or fewer';
  end if;

  -- clean the lists: trim spaces, drop empty items
  new.keywords      := array(select btrim(x) from unnest(new.keywords) x where btrim(x) <> '');
  new.competitors   := array(select btrim(x) from unnest(new.competitors) x where btrim(x) <> '');
  new.exclude_words := array(select btrim(x) from unnest(new.exclude_words) x where btrim(x) <> '');
  new.examples      := array(select btrim(x) from unnest(new.examples) x where btrim(x) <> '');
  new.subreddits    := array(select regexp_replace(btrim(x), '^/?r/', '', 'i') from unnest(new.subreddits) x where btrim(x) <> '');
  new.se_sites      := array(select lower(btrim(x)) from unnest(new.se_sites) x where btrim(x) <> '');

  if coalesce(cardinality(new.keywords), 0) < 1 or cardinality(new.keywords) > 15 then
    raise exception 'Use between 1 and 15 keywords';
  end if;
  if cardinality(new.competitors) > 10 then
    raise exception 'Use at most 10 competitors';
  end if;
  if cardinality(new.exclude_words) > 10 then
    raise exception 'Use at most 10 words to avoid';
  end if;
  if cardinality(new.examples) > 3 then
    raise exception 'Use at most 3 example posts';
  end if;

  if cardinality(new.subreddits) > 8 then
    raise exception 'Use at most 8 communities';
  end if;
  if cardinality(new.se_sites) > 4 then
    raise exception 'Use at most 4 Stack Exchange sites';
  end if;
  foreach k in array new.subreddits loop
    if k !~ '^[A-Za-z0-9_]{2,30}$' then
      raise exception 'Community names can only use letters, numbers and underscores (2 to 30 characters)';
    end if;
  end loop;
  foreach k in array new.se_sites loop
    if k !~ '^[a-z0-9.\-]{2,40}$' then
      raise exception 'Stack Exchange site names can only use letters, numbers, dots and dashes';
    end if;
  end loop;

  foreach k in array new.keywords loop
    if char_length(k) > 60 then raise exception 'Each keyword must be 60 characters or fewer'; end if;
  end loop;
  foreach k in array new.competitors loop
    if char_length(k) > 60 then raise exception 'Each competitor name must be 60 characters or fewer'; end if;
  end loop;
  foreach k in array new.exclude_words loop
    if char_length(k) > 60 then raise exception 'Each word to avoid must be 60 characters or fewer'; end if;
  end loop;
  foreach k in array new.examples loop
    if char_length(k) > 600 then raise exception 'Each example post must be 600 characters or fewer'; end if;
  end loop;

  if tg_op = 'INSERT' then
    select count(*) into n from public.projects where owner_id = new.owner_id;
    if n >= 3 then
      raise exception 'The free plan allows up to 3 products';
    end if;
  end if;

  new.updated_at := now();
  return new;
end $$;

create trigger projects_validate_trg
  before insert or update on public.projects
  for each row execute function public.projects_validate();

-- --------------------------------------------------------------------------
-- 2. Posts found in public communities (shared by everyone, filled by the daily job)
-- --------------------------------------------------------------------------
create table public.raw_posts (
  url        text primary key,
  source     text not null,
  title      text,
  body       text,
  author     text,
  created_at timestamptz,
  first_seen timestamptz not null default now()
);
create index raw_posts_created_idx on public.raw_posts(created_at);

-- --------------------------------------------------------------------------
-- 3. Which post matched which product, and how well (filled by the daily job)
-- --------------------------------------------------------------------------
create table public.matches (
  project_id uuid not null references public.projects(id) on delete cascade,
  url        text not null references public.raw_posts(url) on delete cascade,
  score      int  not null,
  intent     text,
  reason     text,
  angle      text,           -- suggested helpful way to reply (optional, needs AI key)
  scored_by  text not null default 'rules',
  first_seen timestamptz not null default now(),
  primary key (project_id, url)
);
create index matches_url_idx on public.matches(url);

-- --------------------------------------------------------------------------
-- 4. What the customer did with each lead (status and thumbs up/down).
--    Kept even after the post is removed, so "what's working" stays accurate.
-- --------------------------------------------------------------------------
create table public.lead_status (
  project_id uuid not null references public.projects(id) on delete cascade,
  url        text not null,
  status     text check (status in ('replied', 'interested', 'won', 'ignored')),
  feedback   text check (feedback in ('good', 'bad')),
  source     text check (char_length(source) <= 120),
  title      text check (char_length(title) <= 300),
  intent     text check (char_length(intent) <= 40),
  score      int,
  updated_at timestamptz not null default now(),
  primary key (project_id, url)
);
create index lead_status_project_idx on public.lead_status(project_id);

-- --------------------------------------------------------------------------
-- 5. One row with the time of the last daily run
-- --------------------------------------------------------------------------
create table public.app_status (
  id       int primary key default 1 check (id = 1),
  last_run timestamptz,
  note     text
);
insert into public.app_status(id) values (1);

-- --------------------------------------------------------------------------
-- 6. What the website reads: leads for the logged-in customer only
-- --------------------------------------------------------------------------
create view public.my_leads with (security_invoker = true) as
select m.project_id, m.url, m.score, m.intent, m.reason, m.angle,
       m.first_seen as matched_at,
       r.source, r.title, r.author, r.created_at as posted_at,
       left(r.body, 400) as snippet,
       s.status, s.feedback
from public.matches m
join public.raw_posts r on r.url = m.url
left join public.lead_status s on s.project_id = m.project_id and s.url = m.url;

-- ==========================================================================
-- PRIVACY RULES (Row Level Security): turn it on everywhere, then allow only
-- what each customer needs.
-- ==========================================================================
alter table public.projects    enable row level security;
alter table public.raw_posts   enable row level security;
alter table public.matches     enable row level security;
alter table public.lead_status enable row level security;
alter table public.app_status  enable row level security;

create policy "own products: read"   on public.projects for select to authenticated
  using (owner_id = auth.uid());
create policy "own products: add"    on public.projects for insert to authenticated
  with check (owner_id = auth.uid());
create policy "own products: change" on public.projects for update to authenticated
  using (owner_id = auth.uid()) with check (owner_id = auth.uid());
create policy "own products: delete" on public.projects for delete to authenticated
  using (owner_id = auth.uid());

create policy "own matches: read" on public.matches for select to authenticated
  using (exists (select 1 from public.projects p
                 where p.id = matches.project_id and p.owner_id = auth.uid()));

create policy "posts matched to my products: read" on public.raw_posts for select to authenticated
  using (exists (select 1 from public.matches m
                 join public.projects p on p.id = m.project_id
                 where m.url = raw_posts.url and p.owner_id = auth.uid()));

create policy "own lead status: all" on public.lead_status for all to authenticated
  using (exists (select 1 from public.projects p
                 where p.id = lead_status.project_id and p.owner_id = auth.uid()))
  with check (exists (select 1 from public.projects p
                      where p.id = lead_status.project_id and p.owner_id = auth.uid()));

create policy "everyone logged in: read last run" on public.app_status for select to authenticated
  using (true);

-- ==========================================================================
-- PERMISSIONS: start from nothing, then give back only what is needed.
-- (The daily job uses the service key, which can do everything.)
-- ==========================================================================
revoke all on public.projects, public.raw_posts, public.matches,
              public.lead_status, public.app_status, public.my_leads
  from anon, authenticated;

grant select, delete on public.projects to authenticated;
-- customers may NOT set owner_id, active or plan (those are yours)
grant insert (name, description, context, keywords, competitors, exclude_words, examples,
              subreddits, se_sites, small_companies, pack) on public.projects to authenticated;
grant update (name, description, context, keywords, competitors, exclude_words, examples,
              subreddits, se_sites, small_companies, pack) on public.projects to authenticated;

grant select on public.raw_posts, public.matches, public.app_status, public.my_leads to authenticated;
grant select, insert, update, delete on public.lead_status to authenticated;
