-- O Diário Virtual: tabela de preferências por usuário.
-- Rode no Supabase: SQL Editor > New query > cole tudo > Run.

create table if not exists public.user_settings (
  user_id    uuid primary key references auth.users (id) on delete cascade,
  data       jsonb not null default '{}'::jsonb,   -- interesses, veículos, tema, voz (e chaves, se o usuário ativar)
  updated_at timestamptz not null default now()
);

-- Segurança: cada usuário enxerga e altera somente a própria linha.
alter table public.user_settings enable row level security;

drop policy if exists "settings_select_own" on public.user_settings;
drop policy if exists "settings_insert_own" on public.user_settings;
drop policy if exists "settings_update_own" on public.user_settings;
drop policy if exists "settings_delete_own" on public.user_settings;

create policy "settings_select_own" on public.user_settings
  for select to authenticated using ((select auth.uid()) = user_id);
create policy "settings_insert_own" on public.user_settings
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy "settings_update_own" on public.user_settings
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy "settings_delete_own" on public.user_settings
  for delete to authenticated using ((select auth.uid()) = user_id);
