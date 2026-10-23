#!/bin/sh
# Replica containers for testing p1b_stop_staging_api.sh: same names, labels, volumes,
# restart policies and start command as the VPS containers (P0 evidence, 2026-10-23).
#
# DISPOSABLE DOCKER HOSTS ONLY. It removes and recreates containers with the production and
# staging names, so it refuses to run unless every such container and volume is a test replica.
#
#   P1B_REPLICA_TEST=disposable sh setup.sh [staging SUPABASE_URL] [staging revision label]
set -u
STAGE=nexus-remote-stage-rc3-28acdbc-api-1
PROD=nexus-trading-bot-app-1
STAGE_VOL=nexus-remote-stage-rc3-28acdbc_api-state
PROD_VOL=nexus-migration-tradexa-data-cutover-20260913
STAGE_URL=${1-https://stagingref.supabase.co}
REV=${2:-f8778ed42039d415f3829e08a05225976fe0d0a5}

[ "${P1B_REPLICA_TEST:-}" = disposable ] || { echo "refused: set P1B_REPLICA_TEST=disposable (never on the VPS)"; exit 3; }
for c in "$STAGE" "$PROD"; do
    img=$(docker inspect -f '{{.Config.Image}}' "$c" 2>/dev/null) || continue
    case "$img" in p1b-stage-api:test|p1b-prod-app:test) ;; *) echo "refused: $c is not a test replica ($img)"; exit 3 ;; esac
done
for v in "$STAGE_VOL" "$PROD_VOL"; do
    docker volume inspect "$v" >/dev/null 2>&1 || continue
    [ "$(docker volume inspect -f '{{index .Labels "p1b.replica-test"}}' "$v")" = 1 ] \
        || { echo "refused: volume $v was not created by this test"; exit 3; }
done

docker rm -f "$STAGE" "$PROD" >/dev/null 2>&1
docker volume rm "$STAGE_VOL" >/dev/null 2>&1
docker volume create --label p1b.replica-test=1 "$STAGE_VOL" >/dev/null
docker volume inspect "$PROD_VOL" >/dev/null 2>&1 || docker volume create --label p1b.replica-test=1 "$PROD_VOL" >/dev/null
docker network create nexus-trading-bot_default >/dev/null 2>&1
docker network create nexus-remote-stage-rc3-28acdbc_ingress >/dev/null 2>&1

docker run -d --name "$PROD" --network nexus-trading-bot_default --restart unless-stopped \
  --label com.docker.compose.project=nexus-trading-bot --label com.docker.compose.service=app \
  -e SUPABASE_URL=https://ProdRef.supabase.co/ -e SUPABASE_KEY=secret-prod-key -e PORT=8000 \
  -v "$PROD_VOL":/var/lib/tradexa \
  --health-cmd "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)\"" \
  --health-interval 2s p1b-prod-app:test >/dev/null

set -- --name "$STAGE" --network nexus-remote-stage-rc3-28acdbc_ingress \
  --restart on-failure:2 --cpus 1 --memory 1536m --memory-swap 1536m --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --label com.docker.compose.project=nexus-remote-stage-rc3-28acdbc --label com.docker.compose.service=api \
  --label org.opencontainers.image.revision="$REV" \
  -e SUPABASE_KEY=secret-staging-key -e HUB_DATA_DIR=/var/lib/tradexa \
  -v "$STAGE_VOL":/var/lib/tradexa
[ -n "$STAGE_URL" ] && set -- "$@" -e SUPABASE_URL="$STAGE_URL"
if [ -n "${P1B_STAGE_CMD:-}" ]; then   # e.g. a process that never exits on SIGTERM
    docker run -d "$@" p1b-stage-api:test sh -c "$P1B_STAGE_CMD" >/dev/null
else
    docker run -d "$@" p1b-stage-api:test >/dev/null
fi
sleep 4
