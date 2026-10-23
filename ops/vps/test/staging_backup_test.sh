#!/bin/sh
# Test of ops/vps/operations/staging-backup.sh. DISPOSABLE DOCKER HOSTS ONLY.
#   NEXUS_STAGING_BACKUP_TEST=disposable sh ops/vps/test/staging_backup_test.sh   (from the repository root)
set -u
[ "${NEXUS_STAGING_BACKUP_TEST:-}" = disposable ] || { echo "refused: set NEXUS_STAGING_BACKUP_TEST=disposable (never on the VPS)"; exit 3; }
FAILS=0
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
has() { if printf '%s' "$2" | grep -q -- "$3"; then ok "$1"; else bad "$1 (no '$3' in: $(printf '%s' "$2" | tail -n 3 | tr '\n' '|'))"; fi; }
hasnt() { if printf '%s' "$2" | grep -q -- "$3"; then bad "$1 (found '$3')"; else ok "$1"; fi; }
B=ops/vps/operations/staging-backup.sh
P=nexus-remote-stage-rc3-28acdbc
T=$(mktemp -d)
STAGING_BACKUP_TEST_BASE="$T/staging-backup"; export STAGING_BACKUP_TEST_BASE
PGPW=SUPERSECRETpgpass555

for c in "$P-api-1" "$P-postgres-1" "$P-research-worker-1" "$P-ingress-1" "$P-metadata-init-1" "$P-state-init-1" nexus-trading-bot-app-1 intruder nexus-ops-staging-backup-pgtest; do
    img=$(docker inspect -f '{{.Config.Image}}' "$c" 2>/dev/null) || continue
    case "$img" in python:3.12-alpine|postgres:16-alpine|sha256:*) docker rm -f "$c" >/dev/null ;; *) echo "refused: $c is not a test container ($img)"; exit 3 ;; esac
done
for v in "${P}_api-state" "${P}_postgres-state"; do
    docker volume rm -f "$v" >/dev/null 2>&1
done
docker volume create --label com.docker.compose.project=$P "${P}_api-state" >/dev/null
docker volume create --label com.docker.compose.project=$P "${P}_postgres-state" >/dev/null
SV=$(docker volume inspect -f '{{.Mountpoint}}' "${P}_api-state")

# --- staging API volume: SMC database whose last commits live only in the WAL, small dbs, Task 6 folder
python3 -I - "$SV/smc_strategy_paper.db" <<'PY'
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA wal_autocheckpoint=0"); c.execute("PRAGMA foreign_keys=ON")
c.execute("CREATE TABLE smc_sessions(id INTEGER PRIMARY KEY, name TEXT)")
c.execute("CREATE TABLE smc_orders(id INTEGER PRIMARY KEY, session_id INTEGER REFERENCES smc_sessions(id), qty REAL)")
c.execute("CREATE TABLE v2_source_evidence(id INTEGER PRIMARY KEY, kind TEXT, payload TEXT)")
c.executemany("INSERT INTO smc_sessions(name) VALUES (?)", [("s%d" % i,) for i in range(10)])
c.executemany("INSERT INTO smc_orders(session_id, qty) VALUES (?, ?)", [(i % 10 + 1, i * 0.5) for i in range(1000)])
c.executemany("INSERT INTO v2_source_evidence(kind, payload) VALUES ('EQUITY_OBSERVED', ?)", [('{"e": %d, "p": "%s"}' % (i, "z" * 900),) for i in range(20000)])
c.commit(); c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
for i in range(300):   # these commits stay in the WAL
    c.execute("INSERT INTO v2_source_evidence(kind, payload) VALUES ('LATE', ?)", ('{"late": %d}' % i,))
    if i % 30 == 29: c.commit()
c.commit()
os._exit(0)
PY
for f in hub.db journal.db account.db api-operational-spool.sqlite; do
    python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('create table t(x)'); c.executemany('insert into t values(?)', [(i,) for i in range(50)]); c.commit()" "$SV/$f"
done
mkdir -p "$SV/.task6-sqlite-recovery-3678ed8786fa40eda0b79428d374a5ef/preservation" "$SV/sub"
head -c 3000000 /dev/urandom > "$SV/.task6-sqlite-recovery-3678ed8786fa40eda0b79428d374a5ef/preservation/snapshot.sqlite.gz"
echo note > "$SV/sub/readme.txt"
chown -R 1000:1000 "$SV"

fingerprint() { (cd "$SV" && find . -type f ! -path './.task6*' | sort | while read -r f; do printf '%s %s %s\n' "$f" "$(stat -c '%s %.9Y %i' "$f")" "$(sha256sum < "$f" | cut -c1-16)"; done); }

