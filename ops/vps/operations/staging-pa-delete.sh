#!/bin/sh
# Delete the staging Price Action lab database (rubbish per-quote EQUITY_OBSERVED rows, ~16.7 GB).
#
#   check    read-only: confirm it is safe and show exactly what would be deleted; records the files
#   delete   re-check everything, then delete exactly the files recorded by 'check'
#
# Only these files in the STAGING api-state volume are deleted:
#   price_action_paper.db, price_action_paper.db-wal, price_action_paper.db-shm
# Refuses unless: the staging API container is stopped, no running container uses that volume, no
# process has the files open, they are the same files (inode and size) that 'check' recorded, and
# the volume is not production's. Production, its PA Lab, and every other file are never touched.
set -u
umask 077
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
PROJECT=nexus-remote-stage-rc3-28acdbc
API="$PROJECT-api-1"
VOLUME="${PROJECT}_api-state"
PROD_VOLUME=nexus-migration-tradexa-data-cutover-20260913
DB=price_action_paper.db
STATE=${STAGING_PA_TEST_STATE:-/var/lib/nexus-ops/staging-pa}
mkdir -p "$STATE"

say() { printf '%s\n' "$*"; }
refuse() { say "REFUSED: $*"; say "Nothing was deleted."; exit 2; }
gb() { awk -v b="$1" 'BEGIN {printf "%.2f GB", b / 1e9}'; }
free_gb() { df -P -B1 / | awk 'NR == 2 {printf "%.1f GB free of %.1f GB", $4 / 1e9, $2 / 1e9}'; }

preflight() {
    [ "$(id -u)" = 0 ] || refuse "must run as root"
    [ "$VOLUME" != "$PROD_VOLUME" ] || refuse "target volume is production's"
    [ "$(docker volume inspect -f '{{index .Labels "com.docker.compose.project"}}' "$VOLUME" 2>/dev/null)" = "$PROJECT" ] \
        || refuse "volume $VOLUME not found, or not part of $PROJECT"
    DIR=$(docker volume inspect -f '{{.Mountpoint}}' "$VOLUME")
    [ -n "$DIR" ] && [ -d "$DIR" ] || refuse "cannot find the folder of $VOLUME"
    case "$DIR" in */"$PROD_VOLUME"/*) refuse "folder belongs to production" ;; esac

    running=$(docker inspect -f '{{.State.Running}}' "$API" 2>/dev/null) || refuse "$API not found"
    [ "$running" = false ] || refuse "$API is running; it must stay stopped (P1-B)"
    users=$(docker ps -q | xargs -r docker inspect -f '{{.Name}} {{range .Mounts}}{{.Name}} {{end}}' \
        | awk -v v="$VOLUME" '{for (i = 2; i <= NF; i++) if ($i == v) print substr($1, 2)}')
    [ -z "$users" ] || refuse "running container(s) use $VOLUME: $users"

    [ -f "$DIR/$DB" ] || refuse "$DB is not there (already deleted?)"
    [ "$(head -c 16 "$DIR/$DB" | tr -d '\0')" = "SQLite format 3" ] || refuse "$DB is not a SQLite database"

    FILES=""
    for s in "" -wal -shm; do [ -f "$DIR/$DB$s" ] && FILES="$FILES $DB$s"; done
    open=""
    for f in $FILES; do
        for fd in /proc/[0-9]*/fd/*; do
            [ "$(readlink "$fd" 2>/dev/null)" = "$DIR/$f" ] && open="$open ${fd#/proc/}"
        done
    done
    [ -z "$open" ] || refuse "files are open by a process: $open"
}

describe() {  # name inode bytes, one line per file
    for f in $FILES; do say "$f $(stat -c '%i %s' "$DIR/$f")"; done
}

cmd_check() {
    preflight
    describe > "$STATE/checked.txt"
    total=0
    say "SAFE TO DELETE (staging, volume $VOLUME):"
    while read -r f inode bytes; do
        say "  $f  $(gb "$bytes")"
        total=$((total + bytes))
    done < "$STATE/checked.txt"
    say "total: $(gb "$total")"
    say "kept in that volume: $(ls -1 "$DIR" | grep -v -x -e "$DB" -e "$DB-wal" -e "$DB-shm" | wc -l) other files (staging SMC lab, journals, settings)"
    say "checks: $API stopped; no running container uses the volume; no process has the files open"
    say "disk /: $(free_gb)"
    say "Nothing was deleted. Run 'delete' to delete exactly these files."
}

cmd_delete() {
    [ -s "$STATE/checked.txt" ] || refuse "run 'check' first"
    preflight
    describe > "$STATE/now.txt"
    cmp -s "$STATE/checked.txt" "$STATE/now.txt" || refuse "the files changed since 'check'; run 'check' again"
    say "before: $(free_gb)"
    for f in $FILES; do
        rm -f -- "$DIR/$f" && say "  deleted $f"
    done
    rm -f "$STATE/checked.txt"
    for f in $FILES; do [ -e "$DIR/$f" ] && { say "PROBLEM: $f still exists"; exit 3; }; done
    say "after:  $(free_gb)"
    say "DONE. If staging is ever started again, its PA lab starts with an empty database."
}

case "${1:-}" in
    check) cmd_check ;;
    delete) cmd_delete ;;
    *) say "usage: staging-pa-delete.sh check|delete"; exit 64 ;;
esac
