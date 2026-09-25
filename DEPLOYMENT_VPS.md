# Ubuntu 24.04 / Hostinger VPS deployment

Install Docker Engine and the Docker Compose plugin on the VPS, then copy this
working directory to the server.  No cloud platform configuration is required.

1. `cp .env.example .env`
2. Replace every `CHANGE_ME` value with a unique random secret (for example,
   `openssl rand -hex 32`). Set `HUB_CORS_ORIGINS` to the final public origin.
3. Run `docker compose up -d --build`.
4. Verify `docker compose ps` reports both services healthy/running and run
   `./scripts/healthcheck.sh`.

The `tradexa-data` named volume is the durable state store. Back it up before
upgrades with `docker run --rm -v tradexa-trading-bot-production_tradexa-data:/data -v "$PWD":/backup alpine tar czf /backup/tradexa-data.tgz /data`.

## Production HTTPS: `trade-logx.com` and `www.trade-logx.com`

The Compose deployment includes Nginx, Certbot, a persistent `letsencrypt`
volume, and a persistent shared ACME webroot. Certificates and private keys
never enter the repository or the application container.

Run these commands on the VPS after pulling the HTTPS changes:

```sh
cd /opt/VPS-productn
git pull --ff-only origin main
cp -n .env.example .env
nano .env
# Set HUB_CORS_ORIGINS=https://trade-logx.com,https://www.trade-logx.com
# Replace every CHANGE_ME value before continuing.

docker compose config
docker compose up -d --build app nginx certbot
docker compose ps
```

Before requesting the certificate, verify that both DNS records resolve to
`2.24.141.144` and that port 80 reaches this VPS. The initial Nginx config
continues proxying normal HTTP traffic and serves the ACME path:

```sh
docker compose run --rm --entrypoint sh certbot -c \
  'mkdir -p /var/www/certbot/.well-known/acme-challenge && printf acme-ok > /var/www/certbot/.well-known/acme-challenge/health'
curl -fsS http://trade-logx.com/.well-known/acme-challenge/health
curl -fsS http://www.trade-logx.com/.well-known/acme-challenge/health
```

The two requests must print `acme-ok`. Then issue the first certificate (use an
email address you control):

```sh
docker compose run --rm --entrypoint certbot certbot certonly --webroot \
  -w /var/www/certbot \
  -d trade-logx.com -d www.trade-logx.com \
  --email YOUR_EMAIL@example.com --agree-tos --no-eff-email

# Switch immediately to the HTTPS config; the watcher also reloads after future renewals.
docker compose restart nginx
```

Validate the completed deployment:

```sh
docker compose config
docker compose ps
curl -fsSI https://trade-logx.com/
curl -fsSI https://www.trade-logx.com/
curl -sSI http://trade-logx.com/ | grep -Ei 'HTTP/|location:'
curl -sSI http://www.trade-logx.com/ | grep -Ei 'HTTP/|location:'
echo | openssl s_client -connect trade-logx.com:443 -servername trade-logx.com 2>/dev/null | openssl x509 -noout -issuer -subject -dates
docker compose logs --tail=100 nginx certbot app
```

### Optional: the public API at api.trade-logx.com

The public `/v1` API is always reachable at `https://trade-logx.com/v1/`. To
also serve it on its own name (TLS 1.3 only, and nothing but `/v1/` and
`/status` reachable there):

1. Add a DNS `A` record for `api.trade-logx.com` pointing at `2.24.141.144`.
2. Issue its certificate through the same webroot:

```sh
docker compose run --rm --entrypoint certbot certbot certonly --webroot \
  -w /var/www/certbot -d api.trade-logx.com \
  --email YOUR_EMAIL@example.com --agree-tos --no-eff-email
docker compose restart nginx
curl -fsS https://api.trade-logx.com/status | head -c 200; echo
```

Until that certificate exists the API name is simply not served; Nginx never
fails to start over it, and renewals are picked up by the same watcher.

Expected results: both HTTPS URLs return a successful response; both HTTP URLs
return `301` with an `https://` location; certificate dates show a valid
Let's Encrypt certificate; and `app` is healthy while `nginx` is healthy/running.
The Certbot container runs `renew` every 12 hours. Nginx detects an updated
certificate and reloads within 60 seconds. Test renewal without consuming a
certificate issuance limit:

```sh
docker compose run --rm --entrypoint certbot certbot renew --dry-run --webroot -w /var/www/certbot
```

Useful operations:

```sh
docker compose logs -f --tail=200
docker compose restart app
docker compose exec app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').read())"
```

The compose configuration uses `restart: unless-stopped`, bounded local logs,
health checks, a non-root application user, persistent trading and certificate
volumes, an internal-only FastAPI port, and only Nginx publishes ports 80/443.

## Rotating the master key

`HUB_MASTER_KEY` protects the per-tenant data keys, which in turn seal exchange
keys and webhook signing secrets; backups are sealed with it directly. To
replace it (for example after it may have been exposed):

