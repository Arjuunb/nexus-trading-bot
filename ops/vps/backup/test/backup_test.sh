#!/bin/sh
# End-to-end test of setup-backup.sh and nexus-backup. DISPOSABLE TEST HOSTS ONLY: it writes
# /etc/nexus-backup, /usr/local/sbin/nexus-backup and the production replica volume.
# Needs: root, Docker with the P1-B replica images, restic, python3, and an S3 test server, e.g.
#   moto_server -H 127.0.0.1 -p 9000
# Run from the repository root:
#   NEXUS_BACKUP_TEST=disposable S3=http://127.0.0.1:9000 sh ops/vps/backup/test/backup_test.sh
set -u
[ "${NEXUS_BACKUP_TEST:-}" = disposable ] || { echo "refused: set NEXUS_BACKUP_TEST=disposable (never on the VPS)"; exit 3; }
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 3; }
FAILS=0
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
expect() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$3', got '$2')"; fi; }
has() { if printf '%s' "$2" | grep -q -- "$3"; then ok "$1"; else bad "$1 (no '$3' in: $(printf '%s' "$2" | tail -n 4 | tr '\n' '|'))"; fi; }
hasnt() { if printf '%s' "$2" | grep -q -- "$3"; then bad "$1 ('$3' found)"; else ok "$1"; fi; }

ROOT=$(pwd)
T=$(mktemp -d /tmp/nexus-backup-test.XXXXXX)
BUCKET=nexus-test-$(date +%s)
REPO="s3:${S3:?}/$BUCKET"

