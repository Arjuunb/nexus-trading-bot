#!/usr/bin/env bash
# Existing staging inventory only; no container execution or service mutation.
set -euo pipefail
for nexus_service in api research-worker; do
    nexus_image_id="$(docker inspect --format '{{.Image}}' "nexus-remote-stage-rc3-28acdbc-${nexus_service}-1")"
    docker image inspect "$nexus_image_id" --format 'Image={{.Id}} Revision={{index .Config.Labels "org.opencontainers.image.revision"}} Source={{index .Config.Labels "nexus.source.sha256"}} Digests={{json .RepoDigests}}'
done
docker inspect nexus-remote-stage-rc3-28acdbc-state-init-1 nexus-remote-stage-rc3-28acdbc-metadata-init-1 --format 'Name={{.Name}} Status={{.State.Status}} Exit={{.State.ExitCode}} Finished={{.State.FinishedAt}}'
sed -n '/^[[:space:]]*api:/p; /^[[:space:]]*mem_limit:/p; /^[[:space:]]*memswap_limit:/p' /home/ubuntu/nexus-rc3-staging-28acdbc/release/intelligence-recovery-20261018/compose.memory.yaml
