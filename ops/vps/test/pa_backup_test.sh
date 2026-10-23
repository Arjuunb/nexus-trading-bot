#!/bin/sh
# Test of ops/vps/operations/pa-backup.sh together with staging-pa-delete.sh check.
# DISPOSABLE DOCKER HOSTS ONLY.
#   NEXUS_STAGING_PA_TEST=disposable sh ops/vps/test/pa_backup_test.sh   (from the repository root)
set -u
[ "${NEXUS_STAGING_PA_TEST:-}" = disposable ] || { echo "refused: set NEXUS_STAGING_PA_TEST=disposable (never on the VPS)"; exit 3; }
FAILS=0
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
has() { if printf '%s' "$2" | grep -q -- "$3"; then ok "$1"; else bad "$1 (no '$3' in: $(printf '%s' "$2" | tail -n 3 | tr '\n' '|'))"; fi; }
B=ops/vps/operations/pa-backup.sh
C=ops/vps/operations/staging-pa-delete.sh
P=nexus-remote-stage-rc3-28acdbc
T=$(mktemp -d)
STAGING_PA_TEST_STATE="$T/state"; STAGING_PA_TEST_BACKUP="$T/pa-backup"
export STAGING_PA_TEST_STATE STAGING_PA_TEST_BACKUP

for c in "$P-api-1" nexus-trading-bot-app-1; do
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

make_db() {  # ~40 MB PA database whose last transactions live only in the WAL
    rm -f "$SV"/price_action_paper.db*
    python3 -I - "$SV/price_action_paper.db" <<'PY'
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA wal_autocheckpoint=0"); c.execute("PRAGMA foreign_keys=ON")
c.execute("CREATE TABLE pa_sessions(id INTEGER PRIMARY KEY, name TEXT)")
c.execute("CREATE TABLE pa_orders(id INTEGER PRIMARY KEY, session_id INTEGER REFERENCES pa_sessions(id), qty REAL)")
c.execute("CREATE TABLE v2_source_evidence(id INTEGER PRIMARY KEY, kind TEXT, payload TEXT)")
c.execute("CREATE INDEX ix_kind ON v2_source_evidence(kind)")
c.executemany("INSERT INTO pa_sessions(name) VALUES (?)", [("s%d" % i,) for i in range(20)])
c.executemany("INSERT INTO pa_orders(session_id, qty) VALUES (?, ?)", [(i % 20 + 1, i * 0.5) for i in range(2000)])
c.executemany("INSERT INTO v2_source_evidence(kind, payload) VALUES ('EQUITY_OBSERVED', ?)", [('{"equity": %d, "p": "%s"}' % (i, "z" * 1100),) for i in range(30000)])
c.commit(); c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
for i in range(400):   # these commits stay in the WAL
    c.execute("INSERT INTO v2_source_evidence(kind, payload) VALUES ('EQUITY_OBSERVED', ?)", ('{"late": %d, "p": "%s"}' % (i, "w" * 1100),))
    if i % 20 == 19: c.commit()
c.commit()
os._exit(0)
PY
}
fingerprint() { (cd "$SV" && for f in price_action_paper.db price_action_paper.db-wal price_action_paper.db-shm; do [ -f "$f" ] && printf '%s %s %s\n' "$f" "$(stat -c '%s %.9Y %i' "$f")" "$(sha256sum < "$f" | cut -c1-16)"; done); }
wait_done() { i=0; while [ $i -lt 120 ]; do s=$(sh "$B" status); printf '%s' "$s" | grep -q "^RESULT:" && { printf '%s' "$s"; return; }; sleep 1; i=$((i + 1)); done; printf '%s' "$s"; }

make_db
python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('create table t(x)'); c.commit()" "$SV/smc_strategy_paper.db"
python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('create table pa_sessions(x)'); c.commit()" "$PV/price_action_paper.db"
docker create --name "$P-api-1" -v "${P}_api-state":/var/lib/tradexa python:3.12-alpine sleep 600 >/dev/null
docker run -d --name nexus-trading-bot-app-1 -v nexus-migration-tradexa-data-cutover-20260913:/var/lib/tradexa python:3.12-alpine sleep 600 >/dev/null

echo "== 1. before the backup: check is BLOCKED"
out=$(sh "$C" check); has "WAL gate fails" "$out" "\[FAIL\] no committed WAL transactions"
has "backup gate fails" "$out" "\[FAIL\] verified, restore-tested backup"
frames=$(printf '%s' "$out" | sed -n 's/.*committed frames \([0-9]*\);.*/\1/p')
[ -n "$frames" ] && [ "$frames" -gt 0 ] && ok "outstanding committed WAL frames: $frames" || bad "no WAL frames counted"
out=$(sh "$B" status); has "status before any backup" "$out" "no backup has been started"

