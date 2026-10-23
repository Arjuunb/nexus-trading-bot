#!/bin/sh
# Read-only connection test for the VPS operations pipeline. Prints a short status only.
set -u
printf 'host %s, %s, %s\n' "$(hostname)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(uptime -p)"
df -h / | awk 'NR == 2 {print "disk /: " $4 " free of " $2 " (" $5 " used)"}'
free -m | awk '/^Mem:/ {print "memory: " $7 " MiB available of " $2 " MiB"}'
echo "containers:"
docker ps -a --format '  {{.Names}}: {{.Status}}' | sort
echo "last operations:"
tail -n 6 /var/log/nexus-ops/runs.log 2>/dev/null | sed 's/ sha256=[0-9a-f]*//; s/^/  /'
