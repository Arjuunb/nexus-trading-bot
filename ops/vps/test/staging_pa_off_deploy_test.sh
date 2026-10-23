#!/bin/sh
# Rehearsal of ops/vps/operations/staging-pa-off-deploy.sh (with staging-backup.sh first) on a local
# copy of the staging layout. DISPOSABLE DOCKER HOSTS ONLY: it creates /home/ubuntu/nexus-rc3-staging-28acdbc.
#   NEXUS_PA_OFF_TEST=disposable sh ops/vps/test/staging_pa_off_deploy_test.sh   (from the repository root)
set -u
[ "${NEXUS_PA_OFF_TEST:-}" = disposable ] || { echo "refused: set NEXUS_PA_OFF_TEST=disposable (never on the VPS)"; exit 3; }
[ "$(hostname)" = vps-46469a44 ] && { echo "refused: this is the VPS"; exit 3; }
FAILS=0
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
has() { if printf '%s' "$2" | grep -q -- "$3"; then ok "$1"; else bad "$1 (no '$3' in: $(printf '%s' "$2" | tail -n 4 | tr '\n' '|'))"; fi; }
hasnt() { if printf '%s' "$2" | grep -q -- "$3"; then bad "$1 (found '$3')"; else ok "$1"; fi; }
D=$(pwd)/ops/vps/operations/staging-pa-off-deploy.sh
B=$(pwd)/ops/vps/operations/staging-backup.sh
P=nexus-remote-stage-rc3-28acdbc
R=/home/ubuntu/nexus-rc3-staging-28acdbc
T=$(mktemp -d)
COMMIT=1111111111111111111111111111111111111111
export PA_OFF_TEST_ROOT=$R PA_OFF_TEST_WORK=$T/work PA_OFF_TEST_BACKUP=$T/backup PA_OFF_TEST_COMMIT=$COMMIT STAGING_BACKUP_TEST_BASE=$T/backup

cleanup() {
    for c in $(docker ps -aq --filter label=com.docker.compose.project=$P) ; do docker rm -f "$c" >/dev/null; done
    docker rm -f nexus-trading-bot-app-1 >/dev/null 2>&1
    for v in $(docker volume ls -q --filter label=com.docker.compose.project=$P); do docker volume rm -f "$v" >/dev/null; done
    for n in $(docker network ls -q --filter label=com.docker.compose.project=$P); do docker network rm "$n" >/dev/null; done
}
cleanup
rm -rf "$R"; mkdir -p "$R/release/ops" "$R/release/customer-workspace" "$R/release/intelligence-recovery-20261018" \
    "$R/task6-worker-rollout-20261021T190957Z-46560af9531a" "$R/private" "$T/img"

# --- images: old API (PA on), new API (PA off, retired routes), crashing API, worker
cat > "$T/img/server.py" <<'PY'
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
on = os.environ.get("PRICE_ACTION_LAB_ENABLED", os.environ["FAKE_DEFAULT"]).lower() in ("1", "true", "yes", "on")
tags = ["retired-price-action"] if os.environ["FAKE_RETIRED"] == "1" else ["price-action"]
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health": body = {"status": "ok", "features": {"price_action_lab_enabled": on}}
        elif self.path == "/openapi.json": body = {"paths": {"/research/price-action/{path}": {"get": {"tags": tags}, "post": {"tags": tags}},
                                                              "/api/v1/research/price-action": {"get": {"tags": tags}}, "/smc/status": {"get": {"tags": ["smc"]}}}}
        else:
            self.send_response(401); self.end_headers(); return
        b = json.dumps(body).encode(); self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
if os.environ.get("FAKE_CRASH") == "1": raise SystemExit(3)
HTTPServer(("0.0.0.0", 8000), H).serve_forever()
PY
build() {  # build <tag> <revision> <default> <retired> <crash>
    printf 'FROM python:3.12-alpine\nLABEL org.opencontainers.image.revision=%s\nENV FAKE_DEFAULT=%s FAKE_RETIRED=%s FAKE_CRASH=%s\nCOPY server.py /server.py\nUSER 100:101\nCMD ["python", "/server.py"]\n' "$2" "$3" "$4" "$5" > "$T/img/Dockerfile"
    docker build -q -t "$1" "$T/img" >/dev/null && docker image inspect "$1" -f '{{.Id}}'
}
OLD=$(build nexus-rc3-api:fake-old 0000000000000000000000000000000000000000 true 0 0)
NEW=$(build "nexus-ci-api:$COMMIT" $COMMIT false 1 0)
WID=$(build nexus-ci-worker:fake 2222222222222222222222222222222222222222 false 1 0)