# --- containers: stopped API using the volume, healthy Postgres with data, production
docker create --name "$P-api-1" -v "${P}_api-state":/var/lib/tradexa python:3.12-alpine sleep 600 >/dev/null
docker run -d --name "$P-postgres-1" -v "${P}_postgres-state":/var/lib/postgresql/data \
    -e POSTGRES_DB=nexus_staging_metadata -e POSTGRES_PASSWORD=$PGPW \
    --health-cmd 'pg_isready -U postgres -d nexus_staging_metadata' --health-interval 1s postgres:16-alpine >/dev/null
docker run -d --name nexus-trading-bot-app-1 python:3.12-alpine sleep 600 >/dev/null
i=0; until [ "$(docker inspect -f '{{.State.Health.Status}}' "$P-postgres-1")" = healthy ] || [ $i -gt 60 ]; do sleep 1; i=$((i + 1)); done
sleep 2
docker exec -i -u postgres "$P-postgres-1" psql -q -v ON_ERROR_STOP=1 -U postgres -d nexus_staging_metadata >/dev/null <<SQL
CREATE ROLE nexus_staging_worker LOGIN PASSWORD 'SUPERSECRETworker666';
CREATE SCHEMA research AUTHORIZATION nexus_staging_worker;
CREATE TABLE research.jobs(id serial primary key, name text);
CREATE TABLE public.heartbeat(id serial primary key, at timestamptz default now());
INSERT INTO research.jobs(name) SELECT 'job' || g FROM generate_series(1, 500) g;
ALTER TABLE research.jobs OWNER TO nexus_staging_worker;
SQL
before=$(fingerprint)

wait_done() { i=0; while [ $i -lt 240 ]; do s=$(sh "$B" status); printf '%s' "$s" | grep -q "^RESULT:" && { printf '%s' "$s"; return; }; sleep 1; i=$((i + 1)); done; printf '%s' "$s"; }

# --- refusals
docker start "$P-api-1" >/dev/null
out=$(sh "$B" start); has "refuses while the staging API runs" "$out" "BLOCKED: staging API is running"
docker stop -t 1 "$P-api-1" >/dev/null

docker run -d --name intruder -v "${P}_api-state":/x:ro python:3.12-alpine sleep 600 >/dev/null
out=$(sh "$B" start); has "refuses while another container uses the volume" "$out" "BLOCKED: running container intruder"
docker rm -f intruder >/dev/null

python3 -c "import time,sys; f=open(sys.argv[1],'rb'); time.sleep(30)" "$SV/hub.db" & holder=$!
sleep 1
out=$(sh "$B" start); has "refuses while a process holds a file open" "$out" "holds $SV/hub.db open"
kill $holder 2>/dev/null; wait $holder 2>/dev/null

ln -s /etc/passwd "$SV/link"
out=$(sh "$B" start); has "refuses a symlink in the volume" "$out" "link is not a single-link regular file"
rm -f "$SV/link"

docker stop -t 5 "$P-postgres-1" >/dev/null
out=$(sh "$B" start); has "refuses while staging Postgres is down" "$out" "BLOCKED: staging Postgres is not running"
docker start "$P-postgres-1" >/dev/null
i=0; until [ "$(docker inspect -f '{{.State.Health.Status}}' "$P-postgres-1")" = healthy ] || [ $i -gt 60 ]; do sleep 1; i=$((i + 1)); done

out=$(STAGING_BACKUP_TEST_MARGIN=900000000000000 sh "$B" start); has "refuses without enough free disk" "$out" "not enough free disk"
[ ! -e "$STAGING_BACKUP_TEST_BASE/manifest.json" ] && ok "no manifest after refusals" || bad "manifest after refusals"