```bash
cd /opt/nexus-trading-bot
NEW=$(docker compose exec -T app python -m services.key_vault new-key)
docker compose exec -T -e HUB_MASTER_KEY_NEW="$NEW" app python -m services.key_vault rewrap
OLD=$(grep '^HUB_MASTER_KEY=' .env | cut -d= -f2-)
sed -i '/^HUB_MASTER_KEY_PREVIOUS=/d; /^HUB_MASTER_KEY=/d' .env
printf 'HUB_MASTER_KEY_PREVIOUS=%s\nHUB_MASTER_KEY=%s\n' "$OLD" "$NEW" >> .env
bash scripts/deploy.sh
```

`rewrap` re-encrypts only the data keys, so exchange keys and webhook secrets
keep working unchanged. Between `rewrap` and the redeploy the running app still
holds the old key and cannot read the vault; run the steps together. Backups
taken before the rotation restore with `HUB_MASTER_KEY_PREVIOUS`; once they have
aged out (seven days), remove that line. Keep a copy of the new key off the
server. Settings → Security → Security checkup confirms the result.

## Supabase customer authentication (required before production use)

TradeLogX customer sign-up, passwords, email confirmation, password reset,
Google/Apple OAuth, and refresh-token rotation are handled by Supabase Auth.
`HUB_USERNAME` and `HUB_PASSWORD` are **not** customer credentials; they are
only retained for a deliberately disabled emergency recovery mode.

1. Create a Supabase project and set its Auth Site URL to
   `https://trade-logx.com`. Add these redirect URLs:

   ```text
   https://trade-logx.com/auth/verify-email
   https://trade-logx.com/auth/reset-password
   https://www.trade-logx.com/auth/verify-email
   https://www.trade-logx.com/auth/reset-password
   ```

2. In Supabase Auth, enable **Confirm email**. Configure SMTP before inviting
   real users. Enable Google and/or Apple only after adding their provider
   credentials in Supabase; do not place provider client secrets in `.env`.

3. Apply the tracked SQL in the Supabase SQL editor, in this order:

   ```sh
   # From your checkout, copy the text of this file into Supabase SQL Editor:
   less supabase/migrations/0001_saas_auth.sql
   ```

4. Create the first administrator using the normal `/auth/register` page,
   verify that email, then promote the copied Auth user UUID in Supabase SQL
   editor:

   ```sql
   update public.tradexa_profiles
   set role = 'admin'
   where id = '<AUTH_USER_UUID>'::uuid;
   ```

5. If this VPS has legacy trade rows, make a backup first, then copy
   `supabase/migrations/0002_legacy_owner_backfill.sql` outside Git, replace
   the `first_admin` placeholder with that UUID, review it, and run it once.
   It assigns only rows with a null `user_id`; it never overwrites a user owner.

6. Set these values in `/opt/VPS-productn/.env` and rebuild:

   ```dotenv
   HUB_AUTH_MODE=supabase
   SUPABASE_URL=https://YOUR_PROJECT.supabase.co
   SUPABASE_ANON_KEY=YOUR_PUBLIC_ANON_KEY
   SUPABASE_SERVICE_ROLE_KEY=YOUR_SERVER_ONLY_SERVICE_ROLE_KEY
   SUPABASE_KEY=YOUR_SERVER_ONLY_SERVICE_ROLE_KEY
   HUB_AUTH_GOOGLE_ENABLED=0
   HUB_AUTH_APPLE_ENABLED=0
   HUB_EMERGENCY_ADMIN_ENABLED=0
   ```

   Set an OAuth flag to `1` only after its Supabase provider is configured.
   The URL and anon key are injected at runtime into the frontend; the service
   role key remains server-only and must never be prefixed with `VITE_`.

7. Deploy and validate the auth boundary:

   ```sh
   cd /opt/VPS-productn
   docker compose config
   docker compose up -d --build
   docker compose ps
   curl -fsS https://trade-logx.com/health
   curl -sS https://trade-logx.com/auth/status
   docker compose logs --tail=150 app | grep -Ei 'traceback|exception|refusing' && exit 1 || true
   ```

   In a private browser window, register an email, confirm it, sign in, verify
   `/app` loads, update a profile, sign out, reset the password, and sign in
   again. Verify that a second user sees no rows belonging to the first user.

Never commit `.env`, Supabase keys, SMTP passwords, OAuth private keys, or
certificate material. A production startup fails closed if Supabase Auth is
selected without its URL and anon key.

`.env` reaches the container only through compose's `env_file`; `.dockerignore`
keeps every `.env` file out of the image. Images built before that rule existed
copied `.env` into a layer, so after the first deploy that includes it remove
the old images (`docker image prune -a` once the new containers are healthy).
If such an image was ever pushed to a registry or copied off the server,
rotate the secrets it contained.

## Migrate existing VPS SQLite history to Supabase