# --- the staging files
cat > "$R/release/ops/staging.alpine.compose.yaml" <<'EOF'
name: ${NEXUS_REMOTE_STAGING_PROJECT:?project}
services:
  postgres:
    image: postgres:16-alpine
    environment: {POSTGRES_DB: nexus_staging_metadata, POSTGRES_PASSWORD: "${NEXUS_STAGING_OPERATOR_PASSWORD:?pw}"}
    volumes: ["postgres-state:/var/lib/postgresql/data"]
    networks: [metadata]
    healthcheck: {test: ["CMD-SHELL", "pg_isready -U postgres -d nexus_staging_metadata"], interval: 1s, timeout: 2s, retries: 30}
  state-init:
    image: python:3.12-alpine
    command: ["true"]
    volumes: ["api-state:/state/api"]
    network_mode: none
    restart: "no"
  api:
    image: ${NEXUS_STAGING_API_IMAGE:?api image}
    env_file: ${NEXUS_STAGING_APP_ENV:?app env}
    environment:
      HUB_ENV: production
      HUB_DATA_DIR: /var/lib/tradexa
      NEXUS_RESEARCH_WORKER_DSN: postgresql://w:${NEXUS_STAGING_RUNTIME_PASSWORD:?pw}@postgres:5432/x
    depends_on: {state-init: {condition: service_completed_successfully}, postgres: {condition: service_healthy}}
    volumes: ["api-state:/var/lib/tradexa", "research-state:/workers/research:ro"]
    networks: [metadata, ingress]
    read_only: true
    mem_limit: 768m
    cpus: 1.0
    restart: "on-failure:2"
    healthcheck: {test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2)"], interval: 1s, timeout: 3s, retries: 15, start_period: 3s}
  ingress:
    image: nginx:1.27-alpine
    depends_on: [api]
    volumes: ["./nginx.conf:/etc/nginx/nginx.conf:ro"]
    networks: [ingress]
    healthcheck: {test: ["CMD", "wget", "-qO-", "http://127.0.0.1:8080/health"], interval: 1s, timeout: 2s, retries: 2}
  research-worker:
    image: ${NEXUS_STAGING_WORKER_IMAGE:?worker image}
    command: ["sleep", "infinity"]
    volumes: ["research-state:/observation"]
    networks: [metadata]
    profiles: [research]
networks: {metadata: {}, ingress: {}}
volumes: {postgres-state: {}, api-state: {}, research-state: {}}
EOF
printf 'events {}\nhttp { server { listen 8080; location / { proxy_pass http://api:8000; } } }\n' > "$R/release/ops/nginx.conf"
printf 'services:\n  api:\n    environment:\n      HUB_AUTH_MODE: supabase\n' > "$R/release/customer-workspace/compose.customer-workspace.yaml"
printf '{"services": {"research-worker": {"image": "nexus-ci-worker@%s", "pull_policy": "never"}}}\n' "$WID" > "$R/task6-worker-rollout-20261021T190957Z-46560af9531a/compose.worker.yaml"
printf 'services:\n  api:\n    mem_limit: 1536m\n    memswap_limit: 1536m\n' > "$R/release/intelligence-recovery-20261018/compose.memory.yaml"
echo "# extended" > "$R/release/ops/staging.remote.compose.yaml"; echo "# extended" > "$R/release/ops/staging.local.compose.yaml"
cat > "$R/private/compose.env" <<EOF
NEXUS_REMOTE_STAGING_PROJECT=$P
NEXUS_STAGING_API_IMAGE=nexus-rc3-api:fake-old
NEXUS_STAGING_WORKER_IMAGE=python:3.12-alpine
NEXUS_STAGING_APP_ENV=$R/private/app.env
NEXUS_STAGING_OPERATOR_PASSWORD=SUPERSECRETop111
NEXUS_STAGING_RUNTIME_PASSWORD=SUPERSECRETrt222
EOF
printf 'HUB_SECRET=SUPERSECRETapp333\nHUB_SMC_POLL=5\n' > "$R/private/app.env"
pins() { python3 - "$R" <<'PY'
import hashlib, json, os, sys
r = sys.argv[1]
files = ["release/ops/staging.alpine.compose.yaml", "release/customer-workspace/compose.customer-workspace.yaml",
         "task6-worker-rollout-20261021T190957Z-46560af9531a/compose.worker.yaml", "release/intelligence-recovery-20261018/compose.memory.yaml",
         "release/ops/staging.remote.compose.yaml", "release/ops/staging.local.compose.yaml"]
print(json.dumps({os.path.basename(f): hashlib.sha256(open(os.path.join(r, f), "rb").read()).hexdigest()[:16] for f in files}))
PY
}
PA_OFF_TEST_PINS=$(pins); export PA_OFF_TEST_PINS

