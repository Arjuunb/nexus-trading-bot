#!/usr/bin/env bash
# Everything that keeps being pasted by hand after a deploy, in one command.
#
# It exists because hand-pasted blocks kept failing on things a script can just
# look up: the repository path (guessed wrong once), a shell variable that does
# not survive a dropped SSH session (lost once), and a line meant for a laptop
# pasted into the server (once). This script derives its own repository root, so
# running it at all means the path is right, and it prints the one command that
# belongs on your own machine clearly separated at the end rather than mixed in.
#
#   bash /opt/nexus-trading-bot/scripts/pa_status.sh
#
# Read-only with one exception: it copies the rendered review out of the
# container to your home directory. It never deploys, prunes or restarts.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

APP_SERVICE="${APP_SERVICE:-app}"
AUDIT="${AUDIT:-/var/lib/tradexa/research/y2025.json}"
REVIEW_IN_CONTAINER="${REVIEW_IN_CONTAINER:-/var/lib/tradexa/research/review_2025.html}"
REVIEW_OUT="${REVIEW_OUT:-$HOME/review_2025.html}"

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "docker is not on PATH. Are you on the VPS?"
[ -f "$REPO_ROOT/compose.yaml" ] || fail "no compose.yaml in $REPO_ROOT"

CID="$(docker compose ps -q "$APP_SERVICE" 2>/dev/null || true)"
[ -n "$CID" ] || fail "the '$APP_SERVICE' container is not running. Run: bash scripts/deploy.sh"

step "Deployed build"
LOCAL_HEAD="$(git rev-parse HEAD)"
RUNNING="$(docker compose exec -T "$APP_SERVICE" printenv GIT_COMMIT 2>/dev/null | tr -d '\r\n' || true)"
echo "  checkout  $LOCAL_HEAD"
echo "  running   ${RUNNING:-unknown}"
if [ -n "$RUNNING" ] && [ "$RUNNING" != "$LOCAL_HEAD" ]; then
    # Saying this plainly matters: a stale image once made /health report a
    # commit the container did not contain, for two weeks.
    echo "  NOTE: the running image is not this checkout. Run: bash scripts/deploy.sh"
fi

step "Journal guard"
docker compose exec -T "$APP_SERVICE" python scripts/pa_journal_guard_check.py

step "Journal churn"
docker compose exec -T "$APP_SERVICE" python scripts/pa_journal_churn.py --top 20

step "Rulebook review"
if docker compose exec -T "$APP_SERVICE" test -f "$AUDIT"; then
    docker compose exec -T "$APP_SERVICE" python scripts/pa_rulebook_review.py \
        "$AUDIT" -o "$REVIEW_IN_CONTAINER" --text
    docker cp "$CID:$REVIEW_IN_CONTAINER" "$REVIEW_OUT"
    echo
    echo "  copied to $REVIEW_OUT"
    printf '\n\033[1m== Run this on YOUR OWN computer, not here ==\033[0m\n'
    echo "  (if your prompt says ubuntu@vps-..., type 'exit' first or open a new tab)"
    echo
    echo "  scp -i ~/.ssh/id_ed25519 ubuntu@$(hostname -I 2>/dev/null | awk '{print $1}'):$REVIEW_OUT ~/Desktop/"
    echo "  open ~/Desktop/$(basename "$REVIEW_OUT")"
else
    echo "  no audit at $AUDIT -- the replay has not produced one yet."
    echo "  Start it with:"
    echo "    docker compose exec -T $APP_SERVICE mkdir -p \"\$(dirname $AUDIT)\""
    echo "    docker run -d --name pa-replay-2025 --volumes-from \"$CID\" \\"
    echo "      --env-file $REPO_ROOT/.env -e HUB_DATA_DIR=/var/lib/tradexa \\"
    echo "      \"\$(docker inspect -f '{{.Config.Image}}' $CID)\" \\"
    echo "      python scripts/pa_rulebook_replay.py --symbol BTCUSDT --bars 200000 \\"
    echo "        --start 2025-01-01 --end 2026-01-01 --progress --checkpoint-every 2000 \\"
    echo "        --audit $AUDIT"
fi
