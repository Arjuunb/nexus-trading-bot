# Current next step — correct environment protection before a fresh run

The build/tests/scans in [run 39503485769](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39503485769) passed, but publication is now **FAILED**. The exact guard reason is `PROTECTED_ENVIRONMENT_WITH_DISTINCT_USER_REVIEWER_REQUIRED` (exit 2 at 2026-10-20T22:17:11Z). Registry login and push were skipped. The sealed tested/scanned images remain retained below; nothing was deployed.

## Exact protection blocker

GitHub lists only **Arjuunb** as reviewer, also actor/triggering actor (ID 190428336). Actual approval history records that same user approving the run. The approved `ops/github-release.py` guard requires:

1. A trusted **different individual GitHub user** configured as a required reviewer.
2. **Prevent self-review** enabled.
3. An enabled deployment branch policy permitting the intended approved release ref.

The actual configuration guard failed before registry authentication. Full environment settings cannot be read through the connected fetch endpoint, so the assistant cannot identify which combination of reviewer/self-review/branch-policy settings is missing. The recorded initiator approval also cannot satisfy the distinct-reviewer history requirement.

## Next user action

Open repository **Settings → Environments → ghcr-staging-release**. Configure the distinct required reviewer and self-review prevention. Retain a restricted deployment branch policy permitting `release/strategy-intelligence-task6-4eceb97`. Check those settings before starting another workflow; a screenshot of the protection controls is sufficient to inspect without exposing credentials. No invitation/message has been sent by the assistant.

After protection is correct, start a **fresh manual** `staging-release.yml` run with the same approved ref/commit/source hash below, then have the configured distinct reviewer inspect and approve that new run. **Do not use Re-run jobs:** the approved source requires manual attempt 1. Do not remove/weaken the protection or forge review metadata. The connected GitHub tools expose no fresh workflow dispatch. The prior chat source/deployment approval remains valid; another generic approval is unnecessary.

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

These config IDs identify built images, not published registry digests or deployed VPS images. The failed publication made no registry write. A future fresh run must verify its own actual sealed bytes and approval metadata before publication. Registry references remain unavailable.

## Target work after publication

Use only existing project `nexus-remote-stage-rc3-28acdbc` and existing release directory `/home/ubuntu/nexus-rc3-staging-28acdbc/release`. Actual target SSH is blocked from this workspace; retain the existing trusted operator SSH session and do not paste keys/tokens/passwords into chat.

The read-only snapshot script remains `../task6-approved-0ded/vps-readonly-preflight.sh`. Capture full staging/production container IDs, images, starts, resources and mounts before any update. Verify effective Compose overlays, existing credential bindings, persistent data, migration state and restorable backups. Preserve customer workspace and volumes. Place `intelligence-recovery-20261018/compose.memory.yaml` last, with API memory and memory-plus-swap both **1536 MiB / 1610612736 bytes**.

No Compose recreation, new execution, production restart, data reset or automatic promotion is authorized by an image publication review. The original Task 6 bounded staging deployment authorization remains valid once actual prerequisites pass. Full authenticated PRD, real insufficient-evidence abstention, PostgreSQL/restart/resource/recovery and production-isolation acceptance still must be performed. Task 6 remains incomplete.