Do this only after the Supabase Auth SQL above is working and before restarting
an existing VPS into an empty Supabase ledger. The migration is idempotent and
never deletes local SQLite data, but a backup is still mandatory.

1. In the Supabase SQL editor, run the full contents of these files in this
   order (the first creates the ledger/settings tables; the second safely adds
   the Auth ownership/RLS policy to newly-created tables):

   ```sh
   sed -n '1,999p' automation-hub/data/ledger_schema.sql
   sed -n '1,999p' supabase/migrations/0001_saas_auth.sql
   ```

2. Verify the server-only key wiring without printing any secrets:

   ```sh
   docker compose exec -T app python - <<'PY'
   import os
   print("SUPABASE_URL set:", bool(os.environ.get("SUPABASE_URL")))
   print("SUPABASE_KEY set:", bool(os.environ.get("SUPABASE_KEY")))
   print("SUPABASE_KEY matches service role:", os.environ.get("SUPABASE_KEY") == os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))
   PY
   ```

3. Back up the named volume, build the migration tool, inspect its dry run,
   then briefly stop the engine so its SQLite ledger has a stable final
   snapshot. The one-off migration container uses the same `.env` and durable
   volume; it performs UPSERTs only.

   ```sh
   mkdir -p /root/tradexa-backups
   docker run --rm -v vps-productn_tradexa-data:/data:ro -v /root/tradexa-backups:/backup alpine:3.20 \
     sh -c 'tar czf /backup/tradexa-data-$(date +%F-%H%M%S).tgz -C /data .'

   docker compose build app
   docker compose run --rm --no-deps app python /app/automation-hub/scripts/migrate_sqlite_to_supabase.py
   docker compose stop app
   docker compose run --rm --no-deps app python /app/automation-hub/scripts/migrate_sqlite_to_supabase.py --apply
   docker compose up -d app
   ```

4. Confirm Supabase is now the active backend and the health check is clean:

   ```sh
   docker compose logs --tail=100 app
   curl -fsS https://trade-logx.com/health
   ```

   Startup must say `ledger backend = SupabaseLedger (Supabase active: True)`.
   The health JSON must report both `settings_supabase.connected` and
   `ledger_supabase.connected` as `true`.

## Enable multiple paper-forward Trading Instances

Trading Instances use Supabase for their configuration and a separate durable
closed-candle cursor per instance. A one-column patch is not sufficient. Run
the **full** `automation-hub/data/trading_instances_schema.sql` file in the
Supabase SQL Editor before creating an instance.

### Supabase SQL Editor

1. Paste and run the complete contents of:
   `automation-hub/data/trading_instances_schema.sql`.
2. Paste and run the complete contents of:
   `automation-hub/data/verify_trading_instances_schema.sql`.
3. The verifier's first query must return `0 rows`. Its second query must return
   `is_primary_key = true` and `has_owner_foreign_key = true`.

The migration is additive and safe to rerun. It ends with a PostgREST schema
cache reload. Never paste a filesystem path by itself into the SQL Editor; paste
the SQL contained in the file.

### VPS terminal

Deploy the application after the Supabase migration, raise the active slot cap
to three without printing the API key, and validate the runtime:

```sh
cd /opt/VPS-productn
git pull --ff-only origin main
docker compose build app
docker compose up -d --force-recreate --wait --wait-timeout 180 app

docker compose exec -T app python - <<'PY'
import json, os, urllib.request
request = urllib.request.Request(
    "http://127.0.0.1:8000/instances/platform",
    data=json.dumps({"max_active_slots": 3}).encode(),
    headers={"x-webhook-secret": os.environ["HUB_CONTROL_KEY"], "content-type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    status = json.load(response)
print("Active slot cap:", status["max_active_slots"])
PY

curl -fsS https://trade-logx.com/health
docker compose ps
docker compose logs --tail=150 app
```

Create and start `BTCUSDT / Supertrend / 5m` and
`ETHUSDT / Decision Brain / 15m` from Trading Instances. Then verify that both
server-owned workers and cursor rows exist without printing credentials:

```sh
cd /opt/VPS-productn
docker compose exec -T app python - <<'PY'
import json, os, urllib.request
request = urllib.request.Request(
    "http://127.0.0.1:8000/instances",
    headers={"x-webhook-secret": os.environ["HUB_CONTROL_KEY"]},
)
with urllib.request.urlopen(request, timeout=30) as response:
    payload = json.load(response)
print("Active slots:", payload["active_slots"], "/", payload["max_active_slots"])
for row in payload["instances"]:
    market = row.get("market_data") or {}
    print(row["id"], row["symbol"], row["strategy_label"], row["timeframe"],
          row["state"], market.get("last_processed_candle_timestamp"))
PY
```

Expected: `Active slots: 2 / 3` (or higher if another intentional instance is
running), two different instance IDs, and independent cursor timestamps that
only move after each instance processes its own newly closed candle.
