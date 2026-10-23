#!/bin/sh
# P1-C: free disk space by removing old production app images and Docker build cache.
#
#   plan    read-only: list exactly what would be removed and kept, and save that list
#   apply   remove exactly the saved list (refuses if anything changed since the plan), then the
#           build cache
#
# Removed: images of the production app repository (nexus-trading-bot-app) that no container uses,
#          except the two most recent ones; and the Docker build cache (only speeds up rebuilds).
# Kept:    every image used by any container (running or stopped), the two most recent unused
#          production images (rollback), every image of any other repository (staging, CI, base
#          images). Containers, volumes and data are never touched; images are removed by tag
#          without --force, so Docker itself refuses anything still in use.
set -u
umask 077
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
REPO=nexus-trading-bot-app
KEEP_RECENT=2
STATE=${DOCKER_CLEANUP_TEST_STATE:-/var/lib/nexus-ops/cleanup}
mkdir -p "$STATE"

say() { printf '%s\n' "$*"; }
free_gb() { df -P -B1 / | awk 'NR == 2 {printf "%.1f GB free of %.1f GB", $4 / 1e9, $2 / 1e9}'; }

select_images() {  # prints "remove|keep <id> <tag> <created> <reason>" lines
    used=$(docker ps -aq | xargs -r docker inspect -f '{{.Image}}' | sort -u)
    docker image ls --no-trunc --format '{{.ID}} {{.Repository}}:{{.Tag}}' "$REPO" | sort -u > "$STATE/listed"
    [ -s "$STATE/listed" ] || return 0
    cut -d' ' -f1 "$STATE/listed" | sort -u | xargs docker image inspect -f '{{.Id}} {{.Created}}' > "$STATE/created"
    USED="$used" python3 -I - "$STATE/listed" "$STATE/created" "$KEEP_RECENT" <<'PY'
import os, sys
used = set(os.environ["USED"].split())
tags = {}
for line in open(sys.argv[1]):
    iid, ref = line.split()
    tags.setdefault(iid, []).append(ref)
created = dict(line.split(None, 1) for line in open(sys.argv[2]))
keep_recent = int(sys.argv[3])
order = sorted(tags, key=lambda i: created.get(i, ""), reverse=True)
unused = [i for i in order if i not in used]
recent = set(unused[:keep_recent])
for iid in order:
    when = created.get(iid, "?").strip()[:10]
    for ref in sorted(tags[iid]):
        if ref.endswith(":<none>"):
            ref = iid  # untagged: remove by id
        if iid in used:
            print("keep %s %s %s used-by-a-container" % (iid, ref, when))
        elif iid in recent:
            print("keep %s %s %s recent-rollback" % (iid, ref, when))
        else:
            print("remove %s %s %s old-unused" % (iid, ref, when))
PY
}

unique_bytes() {  # sum of Docker's "unique size" for the image ids on stdin
    ids=$(cat)
    docker system df -v --format '{{json .}}' 2>/dev/null | IDS="$ids" python3 -I -c '
import json, os, re, sys
ids = set(os.environ["IDS"].split())
units = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}
def size(s):
    m = re.match(r"([\d.]+)\s*([kKMGT]?B)", s or "")
    return float(m.group(1)) * units[m.group(2).upper()] if m else 0
try:
    d = json.loads(sys.stdin.read())
except ValueError:
    print(0); sys.exit()
total = 0
for img in d.get("Images") or []:
    iid = img.get("ID", "")
    if any(i.endswith(iid.replace("sha256:", "")) or i == iid for i in ids):
        total += size(img.get("UniqueSize"))
print(int(total))'
}

cache_size() {
    docker system df --format '{{.Type}} {{.Size}}' | awk '$1 == "Build" {print $3}'
}

cmd_plan() {
    select_images > "$STATE/plan.txt"
    say "disk /: $(free_gb)"
    say "production app images to KEEP:"
    grep '^keep ' "$STATE/plan.txt" | awk '{printf "  %s  %s  (%s)\n", $4, $3, $5}'
    n=$(grep -c '^remove ' "$STATE/plan.txt")
    say "production app images to REMOVE: $n"
    grep '^remove ' "$STATE/plan.txt" | awk '{printf "  %s  %s\n", $4, $3}'
    est=$(grep '^remove ' "$STATE/plan.txt" | awk '{print $2}' | sort -u | unique_bytes)
    say "space from those images: at least $(awk -v b="${est:-0}" 'BEGIN {if (b >= 1e9) printf "%.2f GB", b / 1e9; else printf "%.0f MB", b / 1e6}') (layers shared only among them are freed too)"
    say "build cache to REMOVE: $(cache_size)"
    say "not touched: containers, volumes, data, and images of any other repository (staging, CI, base images)"
    say "Nothing was changed. Run 'apply' to remove exactly this list."
}

cmd_apply() {
    [ -s "$STATE/plan.txt" ] || { say "refused: run 'plan' first"; exit 2; }
    select_images > "$STATE/now.txt"
    if ! cmp -s "$STATE/plan.txt" "$STATE/now.txt"; then
        say "refused: the images changed since the plan; run 'plan' again and review it"
        diff "$STATE/plan.txt" "$STATE/now.txt" | sed -n 's/^[<>] /  /p' | head -n 10
        exit 2
    fi
    say "before: $(free_gb)"
    removed=0 failed=0
    for ref in $(grep '^remove ' "$STATE/plan.txt" | awk '{print $3}'); do
        if docker image rm "$ref" > /dev/null 2>"$STATE/err"; then
            removed=$((removed + 1))
        else
            failed=$((failed + 1))
            say "  not removed: $ref ($(head -c 160 "$STATE/err"))"
        fi
    done
    say "images removed: $removed, not removed: $failed"
    if docker builder prune -a -f > "$STATE/prune.txt" 2>&1; then
        say "build cache: $(grep -i 'total' "$STATE/prune.txt" | tail -n 1)"
    else
        say "build cache prune failed: $(tail -n 1 "$STATE/prune.txt")"
        failed=$((failed + 1))
    fi
    say "after:  $(free_gb)"
    say "production containers:"
    docker ps --filter label=com.docker.compose.project=nexus-trading-bot --format '  {{.Names}}: {{.Status}}'
    rm -f "$STATE/plan.txt"
    [ "$failed" = 0 ] && say "DONE" || { say "DONE WITH PROBLEMS (see above)"; exit 3; }
}

case "${1:-}" in
    plan) cmd_plan ;;
    apply) cmd_apply ;;
    *) say "usage: docker-cleanup.sh plan|apply"; exit 64 ;;
esac
