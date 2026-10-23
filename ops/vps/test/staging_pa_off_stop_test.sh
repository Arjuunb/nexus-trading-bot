#!/bin/sh
# Test of ops/vps/operations/staging-pa-off-stop.sh. DISPOSABLE DOCKER HOSTS ONLY.
#   NEXUS_PA_OFF_TEST=disposable sh ops/vps/test/staging_pa_off_stop_test.sh   (from the repository root)
set -u
[ "${NEXUS_PA_OFF_TEST:-}" = disposable ] || { echo "refused: set NEXUS_PA_OFF_TEST=disposable (never on the VPS)"; exit 3; }
[ "$(hostname)" = vps-46469a44 ] && { echo "refused: this is the VPS"; exit 3; }
FAILS=0
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
has() { if printf '%s' "$2" | grep -q -- "$3"; then ok "$1"; else bad "$1 (no '$3' in: $(printf '%s' "$2" | tail -n 3 | tr '\n' '|'))"; fi; }
hasnt() { if printf '%s' "$2" | grep -q -- "$3"; then bad "$1 (found '$3')"; else ok "$1"; fi; }
S=$(pwd)/ops/vps/operations/staging-pa-off-stop.sh
P=nexus-remote-stage-rc3-28acdbc
NEW=f9ebd32a3eedf6740c14a3daafb32e981d1d7ce8
T=$(mktemp -d); export PA_OFF_TEST_STOP_WORK=$T/work
SIG='import signal,sys,time; signal.signal(signal.SIGTERM, lambda *a: sys.exit(0)); time.sleep(99999)'

reset() {  # reset <staging revision> <staging supabase host>
    docker rm -f $P-api-1 nexus-trading-bot-app-1 >/dev/null 2>&1
    docker volume rm -f ${P}_api-state prod-data >/dev/null 2>&1
    docker volume create ${P}_api-state >/dev/null; docker volume create prod-data >/dev/null
    docker run -d --name $P-api-1 --restart on-failure:2 -v ${P}_api-state:/var/lib/tradexa \
        --label com.docker.compose.project=$P --label com.docker.compose.service=api --label org.opencontainers.image.revision=$1 \
        -e SUPABASE_URL=https://$2/rest -e HUB_SECRET=SUPERSECRETs1 python:3.12-alpine python -c "$SIG" >/dev/null
    docker run -d --name nexus-trading-bot-app-1 -v prod-data:/var/lib/tradexa --label com.docker.compose.project=nexus-trading-bot \
        -e SUPABASE_URL=https://prod.supabase.co -e HUB_SECRET=SUPERSECRETp2 python:3.12-alpine python -c "$SIG" >/dev/null
    SV=$(docker volume inspect -f '{{.Mountpoint}}' ${P}_api-state)
    head -c 1000 /dev/urandom > "$SV/smc_strategy_paper.db"
}

reset f8778ed42039d415f3829e08a05225976fe0d0a5 staging.supabase.co
out=$(sh "$S" stop); has "refuses the old build" "$out" "NO-GO: .* is not running the staging build $NEW"
[ "$(docker inspect -f '{{.State.Running}}' $P-api-1)" = true ] && ok "old-build container left running" || bad "old-build container stopped"

reset $NEW prod.supabase.co
out=$(sh "$S" stop); has "refuses when staging shares production's Supabase project" "$out" "use the SAME Supabase project"
hasnt "never prints the Supabase address" "$out" "supabase.co"

reset $NEW staging.supabase.co
out=$(sh "$S" check); has "check GO" "$out" "GO: preflight passed"
has "check names the PA-off build" "$out" "staging PA-off build f9ebd32"
hasnt "check prints no secrets" "$out" "SUPERSECRET"
out=$(sh "$S" stop); has "stop DONE" "$out" "DONE: staging API stopped"
[ "$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' $P-api-1)" = "exited 0" ] && ok "staging API exited cleanly" || bad "staging API state"
[ "$(docker inspect -f '{{.State.Running}}' nexus-trading-bot-app-1)" = true ] && ok "production still running" || bad "production stopped"
out=$(sh "$S" verify); has "verify: still stopped" "$out" "ok: staging API is still stopped"
has "verify: files unchanged" "$out" "ok: staging lab files unchanged since the stop"
out=$(sh "$S" stop); has "second stop does nothing" "$out" "already 'exited'; not stopping anything"

docker rm -f $P-api-1 nexus-trading-bot-app-1 >/dev/null 2>&1
docker volume rm -f ${P}_api-state prod-data >/dev/null 2>&1
rm -rf "$T"
echo "staging_pa_off_stop_test: $FAILS failed"
[ "$FAILS" = 0 ]
