#!/bin/sh
# Test of ops/vps/operations/prod-backup.sh. DISPOSABLE DOCKER HOSTS ONLY.
#   NEXUS_PROD_BACKUP_TEST=disposable sh ops/vps/test/prod_backup_test.sh   (from the repository root, as root)
set -u
[ "${NEXUS_PROD_BACKUP_TEST:-}" = disposable ] || { echo "refused: set NEXUS_PROD_BACKUP_TEST=disposable (never on the VPS)"; exit 3; }
[ "$(hostname)" = vps-46469a44 ] && { echo "refused: this is the VPS"; exit 3; }
FAILS=0
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
has() { if printf '%s' "$2" | grep -q -- "$3"; then ok "$1"; else bad "$1 (no '$3' in: $(printf '%s' "$2" | tail -n 3 | tr '\n' '|'))"; fi; }
S=$(pwd)/ops/vps/operations/prod-backup.sh
P=nexus-trading-bot
T=$(mktemp -d)
export PROD_BACKUP_TEST_BASE=$T/base PROD_BACKUP_TEST_MARGIN=1000000
# The "app": a writer that commits to a WAL database without pause, logging any failed write.
WRITER='
import os, sqlite3, time
db = sqlite3.connect("/var/lib/tradexa/smc_strategy_paper.db", timeout=5)
db.execute("PRAGMA journal_mode=WAL")
db.execute("CREATE TABLE IF NOT EXISTS ticks(n INTEGER PRIMARY KEY, pad TEXT)")
n = 0
while True:
    try:
        db.execute("INSERT INTO ticks(pad) VALUES (?)", ("x" * 500,)); db.commit(); n += 1
        if n % 200 == 0: open("/var/lib/tradexa/writes.ok", "w").write(str(n))
    except Exception as e:
        open("/var/lib/tradexa/backups/write-errors.log", "a").write(repr(e) + "\n")
    time.sleep(0.002)
'
cleanup() { docker rm -f $P-app-1 >/dev/null 2>&1; docker volume rm -f ${P}_tradexa-data >/dev/null 2>&1; rm -rf "$T"; }
trap cleanup EXIT
cleanup; T=$(mktemp -d); export PROD_BACKUP_TEST_BASE=$T/base

out=$(sh "$S" start 2>&1); has "blocked when production is not there" "$out" "BLOCKED"

docker volume create --label com.docker.compose.project=$P ${P}_tradexa-data >/dev/null
V=$(docker volume inspect -f '{{.Mountpoint}}' ${P}_tradexa-data)
mkdir -p "$V/backups" "$V/sub"
python3 - "$V" <<'EOF'
import os, sqlite3, sys
d = sys.argv[1]
db = sqlite3.connect(os.path.join(d, "ledger.db"))                       # rollback-journal database
db.execute("CREATE TABLE trades(id INTEGER PRIMARY KEY, v TEXT)")
db.executemany("INSERT INTO trades(v) VALUES (?)", [("t%d" % i,) for i in range(5000)]); db.commit(); db.close()
idle = sqlite3.connect(os.path.join(d, "sub", "idle_wal.db"))            # WAL database nobody has open
idle.execute("PRAGMA journal_mode=WAL"); idle.execute("CREATE TABLE a(x)"); idle.execute("INSERT INTO a VALUES (1)")
idle.commit(); idle.close()
open(os.path.join(d, "settings.json"), "w").write('{"k": 1}')
open(os.path.join(d, "backups", "old.db"), "wb").write(b"SQLite format 3\x00" + b"\x00" * 5000)
for root, dirs, files in os.walk(d):
    for f in dirs + files: os.chown(os.path.join(root, f), 100, 101)
EOF
docker run -d --name $P-app-1 --label com.docker.compose.project=$P --label com.docker.compose.service=app \
    --label com.docker.compose.project.working_dir=/nonexistent -v ${P}_tradexa-data:/var/lib/tradexa \
    python:3.12-alpine python -c "$WRITER" >/dev/null
for i in $(seq 1 50); do [ -f "$V/writes.ok" ] && break; sleep 0.2; done
idle_before=$(ls "$V/sub")
ledger_before=$(sha256sum "$V/ledger.db")

out=$(sh "$S" start 2>&1)
has "starts" "$out" "STARTED in the background"
has "skips the app's own backups folder" "$out" "skipped the app's own backups/"
out2=$(sh "$S" start 2>&1)
case "$out2" in *"already running"*|*"already exists"*) ok "second start does not start another";; *) bad "second start: $out2";; esac
for i in $(seq 1 120); do [ -f "$T/base/result.json" ] && break; sleep 1; done
out=$(sh "$S" status 2>&1)
has "result OK" "$out" "RESULT: OK"
has "restore test passed" "$out" "byte-identical to their snapshots"
has "records the image for rollback" "$out" "rollback record: image python:3.12-alpine"
M=$T/base/manifest.json
python3 - "$M" <<'EOF' && ok "manifest lists every database, checked" || bad "manifest incomplete"
import json, sys
m = json.load(open(sys.argv[1]))
assert set(m["sqlite"]) == {"ledger.db", "smc_strategy_paper.db", "sub/idle_wal.db"}, m["sqlite"].keys()
assert all(v["integrity_check"] == "ok" for v in m["sqlite"].values())
assert m["sqlite"]["ledger.db"]["rows"] == 5000
assert "settings.json" in m["files"] and not any(k.startswith("backups/") for k in m["files"])
assert not any(k.endswith(("-wal", "-shm")) for k in m["files"])
EOF
# The snapshot of the live database is consistent and has what was committed before it began.
D=$(mktemp -d); zstd -d -q -c "$T/base/app-data.tar.zst" 2>/dev/null | tar -C "$D" -xf - 2>/dev/null || gzip -dc "$T"/base/app-data.tar.gz | tar -C "$D" -xf -
rows=$(python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('select count(*) from ticks').fetchone()[0])" "$D/smc_strategy_paper.db")
[ "$rows" -ge 200 ] && ok "live database snapshot has $rows committed rows" || bad "live snapshot has $rows rows"
rm -rf "$D"
[ ! -s "$V/backups/write-errors.log" ] && ok "the app's writes never failed during the backup" || bad "writes failed: $(head -2 "$V/backups/write-errors.log")"
[ "$(ls "$V/sub")" = "$idle_before" ] && ok "no -wal/-shm created next to an idle database" || bad "files created: $(ls "$V/sub")"
[ "$(sha256sum "$V/ledger.db")" = "$ledger_before" ] && ok "idle database byte-identical" || bad "idle database changed"
[ "$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' $P-app-1)" = "true 0" ] && ok "app kept running, never restarted" || bad "app state changed"
[ ! -d "$T/base/work" ] && [ ! -d "$T/base/restore-test" ] && ok "temporary copies removed" || bad "temporary copies left"
out=$(sh "$S" start 2>&1); has "a finished backup is not overwritten" "$out" "already exists"
echo "== $FAILS failed"
[ "$FAILS" = 0 ]
