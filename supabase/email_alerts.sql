alter table public.projects add column if not exists email_alerts boolean not null default true;
alter table public.matches add column if not exists emailed_at timestamptz;
create index if not exists matches_not_emailed_idx on public.matches(project_id) where emailed_at is null;

grant insert (email_alerts) on public.projects to authenticated;
grant update (email_alerts) on public.projects to authenticated;