# --- the backup, while a writer keeps adding Postgres rows (like the research worker)
( j=0; while [ $j -lt 60 ]; do docker exec -u postgres "$P-postgres-1" psql -q -U postgres -d nexus_staging_metadata -c 'insert into public.heartbeat default values' >/dev/null 2>&1; sleep 0.3; j=$((j + 1)); done ) & writer=$!
out=$(sh "$B" start); has "starts" "$out" "STARTED in the background"
has "start names the largest file" "$out" "smc_strategy_paper.db"
has "start reports the skipped Task 6 folder" "$out" "skipped Task 6 recovery folder 0.00 GB"
res=$(wait_done)
kill $writer 2>/dev/null; wait $writer 2>/dev/null
printf '%s\n' "$res" | sed 's/^/      | /'
has "result OK" "$res" "RESULT: OK"
has "source hashes recorded" "$res" "source: SHA-256 of 8 files recorded"
has "restored files byte-identical" "$res" "all 8 files byte-identical"
has "SQLite integrity ok" "$res" "5 SQLite databases pass integrity_check"
has "WAL frames applied in the restored copy" "$res" "smc_strategy_paper.db: [1-9][0-9]* WAL frames applied"
has "Postgres dumped" "$res" "dumped roles and 2 databases (nexus_staging_metadata, postgres)"
has "Postgres restored with every table" "$res" "all 2 tables back"
has "temporary copies deleted" "$res" "temporary restored copies deleted"
hasnt "no Postgres password in output" "$res" "SUPERSECRET"
hasnt "no Postgres password in progress log" "$(cat "$STAGING_BACKUP_TEST_BASE/progress.log")" "SUPERSECRET"
[ "$(fingerprint)" = "$before" ] && ok "originals unchanged (size, mtime, inode, sha256 incl. -wal/-shm)" || bad "originals changed"
[ -f "$SV/smc_strategy_paper.db-wal" ] && ok "original WAL still there (not checkpointed)" || bad "original WAL gone"
[ ! -e "$STAGING_BACKUP_TEST_BASE/restore-test" ] && [ ! -e "$STAGING_BACKUP_TEST_BASE/pg-restore-test" ] && ok "restore folders removed" || bad "restore folders left"
docker inspect nexus-ops-staging-backup-pgtest >/dev/null 2>&1 && bad "throwaway Postgres left running" || ok "throwaway Postgres removed"
[ "$(stat -c %a "$STAGING_BACKUP_TEST_BASE/postgres/globals.sql")" = 600 ] && ok "role dump readable by root only" || bad "role dump permissions"
tarlist=$(zstd -dc "$STAGING_BACKUP_TEST_BASE"/api-state.tar.zst 2>/dev/null | tar -tf - 2>/dev/null || gzip -dc "$STAGING_BACKUP_TEST_BASE"/api-state.tar.gz | tar -tf -)
hasnt "Task 6 folder not in the archive" "$tarlist" "task6"
has "sub-folder files in the archive" "$tarlist" "sub/readme.txt"

# --- independent restore check: unpack and count, load the dump into a fresh Postgres
R=$T/independent; mkdir -p "$R"
(zstd -dc "$STAGING_BACKUP_TEST_BASE"/api-state.tar.zst 2>/dev/null || gzip -dc "$STAGING_BACKUP_TEST_BASE"/api-state.tar.gz) | tar -C "$R" -xf -
n=$(python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('select count(*) from v2_source_evidence').fetchone()[0])" "$R/smc_strategy_paper.db")
[ "$n" = 20300 ] && ok "restored SMC database has all 20300 rows incl. WAL-only commits" || bad "restored SMC rows $n"
docker run -d --name nexus-ops-staging-backup-pgtest --network none -e POSTGRES_PASSWORD=x postgres:16-alpine >/dev/null
i=0; until docker logs nexus-ops-staging-backup-pgtest 2>&1 | grep -q "init process complete" && docker exec -u postgres nexus-ops-staging-backup-pgtest psql -At -c 'select 1' >/dev/null 2>&1 || [ $i -gt 60 ]; do sleep 1; i=$((i + 1)); done
docker exec -i -u postgres nexus-ops-staging-backup-pgtest psql -q -U postgres < "$STAGING_BACKUP_TEST_BASE/postgres/globals.sql" >/dev/null 2>&1
docker exec -u postgres nexus-ops-staging-backup-pgtest createdb -U postgres nexus_staging_metadata
docker exec -i -u postgres nexus-ops-staging-backup-pgtest pg_restore -U postgres --exit-on-error -d nexus_staging_metadata < "$STAGING_BACKUP_TEST_BASE/postgres/db-nexus_staging_metadata.dump" && ok "dump restores independently" || bad "independent dump restore"
n=$(docker exec -u postgres nexus-ops-staging-backup-pgtest psql -At -U postgres -d nexus_staging_metadata -c 'select count(*) from research.jobs')
[ "$n" = 500 ] && ok "restored research.jobs has 500 rows" || bad "restored research.jobs $n"
o=$(docker exec -u postgres nexus-ops-staging-backup-pgtest psql -At -U postgres -d nexus_staging_metadata -c "select tableowner from pg_tables where tablename='jobs'")
[ "$o" = nexus_staging_worker ] && ok "table owner role restored" || bad "table owner $o"
docker rm -f nexus-ops-staging-backup-pgtest >/dev/null

out=$(sh "$B" start); has "second start keeps the verified backup" "$out" "a verified backup already exists"
out=$(sh "$B" status); has "status shows the verified backup" "$out" "verified backup: .*api-state.tar"
out=$(sh "$B" bogus); has "rejects unknown argument" "$out" "usage:"

for c in "$P-api-1" "$P-postgres-1" nexus-trading-bot-app-1; do docker rm -f "$c" >/dev/null; done
docker volume rm -f "${P}_api-state" "${P}_postgres-state" >/dev/null
rm -rf "$T"
echo "staging_backup_test: $FAILS failed"
[ "$FAILS" = 0 ]
