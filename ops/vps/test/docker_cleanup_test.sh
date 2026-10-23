#!/bin/sh
# Test of ops/vps/operations/docker-cleanup.sh. DISPOSABLE DOCKER HOSTS ONLY: it removes images.
#   NEXUS_CLEANUP_TEST=disposable sh ops/vps/test/docker_cleanup_test.sh     (from the repository root)
set -u
[ "${NEXUS_CLEANUP_TEST:-}" = disposable ] || { echo "refused: set NEXUS_CLEANUP_TEST=disposable (never on the VPS)"; exit 3; }
FAILS=0
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
has() { if printf '%s' "$2" | grep -q -- "$3"; then ok "$1"; else bad "$1 (no '$3')"; fi; }
present() { docker image inspect "$1" >/dev/null 2>&1; }
S=ops/vps/operations/docker-cleanup.sh
B=$(mktemp -d)
DOCKER_CLEANUP_TEST_STATE="$B/state"
export DOCKER_CLEANUP_TEST_STATE

for c in nexus-trading-bot-app-1 pa-replay-2025; do
    img=$(docker inspect -f '{{.Config.Image}}' "$c" 2>/dev/null) || continue
    case "$img" in nexus-trading-bot-app:release-*) docker rm -f "$c" >/dev/null ;; *) echo "refused: $c is not a test container"; exit 3 ;; esac
done
docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -E '^nexus-(trading-bot-app|ci-api|ci-worker):' \
    | grep -E ':(release-[0-9]|dev|candidate)$' | xargs -r docker image rm -f >/dev/null 2>&1
for n in 1 2 3 4 5 6; do   # six "releases", oldest first
    mkdir -p "$B/v$n"
    printf 'FROM python:3.12-alpine\nRUN head -c 3000000 /dev/urandom > /blob-%s\nCMD ["sleep","600"]\n' "$n" > "$B/v$n/Dockerfile"
    docker build -q -t "nexus-trading-bot-app:release-$n" "$B/v$n" >/dev/null; sleep 1.2
done
docker tag nexus-trading-bot-app:release-2 nexus-trading-bot-app:dev
mkdir -p "$B/ci"; printf 'FROM python:3.12-alpine\nRUN echo ci > /ci\n' > "$B/ci/Dockerfile"
docker build -q -t nexus-ci-worker:candidate "$B/ci" >/dev/null       # another repository, unused
docker run -d --name nexus-trading-bot-app-1 --label com.docker.compose.project=nexus-trading-bot \
    nexus-trading-bot-app:release-4 >/dev/null                         # running
docker create --name pa-replay-2025 nexus-trading-bot-app:release-1 >/dev/null   # stopped

out=$(sh "$S" apply); has "apply refuses without a plan" "$out" "run 'plan' first"
out=$(sh "$S" plan); echo "$out" | sed 's/^/      /'
has "plan keeps the newest unused (rollback)" "$out" "release-6  (recent-rollback)"
has "plan keeps the second newest unused (rollback)" "$out" "release-5  (recent-rollback)"
has "plan keeps the running container's image" "$out" "release-4  (used-by-a-container)"
has "plan keeps the stopped container's image" "$out" "release-1  (used-by-a-container)"
has "plan removes 3 references (release-3, release-2 and its dev tag)" "$out" "REMOVE: 3"
has "plan estimates the space (2 distinct images x ~6 MB)" "$out" "at least 12 MB"
present nexus-trading-bot-app:release-3 && ok "plan changed nothing" || bad "plan removed something"

docker tag nexus-trading-bot-app:release-3 nexus-trading-bot-app:extra-tag
out=$(sh "$S" apply); has "apply refuses when images changed since the plan" "$out" "changed since the plan"
docker image rm nexus-trading-bot-app:extra-tag >/dev/null
sh "$S" plan >/dev/null
out=$(sh "$S" apply); echo "$out" | sed 's/^/      /'
has "apply removed the 3 references" "$out" "images removed: 3, not removed: 0"
for i in release-1 release-4 release-5 release-6; do present "nexus-trading-bot-app:$i" && ok "kept $i" || bad "lost $i"; done
for i in release-2 release-3 dev; do present "nexus-trading-bot-app:$i" && bad "still has $i" || ok "removed $i"; done
present nexus-ci-worker:candidate && ok "other repository untouched" || bad "other repository image removed"
[ "$(docker inspect -f '{{.State.Running}}' nexus-trading-bot-app-1)" = true ] && ok "running container still running" || bad "running container affected"
docker inspect pa-replay-2025 >/dev/null 2>&1 && ok "stopped container still there" || bad "stopped container removed"
has "build cache removed" "$(docker system df --format '{{.Type}} {{.Size}}')" "Build Cache 0B"
out=$(sh "$S" apply); has "plan is used only once" "$out" "run 'plan' first"

docker rm -f nexus-trading-bot-app-1 pa-replay-2025 >/dev/null
rm -rf "$B"
echo "failures: $FAILS"
[ "$FAILS" = 0 ]