echo "== replica production container and volume"
P1B_REPLICA_TEST=disposable sh validation/p1b-stop-staging/replica-test/setup.sh
VOL=$(docker volume inspect -f '{{.Mountpoint}}' nexus-migration-tradexa-data-cutover-20260913)
rm -rf "${VOL:?}"/*
mkdir -p "$T/opt/nginx"
printf 'HUB_MASTER_KEY=test-master\nSUPABASE_URL=https://example.invalid\n' > "$T/opt/.env"
printf 'services: {}\n' > "$T/opt/compose.yaml"

python3 -I - "$VOL" <<'PY'
import os, sqlite3, sys
v = sys.argv[1]
def make(path, mode, rows, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=%s" % mode)
    c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    c.executemany("INSERT INTO t(v) VALUES (?)", (("x" * payload,) for _ in range(rows)))
    c.commit(); c.close()
make(os.path.join(v, "wal_busy.db"), "wal", 20000, 1000)
make(os.path.join(v, "rollback_busy.db"), "delete", 200000, 1000)      # ~200 MB, like decisions.db
make(os.path.join(v, "rollback_static.db"), "delete", 40000, 1000)
make(os.path.join(v, "rollback_hot.db"), "delete", 60000, 1000)        # 60 MB, written every 2 ms below
make(os.path.join(v, "sub", "market.sqlite3"), "delete", 500, 50)
make(os.path.join(v, "deployment-backups", "x", "static_wal.db"), "wal", 3000, 200)
for s in ("-wal", "-shm"):   # a WAL-format copy with no sidecars, like deployment-backups/
    p = os.path.join(v, "deployment-backups", "x", "static_wal.db" + s)
    if os.path.exists(p): os.remove(p)
os.makedirs(os.path.join(v, "backups"), exist_ok=True)
open(os.path.join(v, "backups", "sealed.bin"), "wb").write(os.urandom(5 << 20))
open(os.path.join(v, "state.json"), "w").write('{"ok": true}')
PY

writer() {  # db, interval seconds, stats file: commits like the app (default 5 s lock wait)
    python3 -I - "$1" "$2" "$3" <<'PY' &
import json, signal, sqlite3, sys, time
db, every, out = sys.argv[1], float(sys.argv[2]), sys.argv[3]
c = sqlite3.connect(db)             # Python's default timeout: 5 s, as in production stores
stats = {"commits": 0, "errors": 0, "max_ms": 0.0}
def stop(*_):
    json.dump(stats, open(out, "w")); sys.exit(0)
signal.signal(signal.SIGTERM, stop)
while True:
    t = time.time()
    try:
        c.execute("INSERT INTO t(v) VALUES (?)", ("y" * 500,)); c.commit()
        stats["commits"] += 1
    except sqlite3.OperationalError:
        stats["errors"] += 1
    stats["max_ms"] = max(stats["max_ms"], (time.time() - t) * 1000)
    time.sleep(every)
PY
}
writer "$VOL/wal_busy.db" 0.05 "$T/w_wal.json"; W1=$!
writer "$VOL/rollback_busy.db" 3 "$T/w_rb.json"; W2=$!

echo "== setup-backup.sh (answers on stdin; storage = local S3 test server)"
rm -rf /etc/nexus-backup /var/lib/nexus-backup /var/cache/restic
out=$(printf 'testkeyid0001\ntestsecretABCDEFGHIJ123456\ncorrect-horse-battery-staple-42\ncorrect-horse-battery-staple-42\n' \
    | NEXUS_BACKUP_TEST_REPOSITORY="$REPO" sh ops/vps/backup/setup-backup.sh ops/vps/backup/nexus-backup 2>&1); code=$?
expect "setup exits 0" "$code" 0
has "setup prepared the encrypted storage" "$out" "ok: encrypted storage prepared"
hasnt "setup never prints the password" "$out" "correct-horse"
hasnt "setup never prints the application key" "$out" "testsecretABCDEFGHIJ"
expect "settings readable by root only" "$(stat -c %a /etc/nexus-backup/restic.env)" 600
expect "password file readable by root only" "$(stat -c %a /etc/nexus-backup/password)" 600
out=$(printf 'testkeyid0001\ntestsecretABCDEFGHIJ123456\nanother-password-0000000\nanother-password-0000000\n' \
    | NEXUS_BACKUP_TEST_REPOSITORY="$REPO" sh ops/vps/backup/setup-backup.sh ops/vps/backup/nexus-backup 2>&1)
has "setup refuses a different password for existing backups" "$out" "different password"
printf 'testkeyid0001\ntestsecretABCDEFGHIJ123456\ncorrect-horse-battery-staple-42\ncorrect-horse-battery-staple-42\n' \
    | NEXUS_BACKUP_TEST_REPOSITORY="$REPO" sh ops/vps/backup/setup-backup.sh ops/vps/backup/nexus-backup > /dev/null 2>&1
out=$(printf 'testkeyid0001\ntestsecretABCDEFGHIJ123456\nshort\nshort\n' \
    | NEXUS_BACKUP_TEST_REPOSITORY="$REPO" sh ops/vps/backup/setup-backup.sh ops/vps/backup/nexus-backup 2>&1)
has "setup refuses a short password" "$out" "at least 16 characters"
printf 'testkeyid0001\ntestsecretABCDEFGHIJ123456\ncorrect-horse-battery-staple-42\ncorrect-horse-battery-staple-42\n' \
    | NEXUS_BACKUP_TEST_REPOSITORY="$REPO" sh ops/vps/backup/setup-backup.sh ops/vps/backup/nexus-backup > /dev/null 2>&1

echo "== first backup while the app keeps writing (hot database excluded from this run)"
mv "$VOL/rollback_hot.db" "$T/rollback_hot.db"
sleep 2
out=$(NEXUS_BACKUP_TEST_APP="$T/opt" nexus-backup run 2>&1); code=$?
echo "$out" | sed 's/^/      /'
expect "backup exits 0" "$code" 0
has "all 5 databases copied" "$out" "databases: 5 copied (1 WAL via SQLite backup, 4 verified copies)"
has "backup DONE" "$out" "DONE: backup stored off-server"
hasnt "storage address hidden" "$out" "127.0.0.1"
kill $W1 $W2; wait $W1 $W2 2>/dev/null
for w in w_wal w_rb; do
    errs=$(python3 -c "import json;print(json.load(open('$T/$w.json'))['errors'])")
    ms=$(python3 -c "import json;print(int(json.load(open('$T/$w.json'))['max_ms']))")
    expect "app writer $w: no 'database is locked' errors" "$errs" 0
    [ "$ms" -lt 1000 ] && ok "app writer $w: slowest commit ${ms} ms (under 1 s)" || bad "app writer $w: slowest commit ${ms} ms"
done

echo "== the stored backup matches the source"
. /etc/nexus-backup/restic.env
export RESTIC_REPOSITORY RESTIC_PASSWORD_FILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION
listing=$(restic ls latest 2>/dev/null)
has "database copies stored" "$listing" "/var/lib/nexus-backup/snapshot/databases/rollback_busy.db"
hasnt "live database files are never uploaded directly" "$listing" "$VOL/rollback_busy.db"
has "other volume files stored" "$listing" "$VOL/backups/sealed.bin"
has ".env stored" "$listing" "$T/opt/.env"
mkdir -p "$T/restore" && restic restore latest --target "$T/restore" --quiet 2>/dev/null
src_n=$(python3 -c "import sqlite3;print(sqlite3.connect('$VOL/rollback_static.db').execute('select count(*) from t').fetchone()[0])")
dst_n=$(python3 -c "import sqlite3;print(sqlite3.connect('$T/restore/var/lib/nexus-backup/snapshot/databases/rollback_static.db').execute('select count(*) from t').fetchone()[0])")
expect "static database: same row count after restore" "$dst_n" "$src_n"
expect "static WAL-format copy restored intact" \
    "$(python3 -c "import sqlite3;print(sqlite3.connect('$T/restore/var/lib/nexus-backup/snapshot/databases/deployment-backups/x/static_wal.db').execute('pragma integrity_check').fetchone()[0])")" ok

echo "== status, check, restore-test, second run"
out=$(nexus-backup status 2>&1); has "status counts backups" "$out" "backups stored: 1"; has "status shows storage used" "$out" "storage used:"
out=$(nexus-backup check 2>&1); expect "check exits 0" "$?" 0; has "check ok" "$out" "ok: stored backups are complete"
out=$(NEXUS_BACKUP_TEST_APP="$T/opt" nexus-backup restore-test 2>&1); code=$?
echo "$out" | sed 's/^/      /'
expect "restore-test exits 0" "$code" 0
has "restore-test checks every database" "$out" "restored databases: 5 passed integrity_check, 0 failed"
has "restore-test finds the master key" "$out" "contains HUB_MASTER_KEY"
hasnt "restore-test never prints the key value" "$out" "test-master"
[ -d /var/lib/nexus-backup/restore-test ] && bad "restore folder left behind" || ok "restore folder deleted"
out=$(NEXUS_BACKUP_TEST_APP="$T/opt" nexus-backup run 2>&1); expect "second backup exits 0" "$?" 0
has "second backup is incremental" "$out" "uploaded: snapshot"

echo "== a database that never stops changing is reported, not uploaded raw"
mv "$T/rollback_hot.db" "$VOL/rollback_hot.db"
writer "$VOL/rollback_hot.db" 0.002 "$T/w_hot.json"; W3=$!
sleep 1
out=$(NEXUS_BACKUP_TEST_APP="$T/opt" NEXUS_BACKUP_TEST_RETRIES=2 nexus-backup run 2>&1); code=$?
kill $W3; wait $W3 2>/dev/null
echo "$out" | sed 's/^/      /'
expect "partial backup exits 3" "$code" 3
has "hot database reported" "$out" "NOT backed up this time: rollback_hot.db"
has "the other databases still stored" "$out" "databases: 5 copied"
hasnt "hot live file not uploaded directly" "$(restic ls latest 2>/dev/null)" "$VOL/rollback_hot.db"
expect "hot writer never blocked" "$(python3 -c "import json;print(json.load(open('$T/w_hot.json'))['errors'])")" 0

echo "== wrong keys: error shown without the storage address"
sed -e "s|^RESTIC_REPOSITORY=.*|RESTIC_REPOSITORY='$REPO-missing'|" \
    -e "s|^NEXUS_BACKUP_BUCKET=.*|NEXUS_BACKUP_BUCKET='$BUCKET-missing'|" /etc/nexus-backup/restic.env > "$T/bad.env"
out=$(NEXUS_BACKUP_TEST_CONF="$T/bad.env" nexus-backup status 2>&1)
has "a storage error is reported" "$out" "<hidden>"
hasnt "storage address hidden in errors" "$out" "127.0.0.1"
hasnt "bucket name hidden in errors" "$out" "$BUCKET"

echo "== remove"
out=$(sh ops/vps/backup/setup-backup.sh --remove 2>&1); has "remove reports" "$out" "removed:"
[ ! -e /usr/local/sbin/nexus-backup ] && [ ! -e /etc/nexus-backup ] && ok "program and settings removed" || bad "leftovers after remove"

rm -rf "$T"
docker rm -f nexus-remote-stage-rc3-28acdbc-api-1 nexus-trading-bot-app-1 >/dev/null 2>&1
echo "failures: $FAILS"
[ "$FAILS" = 0 ]
