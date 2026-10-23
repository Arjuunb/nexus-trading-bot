# TradeLogX Supabase migrations

Apply `0001_saas_auth.sql` before turning on `HUB_AUTH_MODE=supabase`. It makes
Supabase Auth the identity authority, creates a profile automatically for every
new user, enforces per-user RLS, and retains an administrator-only audit log.

Then create and verify the first admin through the normal email flow. In the
Supabase SQL editor, run:

```sql
update public.tradexa_profiles set role = 'admin' where id = '<AUTH_USER_UUID>'::uuid;
```

For a deployment containing legacy rows, copy `0002_legacy_owner_backfill.sql`
to a temporary local file, set `first_admin`, review it, and execute it in one
transaction. Never commit the real UUID or any credential.

`SUPABASE_SERVICE_ROLE_KEY` is server-only and is used solely for account
deletion, audit writes, and bounded administrator user metadata. The public
browser uses only `SUPABASE_ANON_KEY`; RLS protects every table it can access.

## Customer preferences

Review and apply `0006_customer_workspaces.sql` once in the intended project
before using the customer workspace correction. This separate table stores
only `settings-center` metadata, with the verified user's UUID as owner. It
grants authenticated users SELECT/INSERT/UPDATE on their own rows and no
DELETE or anonymous access. A failed or missing migration returns a workspace
error; customer settings never fall back to shared SQLite or the legacy mirror.

The browser uses a user access token; no service key is needed for preference
reads or saves. Identity and email verification come from `/auth/me`, not the
preferences blob. These preferences do not configure the legacy trading engine.

`ops/staging.customer-workspace.nginx.conf` is a proposed private staging
overlay based on the reviewed account-flow gateway. Its only new endpoint is
GET/POST `/user/settings`. Validate the configuration with the pinned nginx
image before selecting the overlay. Profile mutations, worker startup, trading,
public exposure and production cutover are outside this overlay.

Before release acceptance, use two disposable users to verify actual RLS
isolation and save/reload behavior, then verify browser account switching and
session renewal against the newly built, scanned image. Local HTTP mocks and
controller tests do not establish those integration results.
