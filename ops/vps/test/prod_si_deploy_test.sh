#!/bin/sh
# Rehearsal of ops/vps/operations/prod-si-deploy.sh (with prod-backup.sh first) on a local copy of
# production's layout: production's own compose.yaml and nginx files from commit 215c96c (the commit
# production runs), an override that mounts an external data volume, a .env with secrets, nginx and
# certbot running, and the exited one-off container pa-replay-2025.
# DISPOSABLE DOCKER HOSTS ONLY. It publishes ports 80 and 443.
#   NEXUS_PROD_SI_TEST=disposable sh ops/vps/test/prod_si_deploy_test.sh   (from the repository root, as root)
set -u
[ "${NEXUS_PROD_SI_TEST:-}" = disposable ] || { echo "refused: set NEXUS_PROD_SI_TEST=disposable (never on the VPS)"; exit 3; }
[ "$(hostname)" = vps-46469a44 ] && { echo "refused: this is the VPS"; exit 3; }
LIVE=215c96cb397ba1a6132c665301c34c4a39a13a6d
git cat-file -e "$LIVE^{commit}" 2>/dev/null || { echo "refused: production's commit $LIVE is not in this clone (git fetch origin codex/guardian-vps-integration)"; exit 3; }
FAILS=0
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
has() { if printf '%s' "$2" | grep -q -- "$3"; then ok "$1"; else bad "$1 (no '$3' in: $(printf '%s' "$2" | tail -n 4 | tr '\n' '|'))"; fi; }
hasnt() { if printf '%s' "$2" | grep -q -- "$3"; then bad "$1 (found '$3')"; else ok "$1"; fi; }
D=$(pwd)/ops/vps/operations/prod-si-deploy.sh
B=$(pwd)/ops/vps/operations/prod-backup.sh
P=nexus-trading-bot
VOL=nexus-migration-tradexa-data-cutover-20260913
T=$(mktemp -d)
WD=$T/opt/nexus-trading-bot
COMMIT=1111111111111111111111111111111111111111
OLD_TAG=nexus-trading-bot-app:guardian-$LIVE
export PROD_SI_TEST_WD=$WD PROD_SI_TEST_WORK=$T/work PROD_SI_TEST_BACKUP=$T/backup PROD_SI_TEST_COMMIT=$COMMIT
export PROD_BACKUP_TEST_BASE=$T/backup PROD_BACKUP_TEST_MARGIN=1000000

cleanup() {
    for c in $(docker ps -aq --filter label=com.docker.compose.project=$P); do docker rm -f "$c" >/dev/null; done
    docker rm -f pa-replay-2025 >/dev/null 2>&1
    for v in $(docker volume ls -q --filter label=com.docker.compose.project=$P); do docker volume rm -f "$v" >/dev/null; done
    docker volume rm -f $VOL >/dev/null 2>&1
    for n in $(docker network ls -q --filter label=com.docker.compose.project=$P); do docker network rm "$n" >/dev/null; done
}
cleanup
mkdir -p "$WD/nginx/entrypoint" "$WD/nginx/templates" "$T/img"

# --- production's own files, byte for byte, from the commit it runs
for f in compose.yaml nginx/reload-config.sh nginx/entrypoint/10-render-config.sh nginx/entrypoint/20-watch-cert-reload.sh \
         nginx/templates/http.conf nginx/templates/https.conf nginx/templates/api.conf; do
    git show "$LIVE:$f" > "$WD/$f"