# --- today's state: API created with the 2 base files, memory raised directly, then stopped (P1-B);
#     research worker from the full file set; production running
cd "$R/release/ops"
C2="docker compose --project-name $P --project-directory $R/release/ops -f staging.alpine.compose.yaml -f ../customer-workspace/compose.customer-workspace.yaml --env-file $R/private/compose.env"
$C2 up -d >/dev/null 2>&1
docker compose --project-name $P --project-directory "$R/release/ops" -f staging.alpine.compose.yaml -f ../customer-workspace/compose.customer-workspace.yaml \
    -f "$R/task6-worker-rollout-20261021T190957Z-46560af9531a/compose.worker.yaml" -f "$R/release/intelligence-recovery-20261018/compose.memory.yaml" \
    --env-file "$R/private/compose.env" --profile research up -d --no-deps research-worker >/dev/null 2>&1
cd - >/dev/null
i=0; until [ "$(docker inspect -f '{{.State.Health.Status}}' $P-ingress-1 2>/dev/null)" = healthy ] || [ $i -gt 60 ]; do sleep 1; i=$((i + 1)); done
docker update --memory 1536m --memory-swap 1536m $P-api-1 >/dev/null
docker stop -t 2 $P-api-1 >/dev/null
docker run -d --name nexus-trading-bot-app-1 python:3.12-alpine sleep infinity >/dev/null
SV=$(docker volume inspect -f '{{.Mountpoint}}' ${P}_api-state)
python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('create table t(x)'); c.executemany('insert into t values(?)',[(i,) for i in range(100)]); c.commit()" "$SV/smc_strategy_paper.db"
worker_before=$(docker inspect -f '{{.Id}} {{.State.StartedAt}}' $P-research-worker-1)
pg_before=$(docker inspect -f '{{.Id}} {{.State.StartedAt}}' $P-postgres-1)
[ "$(docker inspect -f '{{.State.Running}} {{.HostConfig.Memory}}' $P-api-1)" = "false 1610612736" ] && ok "setup: API stopped with 1536 MiB set directly" || bad "setup API state"

# --- gates
out=$(sh "$D" plan); has "plan refuses before the image is loaded" "$out" "BLOCKED: the PA-off image is not loaded yet"
mkdir -p "$T/work"; printf '{"image_id":"%s","commit":"%s"}' "$NEW" "$COMMIT" > "$T/work/api-image.json"
out=$(sh "$D" plan); has "plan refuses before the backup" "$out" "BLOCKED: the staging backup has not finished"
sh "$B" start >/dev/null; i=0; until sh "$B" status | grep -q "^RESULT:" || [ $i -gt 120 ]; do sleep 1; i=$((i + 1)); done
has "backup OK" "$(sh "$B" status)" "RESULT: OK"
printf '{"image_id":"%s","commit":"%s"}' "$NEW" 3333333333333333333333333333333333333333 > "$T/work/api-image.json"
out=$(sh "$D" plan); has "plan refuses an image recorded for another commit" "$out" "is missing or not commit"
printf '{"image_id":"%s","commit":"%s"}' "$NEW" "$COMMIT" > "$T/work/api-image.json"
echo "# changed" >> "$R/release/customer-workspace/compose.customer-workspace.yaml"
out=$(sh "$D" plan); has "plan refuses a changed Compose file" "$out" "is not the reviewed file"
sed -i '/# changed/d' "$R/release/customer-workspace/compose.customer-workspace.yaml"
touch "$SV/smc_strategy_paper.db"
out=$(sh "$D" plan); has "plan refuses when API data changed after the backup" "$out" "smc_strategy_paper.db changed since the backup"
rm -rf "$T/backup"; sh "$B" start >/dev/null; i=0; until sh "$B" status | grep -q "^RESULT:" || [ $i -gt 120 ]; do sleep 1; i=$((i + 1)); done
docker start $P-api-1 >/dev/null
out=$(sh "$D" plan); has "plan refuses while the API runs" "$out" "BLOCKED: staging API is already running"
docker stop -t 2 $P-api-1 >/dev/null
out=$(sh "$D" apply); has "apply refuses without a plan" "$out" "BLOCKED: run 'plan' first"

