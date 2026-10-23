#!/bin/sh
# Tests p1b_stop_staging_api.sh against replica containers. DISPOSABLE DOCKER HOSTS ONLY.
#
#   P1B_REPLICA_TEST=disposable sh run_tests.sh
#   (optional) P1B_RESTART_DOCKER_CMD='...'   command that restarts dockerd, to simulate a reboot
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT="$HERE/../p1b_stop_staging_api.sh"
STAGE=nexus-remote-stage-rc3-28acdbc-api-1
PROD=nexus-trading-bot-app-1
WORK=/root/p1b-staging
FAILS=0
[ "${P1B_REPLICA_TEST:-}" = disposable ] || { echo "refused: set P1B_REPLICA_TEST=disposable (never on the VPS)"; exit 3; }

ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
expect() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$3', got '$2')"; fi; }
has() { if printf '%s' "$2" | grep -q -- "$3"; then ok "$1"; else bad "$1 (no '$3' in output)"; fi; }
state() { docker inspect -f "$2" "$1" 2>/dev/null; }
no_leak() { if printf '%s' "$2" | grep -qi 'secret-\|prodref\|stagingref'; then bad "$1: secret or address printed"; else ok "$1: no secret or address printed"; fi; }
setup() { sh "$HERE/setup.sh" "$@"; }
restart_docker() {
    sh -c "$P1B_RESTART_DOCKER_CMD"
    i=0; until docker info >/dev/null 2>&1 || [ $i -ge 60 ]; do sleep 1; i=$((i + 1)); done; sleep 6
}
volume_rows() {  # rows in the staging PA file, read from the host without locking (immutable)
    python3 -I - "$(docker volume inspect -f '{{.Mountpoint}}' nexus-remote-stage-rc3-28acdbc_api-state)" <<'PY'
import sqlite3, sys
c = sqlite3.connect("file:%s/price_action_paper.db?mode=ro&immutable=1" % sys.argv[1], uri=True)
print(c.execute("PRAGMA integrity_check").fetchone()[0], c.execute("SELECT count(*) FROM v2_source_evidence").fetchone()[0])
PY
}

echo "== 1. check (read-only)"
setup; rm -rf "$WORK"
out=$(sh "$SCRIPT" check); code=$?
expect "check exits 0" "$code" 0
has "check says GO" "$out" "^GO: preflight passed"
has "check shows production workers" "$out" "workers operational (3 of 3 running)"
expect "check leaves staging running" "$(state $STAGE '{{.State.Status}}')" running
expect "check writes nothing" "$(ls -A $WORK 2>/dev/null | wc -l)" 0
no_leak "check" "$out"

echo "== 2. stop (clean shutdown)"
sleep 2
prod_started=$(state $PROD '{{.State.StartedAt}}'); since=$(date +%s)
out=$(sh "$SCRIPT" stop); code=$?
until_ts=$(( $(date +%s) + 1 ))
expect "stop exits 0" "$code" 0
has "stop says DONE" "$out" "^DONE: staging API stopped"
expect "staging exited" "$(state $STAGE '{{.State.Status}}')" exited
expect "staging exit code" "$(state $STAGE '{{.State.ExitCode}}')" 0
expect "restart policy kept (clean exit)" "$(state $STAGE '{{.HostConfig.RestartPolicy.Name}}')" on-failure
expect "production still running" "$(state $PROD '{{.State.Status}}')" running
expect "production not restarted" "$(state $PROD '{{.State.StartedAt}}')" "$prod_started"
prod_events=$(docker events --since "$since" --until "$until_ts" --filter container=$PROD \
    --format '{{.Action}}' | grep -v -E '^(exec_create|exec_start|exec_die|health_status)' | sort -u | tr '\n' ' ')
expect "no Docker actions on production other than its own health checks" "$prod_events" ""
expect "WAL files merged and removed" "$(grep -c 'wal absent' $WORK/sizes-after-stop.txt)" 2
set -- $(volume_rows)
expect "staging file intact after stop" "$1" ok
[ "${2:-0}" -gt 0 ] && ok "staging rows kept ($2)" || bad "staging rows missing"
no_leak "stop" "$out"

echo "== 3. verify"
sleep 3
out=$(sh "$SCRIPT" verify); code=$?
expect "verify exits 0" "$code" 0
has "verify: still stopped" "$out" "ok: staging API is still stopped"
has "verify: files unchanged" "$out" "ok: staging lab files unchanged since the stop"

echo "== 4. stop again (idempotent)"
before=$(cat $WORK/sizes-before-stop.txt)
out=$(sh "$SCRIPT" stop)
has "second stop does nothing" "$out" "already 'exited'; not stopping anything"
expect "before-record kept" "$(cat $WORK/sizes-before-stop.txt)" "$before"