done
chmod +x "$WD"/nginx/entrypoint/*.sh
[ "$(sha256sum "$WD/compose.yaml" | cut -c1-16)" = 27ab33c5667a1a84 ] && ok "setup: compose.yaml is production's exact file" || bad "setup: compose.yaml differs from production's"
# production's override is not in the repository; this one has its known role (the external data volume)
cat > "$WD/compose.override.yaml" <<EOF
services:
  app:
    environment:
      HUB_PA_POLL: "600"
volumes:
  tradexa-data:
    external: true
    name: $VOL
EOF
# a literal \$ in a value is written \$\$ in .env; the container gets one \$
cat > "$WD/.env" <<'EOF'
HUB_AUTH_MODE=supabase
SUPABASE_URL=https://prod-secret-host.supabase.co
SUPABASE_KEY=SUPERSECRETk1
HUB_MASTER_KEY=SUPERSECRETm$$2
HUB_USE_LIVE_DATA=0
HUB_SMC_POLL=5
HUB_DAILY_REPORT_HOUR=-1
GIT_COMMIT=
EOF
PROD_SI_TEST_PINS=$(python3 - "$WD" <<'PY'
import hashlib, json, os, sys
print(json.dumps({f: hashlib.sha256(open(os.path.join(sys.argv[1], f), "rb").read()).hexdigest()[:16] for f in ("compose.yaml", "compose.override.yaml")}))
PY
); export PROD_SI_TEST_PINS

# --- images. Old: like production's (Debian base, "exec uvicorn ... --timeout-graceful-shutdown 5").
#     New: like the scanned Alpine image (its own start command without it; extra defaults incl. HUB_MONITOR_AGENT=0).
cat > "$T/img/server.py" <<'PY'
import json, os, signal, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
fake = open("/fake").read().split()          # a file, not the environment: the deploy compares environments
if "crash" in fake: raise SystemExit(3)
on = os.environ.get("PRICE_ACTION_LAB_ENABLED", "false").lower() in ("1", "true", "yes", "on")
new = "new" in fake
tag = ["retired-price-action"] if new else ["research-price-action"]
paths = {"/research/price-action/{path}": {"get": {"tags": tag}, "post": {"tags": tag}}, "/smc/status": {"get": {"tags": ["smc"]}}}
if new: paths["/api/v2/strategy-intelligence/overview"] = {"get": {"tags": ["Strategy Intelligence v2"]}}
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if "hang" in fake: time.sleep(30); return
        if self.path == "/health": body = {"status": "ok", "features": {"price_action_lab_enabled": on}}
        elif self.path == "/openapi.json": body = {"paths": paths}
        else: self.send_response(401); self.end_headers(); return
        b = json.dumps(body).encode(); self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8000"))), H).serve_forever()
PY
printf '#!/bin/sh\nexec python /server.py "$@"\n' > "$T/img/uvicorn"
build() {  # build <tag> <revision> <old|new [crash|hang]> <extra Dockerfile lines> ; prints the image ID
    {
        echo "FROM python:3.12-alpine"
        echo "LABEL org.opencontainers.image.revision=$2"
        echo "RUN addgroup -S -g 101 tradexa && adduser -S -D -H -u 100 -G tradexa -h /nonexistent tradexa"
        echo "COPY server.py /server.py"
        echo "COPY uvicorn /usr/local/bin/uvicorn"
        echo "RUN chmod 755 /usr/local/bin/uvicorn && echo '$3' > /fake"
        echo "ENV HUB_DATA_DIR=/var/lib/tradexa HUB_AUTO_ENGINE=0 GIT_COMMIT=baked GIT_BRANCH=baked DEPLOYED_AT=baked"
        echo "WORKDIR /app/automation-hub"
        printf '%b\n' "$4"
    } > "$T/img/Dockerfile"
    docker build -q -t "$1" "$T/img" >/dev/null && docker image inspect "$1" -f '{{.Id}}'
}
PROD_CMD='CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips=* --timeout-graceful-shutdown 5"]'
ALPINE_ENV='ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HUB_ENABLE_EXTERNAL_LIVE=0 HUB_USE_LIVE_DATA=0 HUB_RESEARCH_AUTOSTART=0 HUB_MONITOR_AGENT=0 NEXUS_SHADOW_SUPERVISED=0'
ALPINE_CMD='CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips=*"]'
OLD=$(build "$OLD_TAG" "$LIVE" old "ENV PYTHON_VERSION=3.11.99-debian\nUSER tradexa\n$PROD_CMD")
NEW=$(build "nexus-ci-api:$COMMIT" $COMMIT new "$ALPINE_ENV\nUSER tradexa\n$ALPINE_CMD")
SURPRISE=$(build nexus-ci-api:surprise $COMMIT new "$ALPINE_ENV HUB_SURPRISE=1\nUSER tradexa\n$ALPINE_CMD")
ROOTIMG=$(build nexus-ci-api:root $COMMIT new "$ALPINE_ENV\n$ALPINE_CMD")
CRASH=$(build nexus-ci-api:crash $COMMIT "new crash" "$ALPINE_ENV\nUSER tradexa\n$ALPINE_CMD")
HANG=$(build nexus-ci-api:hang $COMMIT "new hang" "$ALPINE_ENV\nUSER tradexa\n$ALPINE_CMD")
record() { mkdir -p "$T/work"; printf '{"image_id":"%s","commit":"%s"}' "$1" "${2:-$COMMIT}" > "$T/work/api-image.json"; }

# --- production as it runs today: data on the external volume, started the way scripts/deploy.sh does
docker volume create $VOL >/dev/null; V=$(docker volume inspect -f '{{.Mountpoint}}' $VOL)
python3 - "$V" <<'PY'
import os, sqlite3, sys
d = sys.argv[1]
db = sqlite3.connect(os.path.join(d, "smc_strategy_paper.db")); db.execute("CREATE TABLE t(x)")
db.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)]); db.commit(); db.close()
pa = sqlite3.connect(os.path.join(d, "price_action_paper.db")); pa.execute("CREATE TABLE v2_positions(symbol TEXT)"); pa.commit(); pa.close()
for f in os.listdir(d): os.chown(os.path.join(d, f), 100, 101)
os.chown(d, 100, 101)
PY
up_env() { env GIT_COMMIT=$LIVE GIT_BRANCH=codex/guardian-vps-integration DEPLOYED_AT=2026-10-09T00:59:00Z APP_IMAGE_TAG=guardian-$LIVE "$@"; }
(cd "$WD" && up_env docker compose up -d --no-build >/dev/null 2>&1) || bad "setup: production did not start"
(cd "$WD" && up_env docker compose run -d --no-deps --name pa-replay-2025 app python -c "print('replay')" >/dev/null 2>&1)
i=0; until [ "$(docker inspect -f '{{.State.Health.Status}}' $P-nginx-1 2>/dev/null)" = healthy ] || [ $i -gt 120 ]; do sleep 1; i=$((i + 1)); done
sleep 2
[ "$(docker inspect -f '{{.State.Running}} {{.Image}}' $P-app-1)" = "true $OLD" ] && ok "setup: production app runs the old image" || bad "setup: app state"
[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.oneoff"}} {{.State.Status}}' pa-replay-2025 2>/dev/null)" = "True exited" ] && ok "setup: pa-replay-2025 is an exited one-off" || bad "setup: pa-replay-2025"
HASH0=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.config-hash"}}' $P-app-1)
state() { docker inspect -f '{{.Id}} {{.State.StartedAt}} {{.RestartCount}} {{.State.Status}}' "$1"; }
NGINX0=$(state $P-nginx-1); CERTBOT0=$(state $P-certbot-1); REPLAY0=$(state pa-replay-2025)

# --- gates
out=$(sh "$D" plan); has "plan refuses before the image is loaded" "$out" "BLOCKED: the Strategy Intelligence image is not loaded yet"
record "$NEW"
out=$(sh "$D" plan); has "plan refuses before the backup" "$out" "BLOCKED: no finished production backup"
sh "$B" start >/dev/null; i=0; until [ -f "$T/backup/result.json" ] || [ $i -gt 180 ]; do sleep 1; i=$((i + 1)); done
has "backup OK" "$(sh "$B" status)" "RESULT: OK"
record "$NEW" 3333333333333333333333333333333333333333
out=$(sh "$D" plan); has "plan refuses an image recorded for another commit" "$out" "is missing or not commit"
record "$NEW"
echo "# changed" >> "$WD/compose.override.yaml"
out=$(sh "$D" plan); has "plan refuses a changed Compose file" "$out" "is not the file production was started from"
sed -i '/# changed/d' "$WD/compose.override.yaml"
echo "HUB_NEW_SETTING=1" >> "$WD/.env"
out=$(sh "$D" plan); has "plan refuses when .env no longer gives the running app" "$out" "no longer reproduce the running app"
sed -i '/HUB_NEW_SETTING/d' "$WD/.env"
docker run -d --name pa-extra --label com.docker.compose.project=$P --label com.docker.compose.service=app python:3.12-alpine true >/dev/null
out=$(sh "$D" plan); has "plan refuses a second non-one-off app container" "$out" "pa-extra is a second, non-one-off container"
docker rm -f pa-extra >/dev/null
record "$SURPRISE"
out=$(sh "$D" plan); has "plan refuses any other environment change" "$out" "environment would also change: HUB_SURPRISE\.$"
record "$ROOTIMG"
out=$(sh "$D" plan); has "plan refuses an image running as another user" "$out" "the new image's user"
record "$NEW"
out=$(sh "$D" apply); has "apply refuses without a plan" "$out" "BLOCKED: run 'plan' first"

# --- plan
out=$(sh "$D" plan); echo "$out" | sed 's/^/      | /'
has "plan ready" "$out" "PLAN READY"
has "plan shows the image change" "$out" "image: $OLD_TAG .* -> nexus-ci-api@$NEW"
has "plan shows the commit label" "$out" "GIT_COMMIT: $LIVE -> $COMMIT (deploy label)"
has "plan keeps the monitor agent on" "$out" "HUB_MONITOR_AGENT: unset -> 1 (kept on"
has "plan explains same-as-default values" "$out" "HUB_ENABLE_EXTERNAL_LIVE: unset -> 0 (the code's own default, no change)"
has "plan shows base image values" "$out" "PYTHON_VERSION: 3.11.99-debian -> .* (Python base image)"
has "plan keeps production's start command" "$out" "start command: production's own, kept: sh -c exec uvicorn .*--timeout-graceful-shutdown 5"
has "plan lists untouched containers" "$out" "not touched: $P-certbot-1, $P-nginx-1, pa-replay-2025"
hasnt "plan prints no secrets" "$out" "SUPERSECRET"
hasnt "plan prints no Supabase address" "$out" "prod-secret-host"
cp "$T/work/plan.json" "$T/plan.keep"
python3 -c "import json,sys; p=json.load(open(sys.argv[1])); p['at_epoch']-=8000; json.dump(p,open(sys.argv[1],'w'))" "$T/work/plan.json"
out=$(sh "$D" apply); has "apply refuses a plan older than 2 hours" "$out" "older than 2 hours"
cp "$T/plan.keep" "$T/work/plan.json"
python3 -c "import json,sys; p=json.load(open(sys.argv[1])); p['env_digest']='0'*64; json.dump(p,open(sys.argv[1],'w'))" "$T/work/plan.json"
out=$(sh "$D" apply); has "apply refuses when something changed since the plan" "$out" "something changed since the plan (env_digest)"
cp "$T/plan.keep" "$T/work/plan.json"
grep -q SUPERSECRET "$T/work/plan.json" && bad "plan.json holds a secret" || ok "plan.json holds no secret"
[ "$(docker inspect -f '{{.Image}} {{index .Config.Labels "com.docker.compose.config-hash"}}' $P-app-1)" = "$OLD $HASH0" ] && ok "nothing changed by plans and refusals" || bad "refusals changed the app"

# --- apply
out=$(sh "$D" apply); echo "$out" | sed 's/^/      | /'
has "apply deploys" "$out" "DEPLOYED: production runs Strategy Intelligence"
has "container check passes" "$out" "container check: image, volumes, networks, user, start command and all"
has "health says PA off" "$out" '"pa": false'
has "PA routes all retired" "$out" '"not_retired": \[\]'
has "SI routes present" "$out" '"strategy_intelligence_routes": 1'
has "proxy reaches the new app as a visitor would" "$out" "through the proxy: ok"
has "other containers unchanged" "$out" "other containers: unchanged"
hasnt "apply prints no secrets" "$out" "SUPERSECRET"
[ "$(docker inspect -f '{{.State.Running}} {{.Image}}' $P-app-1)" = "true $NEW" ] && ok "app runs the new image" || bad "app state after apply"
case "$(docker inspect -f '{{json .Config.Cmd}}' $P-app-1)" in *"exec uvicorn"*"--timeout-graceful-shutdown 5"*) ok "app runs production's start command";; *) bad "start command: $(docker inspect -f '{{json .Config.Cmd}}' $P-app-1)";; esac
[ "$(docker exec $P-app-1 sh -c 'echo "$HUB_MONITOR_AGENT|$HUB_MASTER_KEY|$HUB_AUTH_MODE|$HUB_PA_POLL|$GIT_COMMIT"')" = "1|SUPERSECRETm\$2|supabase|600|$COMMIT" ] \
    && ok "app keeps the monitor agent, .env values (with \$), override values; commit label updated" || bad "app environment after apply"
[ "$(docker inspect -f '{{range .Mounts}}{{.Name}} {{.Destination}}{{end}}' $P-app-1)" = "$VOL /var/lib/tradexa" ] && ok "app keeps the external data volume" || bad "app mounts"
[ "$(docker inspect -f '{{.Config.User}} {{.Config.WorkingDir}} {{.HostConfig.RestartPolicy.Name}}' $P-app-1)" = "tradexa /app/automation-hub unless-stopped" ] && ok "user, directory, restart policy kept" || bad "app config"
[ "$(state $P-nginx-1)" = "$NGINX0" ] && ok "nginx not restarted (reloaded only)" || bad "nginx changed"
[ "$(state $P-certbot-1)" = "$CERTBOT0" ] && ok "certbot untouched" || bad "certbot changed"
[ "$(state pa-replay-2025)" = "$REPLAY0" ] && ok "pa-replay-2025 untouched" || bad "pa-replay-2025 changed"
rows=$(python3 -c "import sqlite3,sys; print(sqlite3.connect('file:'+sys.argv[1]+'?mode=ro', uri=True).execute('select count(*) from t').fetchone()[0])" "$V/smc_strategy_paper.db")
[ "$rows" = 500 ] && ok "data intact" || bad "data rows $rows"

# --- post-deploy GO/NO-GO check (short window) while the app writes evidence like the new code does
K=$(pwd)/ops/vps/operations/prod-si-check.sh
EVID='
import sqlite3, sys, time
db = sqlite3.connect(sys.argv[1]); db.execute("PRAGMA journal_mode=WAL")
db.execute("CREATE TABLE IF NOT EXISTS v2_source_evidence(event_id TEXT, account_id TEXT, tenant_id TEXT, event_type TEXT, event_timestamp TEXT, source_transaction_id TEXT, evidence_hash TEXT, payload_json TEXT)")
db.commit()
equity, seconds, delay = int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]); t0 = time.time(); n = 0
while time.time() < t0 + seconds:
    db.execute("INSERT INTO v2_source_evidence(account_id, event_type, payload_json) VALUES (?,?,?)", ("smc-1", "QUOTE_SEEN", "{}"))
    if n < equity and time.time() > t0 + delay: db.execute("INSERT INTO v2_source_evidence(account_id, event_type, payload_json) VALUES (?,?,?)", ("smc-1", "EQUITY_OBSERVED", "x" * 900)); n += 1
    db.commit(); time.sleep(0.5)
'
python3 -c "$EVID" "$V/smc_strategy_paper.db" 0 1 0          # the table exists before the window, as after the new app's start
( python3 -c "$EVID" "$V/smc_strategy_paper.db" 1 14 8 ) & w=$!
out=$(PROD_SI_TEST_SECONDS=12 PROD_SI_TEST_STEP=3 sh "$K" measure); wait $w; echo "$out" | sed 's/^/      | /'
has "check says GO" "$out" "^GO: production runs Strategy Intelligence"
has "check counts equity snapshots" "$out" "PASS  equity snapshots within the limit: 1 rows, 1 accounts"
has "check sees the SMC database written" "$out" "PASS  SMC database written during the window"
has "check sees PA data kept and unwritten" "$out" "PASS  no Price Action Lab file written since the deploy: kept, unchanged"
has "check reaches the app through the proxy" "$out" "PASS  https://trade-logx.com/health through the web proxy: ok"
has "check reads app resources" "$out" "app    CPU avg"
hasnt "check prints no secrets" "$out" "SUPERSECRET"
( python3 -c "$EVID" "$V/smc_strategy_paper.db" 25 14 5 ) & w=$!
out=$(PROD_SI_TEST_SECONDS=12 PROD_SI_TEST_STEP=3 sh "$K" measure); wait $w
has "check says NO-GO when snapshots are written on every tick" "$out" "NO-GO: .*equity snapshots within the limit"
python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('insert into v2_positions values (1)'); c.commit()" "$V/price_action_paper.db"
vol_state() { (cd "$V" && ls -1 && find . -type f ! -name '*-shm' -exec sha256sum {} + | sort) | md5sum; }
before=$(vol_state)
out=$(PROD_SI_TEST_SECONDS=3 PROD_SI_TEST_STEP=1 sh "$K" measure)
[ "$(vol_state)" = "$before" ] && ok "check creates and changes no file in the data volume" || bad "check changed the data volume"
has "check says NO-GO when a PA database is written" "$out" "NO-GO: .*no Price Action Lab file written since the deploy"
has "check names the PA file" "$out" "FAIL  no Price Action Lab file written since the deploy: price_action_paper.db"
out=$(sh "$K" watch); has "check rejects unknown argument" "$out" "usage:"

# --- rollback
out=$(sh "$D" rollback); echo "$out" | sed 's/^/      | /'
has "rollback restores the previous app" "$out" "ROLLED BACK"
has "rollback is exactly the app that ran before" "$out" "exactly the app that ran before"
has "proxy reaches the previous app" "$out" "through the proxy: ok"
[ "$(docker inspect -f '{{.State.Running}} {{.Image}} {{index .Config.Labels "com.docker.compose.config-hash"}}' $P-app-1)" = "true $OLD $HASH0" ] && ok "app back on the old image with the original config hash" || bad "app state after rollback"
[ "$(state $P-nginx-1)" = "$NGINX0" ] && ok "nginx not restarted by rollback" || bad "nginx changed by rollback"

# --- a new app that crashes is replaced by the previous one at once
record "$CRASH"; sh "$D" plan >/dev/null
t0=$(date +%s); out=$(sh "$D" apply); t1=$(date +%s); echo "$out" | sed 's/^/      | /'
has "crashing new app is rolled back" "$out" "so it was rolled back"
has "production back and healthy" "$out" "production is back on the previous image and healthy"
[ $((t1 - t0)) -lt 200 ] && ok "crash detected without waiting the full 8 minutes ($((t1 - t0)) s)" || bad "crash rollback took $((t1 - t0)) s"
[ "$(docker inspect -f '{{.State.Running}} {{.Image}} {{index .Config.Labels "com.docker.compose.config-hash"}}' $P-app-1)" = "true $OLD $HASH0" ] && ok "app back on the old image after the crash" || bad "app after crash rollback"

# --- a new app that never becomes healthy is replaced after the wait
record "$HANG"; sh "$D" plan >/dev/null
out=$(PROD_SI_TEST_HEALTH_WAIT=40 sh "$D" apply); echo "$out" | sed 's/^/      | /'
has "never-healthy new app is rolled back" "$out" "the new app was not healthy after 40 s"
[ "$(docker inspect -f '{{.State.Running}} {{.Image}}' $P-app-1)" = "true $OLD" ] && ok "app back on the old image after the timeout" || bad "app after timeout rollback"
[ "$(state $P-nginx-1)" = "$NGINX0" ] && ok "nginx never restarted" || bad "nginx changed"
[ "$(state pa-replay-2025)" = "$REPLAY0" ] && ok "pa-replay-2025 never touched" || bad "pa-replay-2025 changed"

out=$(sh "$D" deploy); has "rejects unknown argument" "$out" "usage:"

cleanup
docker rmi -f "$OLD_TAG" "nexus-ci-api:$COMMIT" nexus-ci-api:surprise nexus-ci-api:root nexus-ci-api:crash nexus-ci-api:hang >/dev/null 2>&1
rm -rf "$T"
echo "prod_si_deploy_test: $FAILS failed"
[ "$FAILS" = 0 ]
