# Next step — inspect existing VPS bindings for direct archive staging

The user has no second reviewer. GHCR publication remains failed and its protection is unchanged. The existing Task 6 authorization separately covers bounded deployment to the existing RC3 staging project; source `4eceb973e98e1350dd625153753637629453c401` was explicitly approved. Use the already built/tested/scanned image archives directly, with immutable local config IDs. The release workflow explicitly separates publication from transfer/startup, and this project supports immutable local image IDs. No new source, build, fake review or registry write is needed for this selected route.

Preparation is in `direct-archive/`: preserved-service image overlay, pinned zip/export checksums, verified exported manifest digests and ordered preservation/deployment/acceptance gates. All checksums and overlay structure were checked offline. Actual Docker Compose rendering and VPS deployment are still pending. The second-reviewer requirement continues to apply if registry publication is requested in the future.

## First operator command — read-only locations only

Paste this in the existing `ubuntu@vps-46469a44` SSH terminal and return its output:

```bash
docker inspect nexus-remote-stage-rc3-28acdbc-api-1 \
  --format 'WorkingDir={{index .Config.Labels "com.docker.compose.project.working_dir"}} ConfigFiles={{index .Config.Labels "com.docker.compose.project.config_files"}} EnvFiles={{index .Config.Labels "com.docker.compose.project.environment_file"}}'
```

This reads only Compose configuration paths. It does not print environment/credential values or change containers. The existing environment-file input must be located and preserved before rendering or updating services. Do not paste credentials, private keys or tokens.

Continue by capturing the full current read-only staging/production baseline (IDs/images/starts/health/resources/users/mounts), checking effective configuration and staging backups/migrations, then verifying the transferred exact artifact and loaded image IDs. The image overlay belongs after the existing customer overlay and **before** the retained 1536 MiB API memory override, which remains last. No service update is ready to run yet. Keep all volumes, customer workspace and credentials, no new paper execution, trading, promotion, feed activation or production replacement.

The prior publication-only instructions are retained in `direct-archive/publication-route-history.md` as history, not a prerequisite for this selected staging route. Task 6 remains incomplete until actual VPS deployment and all acceptance/recovery/isolation gates pass.
