#!/bin/sh
# Test of ops/vps/operations/staging-pa-delete.sh. DISPOSABLE DOCKER HOSTS ONLY: it deletes files.
#   NEXUS_STAGING_PA_TEST=disposable sh ops/vps/test/staging_pa_delete_test.sh   (from the repository root)
set -u
[ "${NEXUS_STAGING_PA_TEST:-}" = disposable ] || { echo "refused: set NEXUS_STAGING_PA_TEST=disposable (never on the VPS)"; exit 3; }
FAILS=0
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
has() { if printf '%s' "$2" | grep -q -- "$3"; then ok "$1"; else bad "$1 (no '$3' in: $(printf '%s' "$2" | tail -n 2 | tr '\n' '|'))"; fi; }
S=ops/vps/operations/staging-pa-delete.sh
P=nexus-remote-stage-rc3-28acdbc
STAGING_PA_TEST_STATE=$(mktemp -d)
export STAGING_PA_TEST_STATE

for c in "$P-api-1" other-user; do
    img=$(docker inspect -f '{{.Config.Image}}' "$c" 2>/dev/null) || continue
    [ "$img" = python:3.12-alpine ] || { echo "refused: $c is not a test container"; exit 3; }
    docker rm -f "$c" >/dev/null
done
for v in "${P}_api-state" nexus-migration-tradexa-data-cutover-20260913; do
    if docker volume inspect "$v" >/dev/null 2>&1; then
        [ "$(docker volume inspect -f '{{index .Labels "p1b.replica-test"}}' "$v")" = 1 ] || { echo "refused: $v is not a test volume"; exit 3; }
        docker volume rm "$v" >/dev/null
    fi
done
docker volume create --label p1b.replica-test=1 --label com.docker.compose.project=$P "${P}_api-state" >/dev/null
docker volume create --label p1b.replica-test=1 nexus-migration-tradexa-data-cutover-20260913 >/dev/null
SV=$(docker volume inspect -f '{{.Mountpoint}}' "${P}_api-state")
PV=$(docker volume inspect -f '{{.Mountpoint}}' nexus-migration-tradexa-data-cutover-20260913)
for d in "$SV" "$PV"; do
    for f in price_action_paper.db smc_strategy_paper.db journal.db; do
        python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('create table t(x)'); c.execute('insert into t values (randomblob(200000))'); c.commit()" "$d/$f"
    done
done
head -c 4096 /dev/urandom > "$SV/price_action_paper.db-wal"; head -c 32768 /dev/zero > "$SV/price_action_paper.db-shm"
docker create --name "$P-api-1" -v "${P}_api-state":/var/lib/tradexa python:3.12-alpine sleep 600 >/dev/null

out=$(sh "$S" delete); has "delete refuses without check" "$out" "run 'check' first"
docker start "$P-api-1" >/dev/null
out=$(sh "$S" check); has "check refuses while staging API runs" "$out" "is running"
docker stop -t 1 "$P-api-1" >/dev/null
docker run -d --name other-user -v "${P}_api-state":/x python:3.12-alpine sleep 600 >/dev/null
out=$(sh "$S" check); has "check refuses while another container uses the volume" "$out" "use ${P}_api-state: other-user"
docker rm -f other-user >/dev/null
python3 -c "import time,sys; f=open(sys.argv[1],'rb'); time.sleep(8)" "$SV/price_action_paper.db" & holder=$!
sleep 1
out=$(sh "$S" check); has "check refuses while a process has the file open" "$out" "open by a process"
kill $holder 2>/dev/null; wait $holder 2>/dev/null

out=$(sh "$S" check); echo "$out" | sed 's/^/      /'
has "check lists the database" "$out" "  price_action_paper.db  "
has "check lists the -wal" "$out" "price_action_paper.db-wal"
has "check lists the -shm" "$out" "price_action_paper.db-shm"
[ -f "$SV/price_action_paper.db" ] && ok "check deleted nothing" || bad "check deleted something"

printf 'x' >> "$SV/price_action_paper.db-wal"
out=$(sh "$S" delete); has "delete refuses when files changed since check" "$out" "changed since 'check'"
sh "$S" check >/dev/null
out=$(sh "$S" delete); echo "$out" | sed 's/^/      /'
has "delete reports DONE" "$out" "DONE"
for f in price_action_paper.db price_action_paper.db-wal price_action_paper.db-shm; do
    [ -e "$SV/$f" ] && bad "staging $f still there" || ok "staging $f deleted"
done
[ -f "$SV/smc_strategy_paper.db" ] && [ -f "$SV/journal.db" ] && ok "other staging files kept" || bad "other staging files lost"
[ -f "$PV/price_action_paper.db" ] && [ -f "$PV/smc_strategy_paper.db" ] && ok "production volume untouched" || bad "production files lost"
out=$(sh "$S" delete); has "second delete refused" "$out" "run 'check' first"
out=$(sh "$S" check); has "check after deletion says it is gone" "$out" "already deleted"

docker rm -f "$P-api-1" >/dev/null
docker volume rm "${P}_api-state" nexus-migration-tradexa-data-cutover-20260913 >/dev/null
rm -rf "$STAGING_PA_TEST_STATE"
echo "failures: $FAILS"
[ "$FAILS" = 0 ]
