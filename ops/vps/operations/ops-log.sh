#!/bin/sh
# Read-only: show the end of an earlier operation's log, by its GitHub run id.
# Use it when the connection to GitHub dropped while that operation kept running on the server.
set -u
run_id=${1:-}
case "$run_id" in ''|*[!0-9]*) echo "give the GitHub run id as the argument"; exit 2 ;; esac
found=0
for log in /var/log/nexus-ops/"$run_id"-*.log; do
    [ -f "$log" ] || continue
    found=1
    rc="${log%.log}.rc"
    if [ -s "$rc" ]; then
        echo "== $(basename "$log"): finished, exit code $(cat "$rc")"
    else
        echo "== $(basename "$log"): still running"
    fi
    tail -n 80 "$log"
done
[ "$found" = 1 ] || { echo "no log for run $run_id"; exit 1; }