# --- plan
out=$(sh "$D" plan); has "plan ready" "$out" "PLAN READY"
has "plan shows image change" "$out" "image: nexus-rc3-api:fake-old .* -> nexus-ci-api@$NEW"
has "plan keeps memory" "$out" "the same 1536 MiB / 1536 MiB"
has "plan lists untouched services" "$out" "not touched: research worker, Postgres"
hasnt "plan prints no secrets" "$out" "SUPERSECRET"
cp "$T/work/plan.json" "$T/plan.keep"
python3 -c "import json,sys; p=json.load(open(sys.argv[1])); p['at_epoch']-=8000; json.dump(p,open(sys.argv[1],'w'))" "$T/work/plan.json"
out=$(sh "$D" apply); has "apply refuses a plan older than 2 hours" "$out" "older than 2 hours"
cp "$T/plan.keep" "$T/work/plan.json"
python3 -c "import json,sys; p=json.load(open(sys.argv[1])); p['image_id']='sha256:'+'0'*64; json.dump(p,open(sys.argv[1],'w'))" "$T/work/plan.json"
out=$(sh "$D" apply); has "apply refuses when something changed since the plan" "$out" "something changed since the plan (image_id)"
cp "$T/plan.keep" "$T/work/plan.json"
[ "$(docker inspect -f '{{.State.Running}} {{.Image}}' $P-api-1)" = "false $OLD" ] && ok "nothing changed by refusals" || bad "refusals changed the API"

# --- apply
out=$(sh "$D" apply); echo "$out" | sed 's/^/      | /'
has "apply deploys" "$out" "DEPLOYED: staging API runs the PA-off image"
has "container check passes" "$out" "container check: image, 1536 MiB memory"
has "health says PA off" "$out" '"pa": false'
has "PA routes all retired" "$out" '"not_retired": \[\]'
has "web proxy reloaded and healthy" "$out" "staging web proxy reloaded: healthy"
has "production unchanged" "$out" "production: unchanged"
hasnt "apply prints no secrets" "$out" "SUPERSECRET"
[ "$(docker inspect -f '{{.State.Running}} {{.Image}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}' $P-api-1)" = "true $NEW 1610612736 1610612736" ] && ok "API running new image with 1536 MiB" || bad "API state after apply"
[ "$(docker inspect -f '{{.Id}} {{.State.StartedAt}}' $P-research-worker-1)" = "$worker_before" ] && ok "research worker untouched" || bad "research worker changed"
[ "$(docker inspect -f '{{.Id}} {{.State.StartedAt}}' $P-postgres-1)" = "$pg_before" ] && ok "Postgres untouched" || bad "Postgres changed"
[ "$(docker exec $P-api-1 sh -c 'echo $HUB_SECRET')" = SUPERSECRETapp333 ] && ok "API keeps its env file values" || bad "API env values"
[ "$(docker inspect -f '{{.State.Health.Status}}' $P-ingress-1)" = healthy ] && ok "web proxy reaches the new API" || bad "web proxy health"

# --- rollback
out=$(sh "$D" rollback); has "rollback restores old image, stopped" "$out" "ROLLED BACK"
[ "$(docker inspect -f '{{.State.Running}} {{.Image}} {{.HostConfig.Memory}}' $P-api-1)" = "false $OLD 1610612736" ] && ok "API back on old image, stopped, 1536 MiB" || bad "API state after rollback"
[ "$(docker inspect -f '{{.Id}} {{.State.StartedAt}}' $P-research-worker-1)" = "$worker_before" ] && ok "research worker untouched by rollback" || bad "worker changed by rollback"

# --- a new image that does not become healthy is stopped again
rm -rf "$T/backup"; sh "$B" start >/dev/null; i=0; until sh "$B" status | grep -q "^RESULT:" || [ $i -gt 120 ]; do sleep 1; i=$((i + 1)); done
BADID=$(build "nexus-ci-api:$COMMIT" $COMMIT false 1 1)
printf '{"image_id":"%s","commit":"%s"}' "$BADID" "$COMMIT" > "$T/work/api-image.json"
# the rollback recreated the API from the full file set; plan must accept that too
sh "$D" plan >/dev/null
out=$(sh "$D" apply); has "unhealthy new API is stopped again" "$out" "it was stopped again, so staging is back to stopped"
[ "$(docker inspect -f '{{.State.Running}}' $P-api-1)" = false ] && ok "API stopped after failed deploy" || bad "API left running after failed deploy"
out=$(sh "$D" rollback); has "rollback after a failed deploy" "$out" "ROLLED BACK"
out=$(sh "$D" deploy); has "rejects unknown argument" "$out" "usage:"

cleanup
docker rmi -f nexus-rc3-api:fake-old "nexus-ci-api:$COMMIT" nexus-ci-worker:fake "$NEW" >/dev/null 2>&1
rm -rf "$T" "$R"
echo "staging_pa_off_deploy_test: $FAILS failed"
[ "$FAILS" = 0 ]
