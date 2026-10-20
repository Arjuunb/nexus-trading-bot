# Next step — inspect existing VPS bindings for direct archive staging

The user has no second reviewer. GHCR publication remains failed and its protection is unchanged. The existing Task 6 authorization separately covers bounded deployment to the existing RC3 staging project; source `4eceb973e98e1350dd625153753637629453c401` was explicitly approved. Use the already built/tested/scanned image archives directly, with immutable local config IDs. The release workflow explicitly separates publication from transfer/startup, and this project supports immutable local image IDs. No new source, build, fake review or registry write is needed for this selected route.

Preparation is in `direct-archive/`: preserved-service image overlay, pinned zip/export checksums, verified exported manifest digests and ordered preservation/deployment/acceptance gates. All checksums and overlay structure were checked offline. Actual Docker Compose rendering and VPS deployment are still pending. The second-reviewer requirement continues to apply if registry publication is requested in the future.

## Existing bindings identified; next capture current baseline

The operator is connected to the VPS and returned the existing API labels:

- Working directory: `/home/ubuntu/nexus-rc3-staging-28acdbc/release/ops`
- Base configuration: `/home/ubuntu/nexus-rc3-staging-28acdbc/release/ops/staging.alpine.compose.yaml`
- Customer overlay: `/home/ubuntu/nexus-rc3-staging-28acdbc/release/customer-workspace/compose.customer-workspace.yaml`
- Existing environment file: `/home/ubuntu/nexus-rc3-staging-28acdbc/private/compose.env`

Recorded in `vps-operator-bindings-20261020.txt`; no credential values were read. The memory overlay is absent from the labels and must be included last before any recreation. Effective configuration remains unverified.

Run the pinned `vps-preservation-preflight.py` helper in the same VPS terminal. It uses only `docker ps`, `docker inspect`, `docker image inspect` and `docker compose config`. It handles absent health keys safely, captures both projects' identities and mounts, renders the existing binding files with the memory override last, and compares configured environment, users and mounts with the running stage services. It prints only selected controls and differing environment **key names**, never credential values. Shell `NEXUS_`, `HUB_` and `COMPOSE_` overrides are excluded from the render so they cannot replace the identified file bindings.

The assistant supplies an immutable raw-GitHub URL and this helper's SHA-256 after pushing the evidence commit. Download into a new temporary directory, verify the SHA-256, then run with `python3` only if verification succeeds. Return the sanitized output. No service startup, restart, database access, backup, image transfer or load is performed by this helper. Its synthetic redaction/missing-health/memory/order checks passed locally (`preflight-helper-checks.json`); actual VPS execution remains pending.

Then verify staging backup/migration state, exact transferred artifacts and loaded image IDs. The image overlay belongs after the existing customer overlay and **before** the retained 1536 MiB API memory override, which remains last. No service update is ready yet. Preserve the observed API `tradexa` and worker `100:100` runtime users, all data and execution boundaries.

The prior publication-only instructions are retained in `direct-archive/publication-route-history.md` as history, not a prerequisite for this selected staging route. Task 6 remains incomplete until actual VPS deployment and all acceptance/recovery/isolation gates pass.
