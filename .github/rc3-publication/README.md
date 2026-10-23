# Separately authorized RC3 publication

The user explicitly approved GHCR publication after reviewing build-and-scan run
38767616216. This controller publishes those exact retained API/worker images,
without a rebuild. This is a separate authorization from the historical build
evidence, whose publication-authorized fields remain false and unchanged.

The workflow runs only on the dedicated `ci/rc3-isolated-image-build` branch and
uses the existing named GitHub environment `ghcr-staging-release`. GitHub's
configured environment protections remain in effect. No environment or package
visibility setting is changed by this controller. The authorization record states
direct user approval; it does not claim a distinct configured reviewer approved
the run or forge an approval accepted by the older staging-release guard.

Accepted source: `28acdbccc4f9b737c683d94e15bb16a09e50aa85`, canonical SHA256
`3ba76e44d9181ee1a8910667be645bfa477a7b3e4055c30538a29030e83caa4d`.
Original archive and evidence artifact IDs: 11989463144 and 11989598060.
The successful original build and archive verification runs, source manifest,
scan reports, archive SHA256, config IDs and every layer digest are checked.
Both destination tags are checked before either image is pushed. A tag with a
different config ID fails closed and is never overwritten.

Only these two RC3-specific destinations are used:

- `ghcr.io/arjuunb/nexus-trading-bot-api:rc3-28acdbccc4f9-build-38767616216`
- `ghcr.io/arjuunb/nexus-trading-bot-worker:rc3-28acdbccc4f9-build-38767616216`

Authentication uses the disposable workflow token with `packages: write`;
credentials stay in a dedicated temporary Docker configuration and are removed.
The actual registry manifest digest is captured for each image. Each registry
manifest must reference the accepted config ID. A subsequent pull by digest must
yield the same config ID, source labels, platform and uncompressed layer hashes.
Publication evidence is retained for 30 days, including partial/failure states.

No image application/worker entrypoint runs. No production host is contacted.
No SSH, deployment, package visibility mutation, trading activation, production
service change, `latest` tag or production release alias is included. Existing
Stage A worker activation limitations remain. No deployment/configuration packet
is generated and publication cannot authorize later deployment.

Local validation:

```bash
python3 .github/rc3-publication/test_publish.py
actionlint .github/workflows/rc3-publish-scanned-images.yml
python3 .github/rc3-publication/publish.py verify \
  --archives /path/to/exact/archives --evidence /path/to/original/evidence \
  --out /path/to/verification-evidence
```
