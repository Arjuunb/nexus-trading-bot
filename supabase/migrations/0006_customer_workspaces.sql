-- Dedicated customer metadata; no changes to legacy settings or financial data.
-- Apply only after reviewing the target project. Runtime never executes SQL.
begin;

create table public.tradexa_customer_workspaces (
  user_id uuid not null references auth.users(id) on delete cascade,
  namespace text not null check (namespace = 'settings-center'),
  data jsonb not null default '{}'::jsonb
    check (jsonb_typeof(data) = 'object' and octet_length(data::text) <= 200000),
  primary key (user_id, namespace)
);

alter table public.tradexa_customer_workspaces enable row level security;
alter table public.tradexa_customer_workspaces force row level security;

revoke all on public.tradexa_customer_workspaces from public, anon, authenticated;
grant select, insert, update on public.tradexa_customer_workspaces to authenticated;

create policy tradexa_customer_workspaces_select
  on public.tradexa_customer_workspaces for select to authenticated
  using (user_id = (select auth.uid()));
create policy tradexa_customer_workspaces_insert
  on public.tradexa_customer_workspaces for insert to authenticated
  with check (user_id = (select auth.uid()));
create policy tradexa_customer_workspaces_update
  on public.tradexa_customer_workspaces for update to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

commit;
