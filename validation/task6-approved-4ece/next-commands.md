# Fresh manual release build for approved 4eceb97

The user approved replacing 0ded1cc with exactly `4eceb973e98e1350dd625153753637629453c401`. The new release ref has been created and independently verified. Keep the existing RC3 staging target and preservation boundaries. No new project or trading execution is authorized.

Open https://github.com/Arjuunb/nexus-trading-bot/actions/workflows/staging-release.yml and choose **Run workflow**, creating a fresh run with these exact fields:

| Field | Value |
| --- | --- |
| Use workflow from | `release/strategy-intelligence-task6-4eceb97` |
| reviewed_commit | `4eceb973e98e1350dd625153753637629453c401` |
| expected_source_sha256 | `82a41d03d5c049051d740d154a6c941efae01b4370f4b6d167b7167b54582ea8` |

Click **Run workflow** once and retain the new run URL. Do not select main/development head or use Re-run jobs: the immutable release guard requires a fresh manual first attempt. The connected GitHub tools cannot perform this dispatch.

Equivalent command from an already authenticated GitHub CLI:

```bash
gh workflow run staging-release.yml --repo Arjuunb/nexus-trading-bot \
  --ref release/strategy-intelligence-task6-4eceb97 \
  --field reviewed_commit=4eceb973e98e1350dd625153753637629453c401 \
  --field expected_source_sha256=82a41d03d5c049051d740d154a6c941efae01b4370f4b6d167b7167b54582ea8
```

This workflow validates, builds, scans and requests guarded image publication. It has no VPS deployment job. Keep the existing distinct-reviewer publication protection; chat approval is not a GitHub environment review. No image or scan success may be inferred until actual jobs and sealed image identities are verified.

Read-only VPS inventory remains available at `../task6-approved-0ded/vps-readonly-preflight.sh`. Do not run Compose recreation, change images or restart production while image proof and preservation checks remain pending. The API override at `intelligence-recovery-20261018/compose.memory.yaml` must remain last, with memory and memory-plus-swap both 1536 MiB. All existing credentials and data stay in place. Do not paste private keys, tokens or passwords into chat.
