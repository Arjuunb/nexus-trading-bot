#!/usr/bin/env bash
# Read-only. Does not read environment values, logs or databases, or change services.
set -euo pipefail
date -u
for nexus_project in nexus-remote-stage-rc3-28acdbc nexus-trading-bot; do
  docker ps -aq --filter "label=com.docker.compose.project=$nexus_project" |
    xargs -r docker inspect --format 'ID={{.Id}} Name={{.Name}} Project={{index .Config.Labels "com.docker.compose.project"}} Service={{index .Config.Labels "com.docker.compose.service"}} Image={{.Image}} Status={{.State.Status}} Health={{with index .State "Health"}}{{.Status}}{{else}}none{{end}} Started={{.State.StartedAt}} Restarts={{.RestartCount}} OOM={{.State.OOMKilled}} User={{.Config.User}} Memory={{.HostConfig.Memory}} Swap={{.HostConfig.MemorySwap}} WorkingDir={{index .Config.Labels "com.docker.compose.project.working_dir"}} ConfigFiles={{index .Config.Labels "com.docker.compose.project.config_files"}} EnvFiles={{index .Config.Labels "com.docker.compose.project.environment_file"}} Mounts={{json .Mounts}}'
done
free -m
df -h /
