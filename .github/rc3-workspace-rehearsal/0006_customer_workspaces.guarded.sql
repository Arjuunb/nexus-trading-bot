-- Target must first be independently verified as the disposable staging project.
-- No persistent object is replaced; an existing workspace table aborts this batch.
begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';
do $preflight$
begin
  if to_regclass('auth.users') is null or to_regclass('public.tradexa_profiles') is null
     or to_regprocedure('auth.uid()') is null then
    raise exception 'EXPECTED_STAGING_AUTH_SCHEMA_REQUIRED';
  end if;
  if to_regclass('public.tradexa_customer_workspaces') is not null then
    raise exception 'WORKSPACE_ALREADY_EXISTS_STOP_AND_INSPECT';
  end if;
  if (select count(*) from pg_roles where rolname in ('anon','authenticated')) <> 2
     or exists (select 1 from pg_roles where rolname in ('anon','authenticated')
                and (rolsuper or rolbypassrls)) then
    raise exception 'EXPECTED_NONBYPASS_RUNTIME_ROLES_REQUIRED';
  end if;
end
$preflight$;

-- Snapshot schema metadata only; never read customer/profile/financial row values.
create temporary view rc3_workspace_existing_public_catalog as
select c.oid, c.relname, c.relkind, c.relowner, c.relacl::text,
       c.relrowsecurity, c.relforcerowsecurity,
       (select coalesce(jsonb_agg(jsonb_build_object(
          'name',a.attname,'type',format_type(a.atttypid,a.atttypmod),
          'notnull',a.attnotnull,'default',pg_get_expr(d.adbin,d.adrelid)) order by a.attnum),'[]'::jsonb)
        from pg_attribute a left join pg_attrdef d on d.adrelid=a.attrelid and d.adnum=a.attnum
        where a.attrelid=c.oid and a.attnum>0 and not a.attisdropped) as columns,
       (select coalesce(jsonb_agg(jsonb_build_object('name',p.polname,'command',p.polcmd,
          'roles',p.polroles,'using',pg_get_expr(p.polqual,p.polrelid),
          'check',pg_get_expr(p.polwithcheck,p.polrelid)) order by p.polname),'[]'::jsonb)
        from pg_policy p where p.polrelid=c.oid) as policies,
       (select coalesce(jsonb_agg(jsonb_build_object('name',k.conname,
          'definition',pg_get_constraintdef(k.oid)) order by k.conname),'[]'::jsonb)
        from pg_constraint k where k.conrelid=c.oid) as constraints
from pg_class c join pg_namespace n on n.oid=c.relnamespace
where n.nspname='public' and c.relkind in ('r','p','v','m','f','S')
  and c.relname <> 'tradexa_customer_workspaces';
create temporary table rc3_workspace_before on commit drop as
select coalesce(jsonb_agg(to_jsonb(x) order by x.oid),'[]'::jsonb) as catalog
from rc3_workspace_existing_public_catalog x;

-- BEGIN exact source migration body; only original transaction markers removed.

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

-- END exact source migration body.

do $verify$
declare before_catalog jsonb; after_catalog jsonb;
begin
  select catalog into before_catalog from rc3_workspace_before;
  select coalesce(jsonb_agg(to_jsonb(x) order by x.oid),'[]'::jsonb)
    into after_catalog from rc3_workspace_existing_public_catalog x;
  if before_catalog is distinct from after_catalog then
    raise exception 'EXISTING_PUBLIC_SCHEMA_CHANGED_ROLLBACK';
  end if;
  if not exists (select 1 from pg_class where oid='public.tradexa_customer_workspaces'::regclass
                 and relrowsecurity and relforcerowsecurity) then
    raise exception 'FORCED_ROW_SECURITY_REQUIRED';
  end if;
  if (select count(*) from public.tradexa_customer_workspaces) <> 0 then
    raise exception 'NEW_TABLE_MUST_BE_EMPTY';
  end if;
  if (select count(*) from pg_policies where schemaname='public'
       and tablename='tradexa_customer_workspaces') <> 3
     or (select count(*) from pg_policies where schemaname='public'
       and tablename='tradexa_customer_workspaces' and roles='{authenticated}'::name[]
       and cmd in ('SELECT','INSERT','UPDATE')) <> 3 then
    raise exception 'EXACT_THREE_CUSTOMER_POLICIES_REQUIRED';
  end if;
  if has_table_privilege('anon','public.tradexa_customer_workspaces','SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
     or has_table_privilege('authenticated','public.tradexa_customer_workspaces','DELETE,TRUNCATE')
     or not has_table_privilege('authenticated','public.tradexa_customer_workspaces','SELECT')
     or not has_table_privilege('authenticated','public.tradexa_customer_workspaces','INSERT')
     or not has_table_privilege('authenticated','public.tradexa_customer_workspaces','UPDATE') then
    raise exception 'CUSTOMER_OR_ANONYMOUS_GRANTS_INVALID';
  end if;
end
$verify$;
notify pgrst, 'reload schema';
commit;
select jsonb_build_object('status','CUSTOMER_WORKSPACE_SCHEMA_COMMITTED',
                         'table','tradexa_customer_workspaces','initial_rows',0);
