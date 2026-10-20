# Current next step — protected review of the existing built images

**Do not start or retry another build.** Fresh manual [run 39503485769](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39503485769) has passed release checks, image construction, actual-image regressions and security scans. Its publish job is waiting on `ghcr-staging-release`.

## Publication blocker and concrete action

GitHub currently lists only **Arjuunb** as required reviewer. The run actor and triggering actor are also Arjuunb (ID 190428336). The approved `ops/github-release.py` guard requires an actual approval by a configured user whose ID differs from both initiators, with `prevent_self_review` enabled and a deployment branch policy enabled. Self-approval cannot pass this guard.

In repository **Settings → Environments → ghcr-staging-release**:

1. Configure a trusted **different GitHub user** as a required reviewer and enable **Prevent self-review**.
2. Retain an enabled deployment branch policy that permits only the intended approved release ref (`release/strategy-intelligence-task6-4eceb97`, or the existing appropriately restricted release policy).
3. Have that configured reviewer inspect this existing run's tested/scanned packet and approve its pending environment deployment. No message or invitation has been sent by the assistant.

The environment's complete settings cannot be read through the connected fetch endpoint. The pending reviewers and initiators were read from the actual GitHub run and pending-deployments API. Existing reviewer protection must not be removed, bypassed or weakened. Chat source approval is recorded and does not replace the required GitHub environment review.

## Concrete review packet

- Source: `4eceb973e98e1350dd625153753637629453c401`
- Tree: `1af45a4993d83919c5007373ba8b9a27e9de5a71`
- Frozen source SHA-256: `82a41d03d5c049051d740d154a6c941efae01b4370f4b6d167b7167b54582ea8`
- Artifact: `unpublished-staging-images-39503485769-1`, ID **12303500964**, **176365085 bytes**.
- Artifact SHA-256: `bcdcdcab3b3870cad1dbc6913e941979b2ec0b22e69e6726117d35e5bf0bb1da`.
- Artifact expires: **2026-10-27 22:07:45 UTC**.
- API config ID: `sha256:09b524395a9706115b53c8c19e8cc4239b873dcb5450bd3d43700d3bff5f0321`.
- Worker config ID: `sha256:b16074411ac65ed2cc8c0d6981b0608eb25bebdf8445abdad1a04aa8913cda41`.
- Fresh tests: **4553 passed/76 skipped per Python version; PostgreSQL 31 passed/2 skipped; browser 165 passed/0 skipped; actual API image 700 passed/25 skipped**.
- Both image reports: **zero HIGH/CRITICAL vulnerabilities**; source and both image all-severity secret scans: **zero secrets**.
- Independent verification: artifact download hash, all **16** sealed files and both stored/compressed blob and decompressed diff hashes for **12** layers match. Source labels, config IDs and scan image IDs match.

These config IDs identify built images, not published registry digests or deployed VPS images. The publication guard will verify the same sealed bytes again before any registry write. Registry references remain pending.

## Target work after publication

Use only existing project `nexus-remote-stage-rc3-28acdbc` and existing release directory `/home/ubuntu/nexus-rc3-staging-28acdbc/release`. Actual target SSH is blocked from this workspace; retain the existing trusted operator SSH session and do not paste keys/tokens/passwords into chat.

The read-only snapshot script remains `../task6-approved-0ded/vps-readonly-preflight.sh`. Capture full staging/production container IDs, images, starts, resources and mounts before any update. Verify effective Compose overlays, existing credential bindings, persistent data, migration state and restorable backups. Preserve customer workspace and volumes. Place `intelligence-recovery-20261018/compose.memory.yaml` last, with API memory and memory-plus-swap both **1536 MiB / 1610612736 bytes**.

No Compose recreation, new execution, production restart, data reset or automatic promotion is authorized by an image publication review. The original Task 6 bounded staging deployment authorization remains valid once actual prerequisites pass. Full authenticated PRD, real insufficient-evidence abstention, PostgreSQL/restart/resource/recovery and production-isolation acceptance still must be performed. Task 6 remains incomplete.
