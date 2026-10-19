#!/usr/bin/env bash
# Inventory only. Does not exec into containers or read logs, env files or databases.
set -euo pipefail

date -u '+recorded_at=%Y-%m-%dT%H:%M:%SZ'
docker compose ls --format json
for nexus_project in nexus-remote-stage-rc3-28acdbc nexus-trading-bot; do
    nexus_ids="$(docker ps -aq --filter "label=com.docker.compose.project=$nexus_project")"
    if [ -z "$nexus_ids" ]; then
        printf 'project=%s containers=NOT_FOUND\n' "$nexus_project"
        continue
    fi
    while IFS= read -r nexus_container_id; do
        docker inspect --format 'ID={{.Id}} Name={{.Name}} Project={{index .Config.Labels "com.docker.compose.project"}} Service={{index .Config.Labels "com.docker.compose.service"}} Image={{.Image}} Status={{.State.Status}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} Started={{.State.StartedAt}} Restarts={{.RestartCount}} OOM={{.State.OOMKilled}} Memory={{.HostConfig.Memory}} Swap={{.HostConfig.MemorySwap}} ConfigFiles={{index .Config.Labels "com.docker.compose.project.config_files"}} Mounts={{json .Mounts}}' "$nexus_container_id"
        nexus_image_id="$(docker inspect --format '{{.Image}}' "$nexus_container_id")"
        docker image inspect --format 'ImageID={{.Id}} Digests={{json .RepoDigests}} Revision={{index .Config.Labels "org.opencontainers.image.revision"}} SourceSHA={{index .Config.Labels "nexus.source.sha256"}} OS={{.Os}} Arch={{.Architecture}}' "$nexus_image_id"
    done <<< "$nexus_ids"
done
free -m
df -h /