if [ -n "${P1B_RESTART_DOCKER_CMD:-}" ]; then
    echo "== 5. reboot simulation after a clean stop"
    restart_docker
    expect "staging stays stopped after Docker restart" "$(state $STAGE '{{.State.Status}}')" exited
    expect "production back after Docker restart" "$(state $PROD '{{.State.Status}}')" running
fi

echo "== 6. undo (runbook commands) is detected by verify"
docker start $STAGE >/dev/null; sleep 4
out=$(sh "$SCRIPT" verify)
has "verify warns staging running" "$out" "WARNING: staging API is 'running'"
has "verify reports changed files" "$out" "now:     price_action_paper.db"

echo "== 7. shutdown that hangs until Docker kills it (exit 137)"
P1B_STAGE_CMD='python -c "import time; time.sleep(10**6)"' setup; rm -rf "$WORK"
fast=$(mktemp); sed 's/^STOP_TIMEOUT=120$/STOP_TIMEOUT=3/' "$SCRIPT" > "$fast"
out=$(sh "$fast" stop); rm -f "$fast"
expect "killed staging exit code" "$(state $STAGE '{{.State.ExitCode}}')" 137
expect "restart policy set to no" "$(state $STAGE '{{.HostConfig.RestartPolicy.Name}}')" no
expect "original policy recorded" "$(cat $WORK/restart-policy-before.txt)" on-failure:2
has "stop explains the policy change" "$out" "restart policy changed to 'no' (exit code 137"
if [ -n "${P1B_RESTART_DOCKER_CMD:-}" ]; then
    docker rm -f p1b-counterfactual >/dev/null 2>&1
    docker run -d --name p1b-counterfactual --restart on-failure:2 p1b-stage-api:test \
        sh -c 'python -c "import time; time.sleep(10**6)"' >/dev/null
    docker stop -t 2 p1b-counterfactual >/dev/null
    restart_docker
    expect "staging stays stopped after Docker restart" "$(state $STAGE '{{.State.Status}}')" exited
    expect "control container WITHOUT the change restarts" "$(state p1b-counterfactual '{{.State.Status}}')" running
    docker rm -f p1b-counterfactual >/dev/null
fi

echo "== 8. refusals change nothing"
refuse() {  # description, expected message; the replicas are already set up
    out=$(sh "$SCRIPT" stop 2>&1); code=$?
    expect "$1: exit 2" "$code" 2
    has "$1: message" "$out" "$2"
    expect "$1: staging still running" "$(state $STAGE '{{.State.Status}}')" running
    no_leak "$1" "$out"
}
rm -rf "$WORK"
setup "https://PRODREF.supabase.co/rest/v1"; refuse "same Supabase project" "SAME Supabase project"
setup "https://stagingref.supabase.co" 0000000000000000000000000000000000000000; refuse "other staging build" "not running the staging build"
setup ""; refuse "staging without SUPABASE_URL" "staging SUPABASE_URL not found"
setup; docker rm -f $PROD >/dev/null; refuse "production missing" "production container $PROD not found"
setup; docker volume create --label p1b.replica-test=1 p1b-other >/dev/null
docker rm -f $STAGE >/dev/null
docker run -d --name $STAGE --restart on-failure:2 --label com.docker.compose.project=nexus-remote-stage-rc3-28acdbc \
    --label com.docker.compose.service=api --label org.opencontainers.image.revision=f8778ed42039d415f3829e08a05225976fe0d0a5 \
    -e SUPABASE_URL=https://stagingref.supabase.co -v p1b-other:/var/lib/tradexa p1b-stage-api:test >/dev/null; sleep 2
refuse "staging on another volume" "does not use the data volume"
setup; out=$(su nobody -s /bin/sh -c "sh '$SCRIPT' stop" 2>&1); code=$?
expect "non-root: exit 2" "$code" 2
expect "non-root: staging still running" "$(state $STAGE '{{.State.Status}}')" running
expect "refusals wrote nothing" "$(ls -A $WORK 2>/dev/null | wc -l)" 0
sh "$SCRIPT" >/dev/null 2>&1; expect "no mode: usage exit 64" "$?" 64

echo "== cleanup"
docker rm -f $STAGE $PROD >/dev/null 2>&1
docker volume rm nexus-remote-stage-rc3-28acdbc_api-state nexus-migration-tradexa-data-cutover-20260913 p1b-other >/dev/null 2>&1
rm -rf "$WORK"
echo "failures: $FAILS"
[ "$FAILS" = 0 ]
