#!/bin/sh
# Test of ops/vps/operations/staging-pa-delete.sh. DISPOSABLE DOCKER HOSTS ONLY.
#   NEXUS_STAGING_PA_TEST=disposable sh ops/vps/test/staging_pa_delete_test.sh   (from the repository root)
set -u
[ "${NEXUS_STAGING_PA_TEST:-}" = disposable ] || { echo "refused: set NEXUS_STAGING_PA_TEST=disposable (never on the VPS)"; exit 3; }
FAILS=0
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
has() { if printf '%s' "$2" | grep -q -- "$3"; then ok "$1"; else bad "$1 (no '$3' in: $(printf '%s' "$2" | tail -n 3 | tr '\n' '|'))"; fi; }
hasnt() { if printf '%s' "$2" | grep -q -- "$3"; then bad "$1 ('$3' found)"; else ok "$1"; fi; }
S=ops/vps/operations/staging-pa-delete.sh
P=nexus-remote-stage-rc3-28acdbc
T=$(mktemp -d)
STAGING_PA_TEST_STATE="$T/state"; STAGING_PA_TEST_BACKUP="$T/pa-backup"
export STAGING_PA_TEST_STATE STAGING_PA_TEST_BACKUP

for c in "$P-api-1" other-user nexus-trading-bot-app-1; do
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

# staging PA database whose last transactions exist only in the WAL (the app exited without a checkpoint)
python3 -I - "$SV/price_action_paper.db" <<'PY'
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA wal_autocheckpoint=0")
c.execute("CREATE TABLE pa_sessions(id INTEGER PRIMARY KEY, v TEXT)")
c.execute("CREATE TABLE v2_source_evidence(id INTEGER PRIMARY KEY, kind TEXT, payload TEXT)")
c.commit(); c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
for i in range(300):
    c.execute("INSERT INTO v2_source_evidence(kind, payload) VALUES ('EQUITY_OBSERVED', ?)", ('{"equity": %d, "x": "%s"}' % (i, "y" * 900),))
    if i % 10 == 9: c.commit()
c.commit()
os._exit(0)   # no close: WAL and SHM stay, nothing checkpointed
PY
for d in "$SV" "$PV"; do
    for f in smc_strategy_paper.db journal.db; do
        python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('create table t(x)'); c.commit()" "$d/$f"
    done
done
python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('create table pa_sessions(x)'); c.commit()" "$PV/price_action_paper.db"
docker create --name "$P-api-1" -v "${P}_api-state":/var/lib/tradexa python:3.12-alpine sleep 600 >/dev/null
docker run -d --name nexus-trading-bot-app-1 -v nexus-migration-tradexa-data-cutover-20260913:/var/lib/tradexa python:3.12-alpine sleep 600 >/dev/null

fingerprint() { (cd "$SV" && for f in $(ls -A | sort); do [ -f "$f" ] && printf '%s %s %s\n' "$f" "$(stat -c '%s %Y %i' "$f")" "$(sha256sum < "$f" | cut -c1-16)"; done); }
before=$(fingerprint)

echo "== 1. realistic state: WAL holds committed data, no backup"
out=$(sh "$S" check); code=$?
echo "$out" | sed 's/^/      /'
[ "$code" = 2 ] && ok "check exits 2 (blocked)" || bad "check exit $code"
has "staging API stopped gate passes" "$out" "\[PASS\] staging API stopped"
has "no running consumer gate passes" "$out" "\[PASS\] no running container uses the volume"
has "process gate passes" "$out" "\[PASS\] no process holds any file in the volume open"
has "schema gate passes" "$out" "\[PASS\] schema is the Price Action paper database"
has "production isolation passes" "$out" "\[PASS\] production uses a different volume"
has "WAL gate fails: committed frames only in the WAL" "$out" "\[FAIL\] no committed WAL transactions waiting for a checkpoint"
has "backup gate fails: none" "$out" "\[FAIL\] verified, restore-tested backup"
has "verdict BLOCKED" "$out" "^BLOCKED: no committed WAL transactions"
has "SMC listed as excluded" "$out" "  smc_strategy_paper.db  "
has "exact scope printed" "$out" "deletion would remove exactly: $SV/price_action_paper.db, $SV/price_action_paper.db-wal, $SV/price_action_paper.db-shm"
[ "$(fingerprint)" = "$before" ] && ok "check changed no byte, size, time or inode in the volume" || bad "check modified the volume"

