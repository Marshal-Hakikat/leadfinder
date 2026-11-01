-- ==========================================================================
-- Lead Finder: AI keyword helper + weekly summary
-- Paste this whole file into  Supabase > SQL Editor > New query  and click Run.
-- Safe to run more than once.
-- ==========================================================================

-- --------------------------------------------------------------------------
-- 1. Keyword helper requests
--    The website adds a row (status 'pending'); a GitHub job asks the free AI
--    and writes the suggestions into "result" (status 'done').
-- --------------------------------------------------------------------------
create table if not exists public.keyword_suggestions (
  id          uuid primary key default gen_random_uuid(),
  owner_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  name        text not null check (char_length(name) between 1 and 80),
  description text not null check (char_length(description) between 20 and 600),
  context     text not null default '' check (char_length(context) <= 60),
  status      text not null default 'pending' check (status in ('pending', 'done', 'failed')),
  result      jsonb,
  created_at  timestamptz not null default now(),
  done_at     timestamptz
);
create index if not exists keyword_suggestions_pending_idx on public.keyword_suggestions(status) where status = 'pending';
create index if not exists keyword_suggestions_owner_idx on public.keyword_suggestions(owner_id, created_at desc);

-- At most 10 requests per person per day, and customers cannot fake a result
create or replace function public.keyword_suggestions_guard() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if (select count(*) from public.keyword_suggestions
      where owner_id = new.owner_id and created_at > now() - interval '1 day') >= 10 then
    raise exception 'You can ask for 10 suggestions a day. Please try again tomorrow.';
  end if;
  new.status := 'pending';
  new.result := null;
  new.done_at := null;
  new.created_at := now();
  return new;
end $$;
drop trigger if exists keyword_suggestions_guard on public.keyword_suggestions;
create trigger keyword_suggestions_guard before insert on public.keyword_suggestions
  for each row execute function public.keyword_suggestions_guard();

alter table public.keyword_suggestions enable row level security;
drop policy if exists "own suggestions: read" on public.keyword_suggestions;
create policy "own suggestions: read" on public.keyword_suggestions for select to authenticated
  using (owner_id = auth.uid());
drop policy if exists "own suggestions: ask" on public.keyword_suggestions;
create policy "own suggestions: ask" on public.keyword_suggestions for insert to authenticated
  with check (owner_id = auth.uid());

revoke all on public.keyword_suggestions from anon, authenticated;
grant select on public.keyword_suggestions to authenticated;
grant insert (name, description, context) on public.keyword_suggestions to authenticated;

-- --------------------------------------------------------------------------
-- 2. Weekly summary (one row per product per week, written by the daily job)
-- --------------------------------------------------------------------------
create table if not exists public.weekly_summaries (
  project_id uuid not null references public.projects(id) on delete cascade,
  week_start date not null,
  stats      jsonb not null default '{}'::jsonb,
  summary    text,
  created_at timestamptz not null default now(),
  primary key (project_id, week_start)
);

alter table public.weekly_summaries enable row level security;
drop policy if exists "own summaries: read" on public.weekly_summaries;
create policy "own summaries: read" on public.weekly_summaries for select to authenticated
  using (exists (select 1 from public.projects p
                 where p.id = weekly_summaries.project_id and p.owner_id = auth.uid()));

revoke all on public.weekly_summaries from anon, authenticated;
grant select on public.weekly_summaries to authenticated;
