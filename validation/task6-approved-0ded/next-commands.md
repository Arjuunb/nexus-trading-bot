# Task 6 continuation — exact approved source, existing RC3 project

**2026-10-20: do not repeat the unchanged 0ded release command below.** The correct manual run [39499761284](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39499761284) failed in the browser launcher before tests. Backend suites and disposable PostgreSQL tests passed; image build/publication were skipped. No deployment or live acceptance has run.

A tested replacement source, `4eceb973e98e1350dd625153753637629453c401`, is prepared but requires replacement of the user's exact-source approval. See `acceptance-report.md` and `release-run-39499761284.json`. Keep the 0ded release ref unchanged. The historical dispatch command is retained below as a receipt, not a retry instruction. Read-only VPS inventory remains useful.

## Historical exact-source dispatch — completed, failed before build

The publication workflow is now present on GitHub's default branch. The earlier missing-workflow blocker is resolved. The new release branch was created and independently read back at exactly `0ded1cc4cb77268cd4120224fa1f0d629589bd6b`:

`release/strategy-intelligence-task6-0ded1cc`

The operator has completed the equivalent dispatch:

```bash
gh workflow run staging-release.yml --repo Arjuunb/nexus-trading-bot \
  --ref release/strategy-intelligence-task6-0ded1cc \
  --field reviewed_commit=0ded1cc4cb77268cd4120224fa1f0d629589bd6b \
  --field expected_source_sha256=b58399c346abb3416579cade8dd2df159a7bb197cfb0e1c36ef1dad7d0045d9c
```

The original alternative was [the release workflow](https://github.com/Arjuunb/nexus-trading-bot/actions/workflows/staging-release.yml), select that release branch and enter the exact commit and source hash above. This build workflow has no SSH or staging-deployment job. Its checks, scans and protected publication guards must pass. Do not select main or the later development-branch head as the deployment source. Failures must be retained; do not edit the approved source or weaken guards to make the run pass.

The operator dispatched the workflow through GitHub Actions. Ordinary CI run 39333631513 also completed successfully, but neither result supplies release images. The manual run failed at the browser launcher; no image build, scan, publication or deployment success is claimed.

## Read-only target inventory (operator baseline received)

Run `vps-readonly-preflight.sh` from this directory on the existing VPS. It uses Docker inventory/inspection only, plus host memory and disk reporting. It does not restart containers, change resources, read environment values/logs/credentials/financial data, execute container processes, or write remote files. Its production snapshot is a baseline only; a before/after comparison still requires a second real capture after an authorized staging change. Local `bash -n` checks syntax only, not target behavior.

For an immediate shorter inventory, paste this in the already-open `ubuntu@vps-46469a44` SSH terminal:

```bash
docker compose ls
for nexus_project in nexus-remote-stage-rc3-28acdbc nexus-trading-bot; do
  docker ps -aq --filter "label=com.docker.compose.project=$nexus_project" |
    xargs -r docker inspect --format 'Name={{.Name}} Image={{.Image}} Status={{.State.Status}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} Started={{.State.StartedAt}} Restarts={{.RestartCount}} OOM={{.State.OOMKilled}} Memory={{.HostConfig.Memory}} Swap={{.HostConfig.MemorySwap}}'
done
free -m
df -h /
```

Return only the command output. Do not paste private keys, tokens, dotenv content or full unfiltered Docker inspect/config output.

## Deployment gate remains closed

No service update command is supplied before actual target inventory, exact-source image scan/digest evidence, existing credentials/mounts/migrations and recovery verification. The only authorized target is `nexus-remote-stage-rc3-28acdbc`; preserve the customer workspace and existing volumes. Its API limit must remain **1536 MiB = 1610612736 bytes**, with the existing memory overlay last. No public feed, real-money or new-paper execution, automatic promotion, production update or new feature sprint is authorized.

## Latest verified operator results

Existing API source is `f8778ed`, worker source is RC3 `28acdbc`. Both initializers exited 0 and the retained memory overlay contains 1536m for both limits. The initial inventory and identity commands have now been supplied by the operator; do not repeat them without a reason. Next start the exact-source build workflow above. Mounts/volume isolation, existing credentials, effective merged Compose configuration and rollback/recovery evidence remain to be checked before any service update.