echo "== 2. refusals"
docker start "$P-api-1" >/dev/null
out=$(sh "$B" start); has "refuses while staging API runs" "$out" "BLOCKED: staging API is running"
docker stop -t 1 "$P-api-1" >/dev/null
python3 -c "import time,sys; f=open(sys.argv[1],'rb'); time.sleep(8)" "$SV/smc_strategy_paper.db" & h=$!
sleep 1
out=$(sh "$B" start); has "refuses while any file in the volume is open" "$out" "BLOCKED: process"
kill $h 2>/dev/null; wait $h 2>/dev/null
out=$(STAGING_PA_TEST_MARGIN=999000000000000 sh "$B" start); has "refuses without enough free disk" "$out" "BLOCKED: not enough free disk"

echo "== 3. backup, restore test and manifest"
before=$(fingerprint)
out=$(sh "$B" start); has "starts in the background" "$out" "STARTED in the background"
out=$(wait_done); echo "$out" | sed 's/^/      /'
has "result OK" "$out" "RESULT: OK"
has "restored files byte-identical" "$out" "byte-identical to the originals"
has "restore test applied exactly the outstanding frames" "$out" "applied $frames committed WAL frames"
has "integrity_check ok" "$out" "integrity_check ok; foreign key violations 0"
has "temporary restored copy deleted" "$out" "temporary restored copy deleted"
[ -d "$STAGING_PA_TEST_BACKUP/restore-test" ] && bad "restore folder left behind" || ok "restore folder gone"
[ "$(fingerprint)" = "$before" ] && ok "originals unchanged (size, ns timestamp, inode, sha256)" || bad "originals changed"
rows=$(python3 -c "import json,sys; m=json.load(open(sys.argv[1])); print(m['restore_test']['tables']['v2_source_evidence'])" "$STAGING_PA_TEST_BACKUP/manifest.json")
[ "$rows" = 30400 ] && ok "restored copy has all 30,400 evidence rows, including the 400 that were only in the WAL" || bad "evidence rows: $rows"

echo "== 4. check after the backup: SAFE, delete still refuses"
out=$(sh "$C" check); code=$?; echo "$out" | grep -E "\[(PASS|FAIL)\]|^SAFE|^BLOCKED" | sed 's/^/      /'
[ "$code" = 0 ] && ok "check exits 0" || bad "check exit $code"
has "WAL gate passes because the backup holds exactly those frames" "$out" "\[PASS\] no committed WAL transactions waiting for a checkpoint, or all of them inside the verified backup"
has "backup gate passes" "$out" "\[PASS\] verified, restore-tested backup of the current files"
has "verdict SAFE FOR SEPARATE DELETION APPROVAL" "$out" "^SAFE FOR SEPARATE DELETION APPROVAL"
out=$(sh "$C" delete); has "delete still disabled" "$out" "deletion is disabled in this version"
[ "$(fingerprint)" = "$before" ] && ok "still nothing changed" || bad "files changed"
out=$(sh "$B" start); has "second start does not redo a verified backup" "$out" "already exists"

echo "== 5. a backup of older files does not count"
printf 'x' >> "$SV/price_action_paper.db-wal"
out=$(sh "$C" check); has "changed WAL invalidates the backup" "$out" "made from different files"
has "and the WAL gate fails again" "$out" "\[FAIL\] no committed WAL"

echo "== 6. files changing during the backup -> FAILED, no manifest"
make_db
rm -f "$STAGING_PA_TEST_BACKUP/manifest.json" "$STAGING_PA_TEST_BACKUP"/price_action_paper.tar.*
( i=0; while [ $i -lt 60 ]; do printf 'y' >> "$SV/price_action_paper.db-wal"; sleep 0.05; i=$((i + 1)); done ) &
out=$(sh "$B" start); out=$(wait_done); wait
has "backup FAILED when the source changed" "$out" "RESULT: FAILED"
[ -f "$STAGING_PA_TEST_BACKUP/manifest.json" ] && bad "manifest written despite failure" || ok "no manifest after a failed backup"

echo "== 7. production untouched"
[ -f "$PV/price_action_paper.db" ] && ok "production PA file present" || bad "production PA file missing"

docker rm -f "$P-api-1" nexus-trading-bot-app-1 >/dev/null
docker volume rm "${P}_api-state" nexus-migration-tradexa-data-cutover-20260913 >/dev/null
rm -rf "$T"
echo "failures: $FAILS"
[ "$FAILS" = 0 ]
