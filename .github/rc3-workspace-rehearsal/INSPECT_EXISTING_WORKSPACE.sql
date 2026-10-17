-- READ ONLY: inspect the existing object; never rerun or replace the migration.
-- Run only in the disposable project simgvasrjiyltnwtmyzt.
begin read only;
set local statement_timeout = '30s';
do $inspect$
declare target oid := to_regclass('public.tradexa_customer_workspaces');
        expected_role oid;
        expr text;
        item record;
begin
  if target is null then raise exception 'WORKSPACE_TABLE_MISSING'; end if;
  if not exists (select 1 from pg_class where oid=target and relkind='r'
                 and relrowsecurity and relforcerowsecurity) then
    raise exception 'WORKSPACE_TABLE_TYPE_OR_FORCED_RLS_MISMATCH';
  end if;
  if (select count(*) from pg_attribute where attrelid=target and attnum>0 and not attisdropped)<>3
     or (select count(*) from pg_attribute where attrelid=target and attnum>0 and not attisdropped
         and attnotnull and ((attname='user_id' and atttypid='uuid'::regtype)
          or (attname='namespace' and atttypid='text'::regtype)
          or (attname='data' and atttypid='jsonb'::regtype)))<>3 then
    raise exception 'WORKSPACE_COLUMNS_MISMATCH';
  end if;
  if (select count(*) from pg_attrdef where adrelid=target)<>1
     or not exists (select 1 from pg_attrdef d join pg_attribute a
        on a.attrelid=d.adrelid and a.attnum=d.adnum
        where d.adrelid=target and a.attname='data'
        and regexp_replace(pg_get_expr(d.adbin,d.adrelid),'[[:space:]()]','','g')='''{}''::jsonb') then
    raise exception 'WORKSPACE_DEFAULT_MISMATCH';
  end if;
  if (select count(*) from pg_constraint where conrelid=target and contype in ('p','f','c'))<>4
     or exists (select 1 from pg_constraint where conrelid=target and contype not in ('p','f','c','n'))
     or exists (select 1 from pg_constraint where conrelid=target
                and (not convalidated or condeferrable)) then
    raise exception 'WORKSPACE_CONSTRAINT_SET_MISMATCH';
  end if;
  if not exists (select 1 from pg_constraint k where k.conrelid=target and k.contype='p'
     and (select array_agg(a.attname::text order by p.ord)
          from unnest(k.conkey) with ordinality p(num,ord)
          join pg_attribute a on a.attrelid=target and a.attnum=p.num)=array['user_id','namespace']) then
    raise exception 'WORKSPACE_PRIMARY_KEY_MISMATCH';
  end if;
  if not exists (select 1 from pg_constraint k where k.conrelid=target and k.contype='f'
     and k.confrelid='auth.users'::regclass and k.confdeltype='c' and k.confupdtype='a'
     and k.conkey=array[(select attnum from pg_attribute where attrelid=target and attname='user_id')]::smallint[]
     and k.confkey=array[(select attnum from pg_attribute where attrelid='auth.users'::regclass and attname='id')]::smallint[]) then
    raise exception 'WORKSPACE_AUTH_OWNER_FOREIGN_KEY_MISMATCH';
  end if;
  if (select count(*) from pg_constraint where conrelid=target and contype='c'
      and regexp_replace(lower(pg_get_constraintdef(oid)),'[[:space:]()]','','g')
          in ('checknamespace=''settings-center''::text',
              'checkjsonb_typeofdata=''object''::textandoctet_lengthdata::text<=200000'))<>2 then
    raise exception 'WORKSPACE_NAMESPACE_OR_JSON_SIZE_CHECK_MISMATCH';
  end if;
  select oid into expected_role from pg_roles where rolname='authenticated' and not rolsuper and not rolbypassrls;
  if expected_role is null or (select count(*) from pg_policy where polrelid=target)<>3 then
    raise exception 'WORKSPACE_RUNTIME_ROLE_OR_POLICY_COUNT_MISMATCH';
  end if;
  for item in select * from pg_policy where polrelid=target loop
    if not item.polpermissive or item.polroles<>array[expected_role] then
      raise exception 'WORKSPACE_POLICY_ROLE_MISMATCH';
    end if;
    if (item.polname='tradexa_customer_workspaces_select' and item.polcmd='r'
         and item.polqual is not null and item.polwithcheck is null)
       or (item.polname='tradexa_customer_workspaces_insert' and item.polcmd='a'
         and item.polqual is null and item.polwithcheck is not null)
       or (item.polname='tradexa_customer_workspaces_update' and item.polcmd='w'
         and item.polqual is not null and item.polwithcheck is not null) then
      null;
    else raise exception 'WORKSPACE_POLICY_COMMAND_MISMATCH';
    end if;
    foreach expr in array array[pg_get_expr(item.polqual,target),pg_get_expr(item.polwithcheck,target)] loop
      if expr is not null and regexp_replace(lower(expr),'[[:space:]()]','','g')
          not in ('user_id=selectauth.uidasuid','user_id=selectauth.uid') then
        raise exception 'WORKSPACE_POLICY_OWNER_EXPRESSION_MISMATCH';
      end if;
    end loop;
  end loop;
  if has_table_privilege('anon',target,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
     or has_table_privilege('authenticated',target,'DELETE,TRUNCATE,REFERENCES,TRIGGER')
     or not has_table_privilege('authenticated',target,'SELECT')
     or not has_table_privilege('authenticated',target,'INSERT')
     or not has_table_privilege('authenticated',target,'UPDATE') then
    raise exception 'WORKSPACE_GRANTS_MISMATCH';
  end if;
  if exists (select 1 from pg_trigger where tgrelid=target and not tgisinternal)
     or exists (select 1 from pg_rewrite where ev_class=target) then
    raise exception 'UNREVIEWED_WORKSPACE_TRIGGER_OR_RULE';
  end if;
end
$inspect$;
select jsonb_build_object('status','WORKSPACE_SCHEMA_MATCHES_REVIEW',
 'forced_rls',true,'policies',3,'columns',3,
 'existing_row_count',(select count(*) from public.tradexa_customer_workspaces),
 'server_version',current_setting('server_version'),
 'owner',(select pg_get_userbyid(relowner) from pg_class where oid='public.tradexa_customer_workspaces'::regclass),
 'database_changes',false);
commit;