echo "== 2. WAL frame count matches SQLite's own count"
mine=$(printf '%s' "$out" | sed -n 's/.*committed frames \([0-9]*\);.*/\1/p')
mkdir -p "$T/copy" && cp "$SV"/price_action_paper.db* "$T/copy/"
theirs=$(python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()[1])" "$T/copy/price_action_paper.db")
[ -n "$mine" ] && [ "$mine" = "$theirs" ] && ok "committed WAL frames: script $mine = SQLite $theirs" || bad "WAL frames: script '$mine' vs SQLite '$theirs'"

echo "== 3. unsafe states are reported"
docker start "$P-api-1" >/dev/null
out=$(sh "$S" check); has "running staging API fails" "$out" "\[FAIL\] staging API stopped"
docker stop -t 1 "$P-api-1" >/dev/null
docker run -d --name other-user -v "${P}_api-state":/x python:3.12-alpine sleep 600 >/dev/null
out=$(sh "$S" check); has "other running consumer fails" "$out" "\[FAIL\] no running container uses the volume: other-user"
docker rm -f other-user >/dev/null
python3 -c "import time,sys; f=open(sys.argv[1],'rb'); time.sleep(10)" "$SV/journal.db" & holder=$!
sleep 1
out=$(sh "$S" check); has "open file anywhere in the volume fails" "$out" "\[FAIL\] no process holds any file in the volume open"
kill $holder 2>/dev/null; wait $holder 2>/dev/null

echo "== 4. all gates satisfied (checkpointed copy state + verified backup) -> SAFE, still nothing deleted"
python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()" "$SV/price_action_paper.db"
mkdir -p "$STAGING_PA_TEST_BACKUP"
gzip -c "$SV/price_action_paper.db" > "$STAGING_PA_TEST_BACKUP/price_action_paper.db.gz"
python3 -I - "$SV" "$STAGING_PA_TEST_BACKUP" <<'PY'
import hashlib, json, os, sys
sv, b = sys.argv[1], sys.argv[2]
arch = os.path.join(b, "price_action_paper.db.gz")
src = {}
for f in ("price_action_paper.db", "price_action_paper.db-wal"):
    p = os.path.join(sv, f)
    if os.path.exists(p): st = os.stat(p); src[f] = {"inode": st.st_ino, "size": st.st_size}
json.dump({"source": src, "archive": arch, "archive_bytes": os.path.getsize(arch),
           "archive_sha256": hashlib.sha256(open(arch, "rb").read()).hexdigest(),
           "restore_test": {"integrity_check": "ok", "at": "test"}}, open(os.path.join(b, "manifest.json"), "w"))
PY
out=$(sh "$S" check); code=$?
[ "$code" = 0 ] && ok "check exits 0" || bad "check exit $code"
has "WAL gate passes after checkpoint" "$out" "\[PASS\] no committed WAL transactions"
has "backup gate passes with verified manifest" "$out" "\[PASS\] verified, restore-tested backup"
has "verdict SAFE (for separate approval)" "$out" "^SAFE FOR SEPARATE DELETION APPROVAL"
has "states it is not permission" "$out" "not permission to delete"
out=$(sh "$S" delete); code=$?
[ "$code" = 2 ] && ok "delete exits 2 even when every gate passes" || bad "delete exit $code"
has "delete is disabled in this version" "$out" "deletion is disabled in this version"
[ -f "$SV/price_action_paper.db" ] && ok "nothing deleted" || bad "database was deleted"

echo "== 5. backup tampering and mismatched source are caught"
printf 'x' >> "$STAGING_PA_TEST_BACKUP/price_action_paper.db.gz"
out=$(sh "$S" check); has "tampered archive fails" "$out" "archive missing or wrong size\|checksum mismatch"
gzip -c "$SV/price_action_paper.db" > "$STAGING_PA_TEST_BACKUP/price_action_paper.db.gz"
cp "$SV/price_action_paper.db" "$T/x" && cat "$T/x" > "$SV/price_action_paper.db.new" && mv "$SV/price_action_paper.db.new" "$SV/price_action_paper.db"
out=$(sh "$S" check); has "backup of different files fails" "$out" "made from different files"

echo "== 6. production database is never a target"
hasnt "production path not in scope" "$(sh "$S" check | grep 'deletion would remove')" "$PV"
[ -f "$PV/price_action_paper.db" ] && ok "production PA file untouched" || bad "production PA file missing"

docker rm -f "$P-api-1" nexus-trading-bot-app-1 >/dev/null
docker volume rm "${P}_api-state" nexus-migration-tradexa-data-cutover-20260913 >/dev/null
rm -rf "$T"
echo "failures: $FAILS"
[ "$FAILS" = 0 ]
