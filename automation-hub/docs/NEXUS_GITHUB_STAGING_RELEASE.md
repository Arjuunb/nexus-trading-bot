# GitHub / GHCR staging release preparation

**PREPARED; NOT PUSHED, PUBLISHED, TRANSFERRED OR DEPLOYED. Stage A: NO-GO.**
This is an additive release procedure. It does not replace the existing VPS
preflight, controlled staging plan or human authorization. All command blocks
below are prepared operator instructions; none has been executed against a VPS.
Existing production Compose, services, data, ports, networks and volumes remain
outside this procedure. Historical research, shadow observation and financial
evidence remain separate. No trading or strategy changes are included.

## Source and release identity

The local base is `7dd5e9ab8ad9e300d5d99e72a27bb8aeabc27ae0`, branch `work`.
There were 191 preexisting dirty paths at entry, including required untracked
source. A read-only remote reference query returned default HEAD
`13626a49abc36ae2595925d6503dc0abb3c42b8d`; it differs from the local reviewed
base. No fetch, merge, reset, branch creation, staging, commit or push occurred.
Review ownership and divergence before an authorized release commit. Do not
blindly pull into this checkout or the production checkout.

The preserved prior packet has review identity
`release-review:89bcecfb3bf189f27b99ccab545d811c96ce2ff1304bbefb0488b1544a4af9a2`,
operator-source SHA-256
`75eca8506f48e04cf00e88c403f3165749ad0974b01a19c8858a82ce41cc9a1f`,
and API build-source SHA-256
`e7de53f84034262433699c91588f1b60f8915e82861cbd0fd02de6b73cd44dc1`.
It remains unchanged. New workflow/guard/operator files require a **new** source
review and image build; earlier image config IDs are not GHCR registry digests.
The final prospective source manifest and preservation report are outside Git at
`/workspace/nexus-audit/github-staging-readiness/`. They list exact paths,
untracked requirements, modes, hashes, locks and SQL migration versions.

Proposed branch: `release/nexus-s14-ghcr`.
Proposed tag: `nexus-staging-s14-rc.1`.
Neither exists as an action taken here. The accepted commit is UNKNOWN until
separately authorized source selection, commit and push. Use a reviewed explicit
path list, including required untracked source; do not use `git add .` or commit
credentials, private keys, financial databases, runtime state, node_modules or
virtual environments. The source freezer reuses `services.release_source`,
excludes private/state paths without reading them and rejects tracked private
inputs. A source hash authenticates retained inputs, not bit-identical images.

## GitHub Actions and approval boundary

`release-validation.yml` runs on PR, manual dispatch or reusable workflow call.
It installs hash-locked Python runtime/test dependencies, frozen npm locks,
checks both Python 3.11 and 3.12, builds both real SPAs, runs the root suite,
disposable PostgreSQL concurrency/recovery tests and existing browser tests.
Actions are pinned by full commit; concurrency and execution time are bounded.
GitHub browser installation occurs only on disposable CI runners.

`staging-release.yml` runs only by manual dispatch on a reviewed release branch
or staging tag. The exact commit must equal the selected ref's `GITHUB_SHA` and
the frozen source must match the supplied accepted SHA-256. It builds existing
API/worker Dockerfiles for Linux amd64; worker dependencies are hash verified
and its build has no network. It scans checkout secrets and both actual images
for secrets and HIGH/CRITICAL vulnerabilities, including unfixed findings.
The image archive, source manifest, runtime/source probes, wheel identities and
scan reports form a sealed artifact. Publication downloads its exact artifact
ID and rechecks its hashes and actual image bytes before login.

Repository owners must configure environment **`ghcr-staging-release`** with a
named individual required reviewer, prevent self-review, and a deployment branch
policy allowing only accepted release refs. A different authorized reviewer must
approve the actual run. Configure repository branch/workflow protections and
package permissions; these remote settings are NOT verified here. The guard
checks server-side environment metadata and that run's approval history before
using `GITHUB_TOKEN` for GHCR login. Missing protection, team-only reviewer
configuration, self-approval, bypass without a configured review, stale source,
unavailable approval APIs or rerun attempts fail closed. A failed build/release
requires a **new** dispatch and review, not "Re-run jobs" approval reuse.
The workflow definition must also exist on the repository's default branch for
manual dispatch to be available. Install/accept it through a separately approved
PR; do not replace the remote default branch with the dirty local base. Then
select the exact accepted release ref containing the reviewed source/workflows.
GitHub plan support for required reviewers and the approval API must be verified;
an unavailable feature/API is a publication blocker, not permission to bypass it.

After explicit source push and release-build/publication authorization, this
prepared command requests the run; the publish job still requires the distinct
environment review. **Not executed here.**

```bash
: "${RELEASE_REF:?accepted release/nexus-s14-ghcr or nexus-staging-s14-rc.1}"
: "${RELEASE_COMMIT:?accepted exact commit}"
: "${SOURCE_SHA256:?accepted exact source fingerprint}"
gh workflow run staging-release.yml --repo Arjuunb/nexus-trading-bot \
  --ref "$RELEASE_REF" --field reviewed_commit="$RELEASE_COMMIT" \
  --field expected_source_sha256="$SOURCE_SHA256"
```

The GHCR references will be:

* `ghcr.io/arjuunb/nexus-trading-bot-api@sha256:<actual registry digest>`
* `ghcr.io/arjuunb/nexus-trading-bot-worker@sha256:<actual registry digest>`

Commit-named discovery tags are mutable; deployment always uses full digests.
`publication.json` records registry digests separately from Docker config IDs,
exact source/commit, image UID, upstream image digests, lock/migration/config
hashes and actual reviewer/run references. Actual GHCR digests remain UNKNOWN:
publication has not run. Two image pushes cannot be one registry transaction;
a partial push is not an accepted release. No accepted publication artifact is
produced until both references are verified. There is no automatic registry
cleanup, deployment, SSH credential, VPS secret or trading authorization in CI.
No OCI signature/attestation certification is claimed. Scan reports and source
proofs accompany the images; they do not substitute for human acceptance.

Local security scan found no source secrets. Each retained API/worker image scan found
47 HIGH vulnerability findings (44 OS-package findings and 3 Python findings),
so publication under this policy is **BLOCKED**. Sanitized findings and
full current test evidence are recorded in the accompanying readiness report.
These are scans of preserved prior images; a newly built candidate must be
scanned independently. Remediation needs a reviewed rebuild/retest; findings
are not waived, hidden or removed from the scanner gate by this preparation.

## Staging profile and capacity

The public config packet contains exactly four reviewed operator inputs:
`ops/staging.remote.compose.yaml`, `ops/staging.local.compose.yaml`,
`ops/staging.remote.nginx.conf`, `ops/prepare-staging-bindings.py`, plus
`publication.json` and `packet-integrity.json`. No full repository or private
binding file is transferred in this packet. Images contain required app code.

The remote profile uses new `nexus-remote-stage-*` project-scoped networks and
volumes, private PostgreSQL with separate operator and restricted worker roles,
loopback `127.0.0.1:18443` HTTPS ingress, readonly mounts, health checks, bounded
logs/PIDs/restarts and CPU/RAM limits. It does not merge production Compose.
Trading/live/data/autostart/monitor flags are zero, emergency admin is disabled,
shadow is opt-in and remains off, and no public-feed producer exists.
The binding helper generates five new independent secrets outside the checkout.
It accepts immutable GHCR/upstream digests; it never provisions customer auth or
trusted TLS by pretending a fixture is sufficient. Never copy production dotenv.

Stage A starts only PostgreSQL, API and ingress, after sequential initializers:

| Stage | CPU ceilings | RAM ceilings | Minimum sampled margin |
| --- | ---: | ---: | --- |
| A, steady | 2.25 CPUs | 1,376 MiB / 1.344 GiB | 2.43 CPUs, approximately 4.46 GiB |
| PG + one initializer | 1.5 CPUs | 768 MiB | Lower than Stage A steady ceilings |
| B, A plus research worker | 3.25 CPUs | 1,888 MiB / 1.844 GiB | 1.43 CPUs, approximately 3.96 GiB |

Initializers are each limited to 0.5 CPU, 256 MiB and 64 PIDs and run sequentially.
Optional shadow adds 0.5 CPU/384 MiB and is outside both approved-stage proposals.
Local Compose 2.40.3 syntax and resolved isolation/limit checks passed with
generated fixtures. VPS Compose 5.5.1 parser/override compatibility is UNKNOWN
because the operator explicitly did not execute the probe. Version alone is
not verification. No staging resources were started in this task.

Existing operator evidence (13 October 2026, 20:41:46–20:42:32 UTC / 21:41:46–
21:42:32 London) reports 6 cores, minimum rounded available RAM 5.8 GiB and
78–82% host CPU idle. These are six short samples, not production peak capacity.
22 GiB free disk, 78% utilization, overlayfs/ext4 and cgroups v2 are observed.
The 49.13 GB cache and "reclaimable" figures are already occupied storage; no
cleanup is authorized or assumed. Final registry transfer sizes, shared layers,
uncompressed images, PostgreSQL growth, two restore/backup generations, datasets,
logs and safety headroom must fit an operator-approved storage budget. Build on
CI, not this VPS. No numerical storage PASS is asserted from free space alone.

Existing names (`nexus-trading-bot_*` network/container resources, the five
recorded volumes and `pa-replay-2025`) do not literally collide with the proposed
new prefix. Compose ownership labels remain UNKNOWN. Port 18443 had no listener
at inspection; bind-time availability requires rechecking under a new approved
window. Pending Docker/OpenSSH/OS updates and reboot require an explicit
maintenance or accepted-deferral decision, not an automatic update/restart.

## Remaining gates

| Requirement | Status | Required evidence/action |
| --- | --- | --- |
| Source inventory and frozen preview | PASS locally | Accept new source identity; authorized commit/push still pending |
| Workflow syntax and deterministic release guards | PASS locally | Execute on GitHub after authorized push |
| Security publication gate | FAIL for retained API and worker | Remediate/rebuild and scan actual accepted candidate |
| Protected publication environment / package ACL | UNKNOWN | Named reviewer, branch protection, actual approval API and package access proof |
| Human release acceptance / immutable GHCR references | BLOCKED | Reviewed candidate, green CI, explicit publication approval, actual manifest |
| Compose syntax / isolation / initializer ceilings | PASS locally | Target parser/override/profile probe remains UNKNOWN |
| Target project ownership and bind-time port | UNKNOWN | Project-label metadata and approved target profile/port check |
| CPU/RAM/disk reserves | CONDITIONAL / pending approval | Peak-risk decision, storage budget and monitoring thresholds |
| Patch/reboot disposition | BLOCKED pending decision | Approved maintenance or documented security risk acceptance |
| Independent staging auth / trusted TLS | BLOCKED | Separate identity project, customer sessions/RBAC and trusted staging certificate |
| Off-host backup / restore ownership | BLOCKED | Encryption/access/retention owner and isolated restore acceptance |
| Monitoring / incident / rollback ownership | BLOCKED | Named owners, alerts, maintenance window, rollback acceptance |
| VPS access / transfer / startup authorization | BLOCKED | New explicit scope and UTC window; previous inspections grant none |

Infrastructure capacity remains CONDITIONAL. **Stage A: NO-GO. Stage B: NO-GO.**
The smallest safe next step is review of the new source/workflow/security report,
then separately authorize the required release remediation and exact source
commit/push. Configure the protected publication environment before manual CI
dispatch. This does not authorize VPS access, transfer or startup.

## Prepared Mac Terminal sequence — release retrieval

Use a new Bash session, an installed trusted Git/GitHub CLI/Docker/Python client
and accepted values. Never run placeholders. These blocks are deliberately
separate approval checkpoints, not an unattended deployment script.

```bash
set -euo pipefail
umask 077
: "${RELEASE_COMMIT:?set the accepted 40-character commit}"
: "${SOURCE_SHA256:?set the accepted source SHA-256}"
: "${PUBLICATION_RUN_ID:?set the approved successful GitHub run ID}"
[[ "$RELEASE_COMMIT" =~ ^[0-9a-f]{40}$ ]]
[[ "$SOURCE_SHA256" =~ ^[0-9a-f]{64}$ ]]
[[ "$PUBLICATION_RUN_ID" =~ ^[0-9]+$ ]]
RELEASE_DIR="$HOME/nexus-staging-releases/$RELEASE_COMMIT"
REVIEW_DIR="$HOME/nexus-staging-reviews/$PUBLICATION_RUN_ID"
test ! -e "$RELEASE_DIR"
test ! -e "$REVIEW_DIR"
git clone --no-checkout https://github.com/Arjuunb/nexus-trading-bot.git "$RELEASE_DIR"
git -C "$RELEASE_DIR" checkout --detach "$RELEASE_COMMIT"
test "$(git -C "$RELEASE_DIR" rev-parse HEAD)" = "$RELEASE_COMMIT"
python3 "$RELEASE_DIR/ops/github-release.py" freeze \
  --output "$REVIEW_DIR/source" --reviewed-commit "$RELEASE_COMMIT" \
  --expected-source "$SOURCE_SHA256"
gh run view "$PUBLICATION_RUN_ID" --repo Arjuunb/nexus-trading-bot \
  --json conclusion,headSha,event,url
# Review success, exact commit and approval proof before using the artifact.
gh run download "$PUBLICATION_RUN_ID" --repo Arjuunb/nexus-trading-bot \
  --name "staging-publication-$PUBLICATION_RUN_ID-1" --dir "$REVIEW_DIR/publication"
CONFIG_PACKET="$REVIEW_DIR/publication/staging-config"
python3 "$RELEASE_DIR/ops/github-release.py" verify-packet \
  --directory "$CONFIG_PACKET" --reviewed-commit "$RELEASE_COMMIT" \
  --source-sha "$SOURCE_SHA256"
```

Read and accept public `publication.json`, actual reviewer/run references, scan
reports and accepted source identity before continuing. The publication artifact
is not deployment permission. Obtain a separate staging-only GHCR `read:packages`
credential with no write scope. Prefer the approved credential helper; if using
password stdin, keep a private Docker config on the appropriate client. Do not
copy a Mac Docker credential store to the VPS or use production credentials.

```bash
: "${GHCR_PULL_USER:?approved staging-only package reader}"
PULL_CONFIG="$REVIEW_DIR/docker-pull"
mkdir -m 700 "$PULL_CONFIG"
read -r -s -p 'Staging-only GHCR read token: ' GHCR_PULL_TOKEN
printf '\n'
printf '%s' "$GHCR_PULL_TOKEN" | docker --config "$PULL_CONFIG" login ghcr.io \
  --username "$GHCR_PULL_USER" --password-stdin
unset GHCR_PULL_TOKEN
```

Without a credential helper Docker stores a reversibly encoded credential in
that private directory. Assign retention/revocation ownership. Package ACLs,
token compatibility and CI approval API access remain untested remote gates.
For VPS image pulls authenticate separately on the approved VPS in a new private
staging-only Docker config using the same stdin pattern. No token in command-line
arguments, copied public packet, reports or shell history; no `set -x`.

## Prepared Mac Terminal sequence — explicitly approved transfer

Only after a **new** authorization naming target, public packet, credentials
channel and UTC window. Independently verify the ED25519 server fingerprint
`SHA256:4dE292zoEpAxwAdJa1TtmqNpk24DouBXuTsxCFFl1hU` through the provider console
against the trusted local known-hosts record. If missing or changed, stop;
do not use accept-new, disable host-key checking, or trust SSH keyscan alone.
The private key is never read, displayed or transferred.

```bash
: "${VPS_ACCESS_START_UTC:?approved YYYY-MM-DDTHH:MM:SSZ}"
: "${VPS_ACCESS_END_UTC:?approved YYYY-MM-DDTHH:MM:SSZ}"
: "${REMOTE_RELEASE_DIR:?approved NEW /home/ubuntu/nexus-staging/... path}"
[[ "$REMOTE_RELEASE_DIR" =~ ^/home/ubuntu/nexus-staging/[a-zA-Z0-9._/-]+$ ]]
[[ "$REMOTE_RELEASE_DIR" != *'..'* ]]
test -r "$HOME/.ssh/id_ed25519"
check_window() {
  python3 - "$VPS_ACCESS_START_UTC" "$VPS_ACCESS_END_UTC" <<'PY'
import sys
from datetime import datetime,timezone
start,end=(datetime.fromisoformat(v.replace('Z','+00:00')) for v in sys.argv[1:])
assert start.tzinfo is not None and end.tzinfo is not None
assert start <= datetime.now(timezone.utc) < end, 'AUTHORIZATION_WINDOW_CLOSED'
PY
}
SSH_ARGS=(-p 22 -i "$HOME/.ssh/id_ed25519" -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o UpdateHostKeys=no -o BatchMode=yes)
check_window
ssh "${SSH_ARGS[@]}" ubuntu@57.129.155.230 'date -u; hostname'
# Check actual target identity and remaining window before separately approved transfer.
check_window
ssh "${SSH_ARGS[@]}" ubuntu@57.129.155.230 \
  "umask 077; test ! -e '$REMOTE_RELEASE_DIR' && mkdir -p '$REMOTE_RELEASE_DIR'"
check_window
scp -P 22 -i "$HOME/.ssh/id_ed25519" -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o UpdateHostKeys=no -o BatchMode=yes \
  -r "$CONFIG_PACKET/." "ubuntu@57.129.155.230:$REMOTE_RELEASE_DIR/"
```

Select a transfer window long enough for the reviewed payload; do not start an
operation that could exceed it. Stop at its expiry. Only the verified public
config packet is transferred here. Staging authentication/TLS bindings use the
separately approved secure provisioning channel. No production state, repository,
SSH key or dotenv is copied. The next blocks run manually on the approved target
after rechecking its identity, authorization/window, namespace and capacity.

## Prepared target sequence — immutable pulls and fresh private bindings

Set `RELEASE_DIR` to the approved transferred packet, `STAGING_PRIVATE` to a new
private directory outside it, and `PULL_CONFIG` to that target's separately
approved private GHCR credential configuration. No default production path.

```bash
set -euo pipefail
umask 077
: "${RELEASE_DIR:?approved absolute transferred packet directory}"
: "${STAGING_PRIVATE:?new separate absolute private binding directory}"
: "${PULL_CONFIG:?approved staging-only Docker credential directory}"
test -d "$RELEASE_DIR/ops"
test ! -e "$STAGING_PRIVATE"
: "${RELEASE_COMMIT:?accepted commit from the independently reviewed receipt}"
: "${SOURCE_SHA256:?accepted SHA-256 from the independently reviewed receipt}"
python3 - "$RELEASE_DIR" "$RELEASE_COMMIT" "$SOURCE_SHA256" <<'PY'
import hashlib,json,sys
from pathlib import Path,PurePosixPath
root=Path(sys.argv[1]); commit,source=sys.argv[2:]
allowed={'ops/staging.remote.compose.yaml','ops/staging.local.compose.yaml',
         'ops/staging.remote.nginx.conf','ops/prepare-staging-bindings.py','publication.json'}
receipt=json.loads((root/'packet-integrity.json').read_text())
assert receipt['commit']==commit and receipt['source_sha256']==source
assert len(receipt['files'])==len(allowed)
assert {r['path'] for r in receipt['files']}==allowed
for row in receipt['files']:
    p=PurePosixPath(row['path']); assert not p.is_absolute() and '..' not in p.parts
    assert all(not (root/Path(*p.parts[:n])).is_symlink() for n in range(1,len(p.parts)+1))
    f=root/p; assert f.is_file() and f.stat().st_size==row['bytes'] and f.stat().st_size<2*1024*1024
    assert hashlib.sha256(f.read_bytes()).hexdigest()==row['sha256']
actual={str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()}
assert actual==allowed|{'packet-integrity.json'}
m=json.loads((root/'publication.json').read_text())
assert m['commit']==commit and m['source_sha256']==source
assert m['approval']['verification']=='CONFIGURED_REVIEWER_APPROVED_THIS_RUN'
print('Transferred public packet matches accepted commit/source and declared hashes')
PY
mapfile -t RELEASE_VALUES < <(python3 - "$RELEASE_DIR/publication.json" <<'PY'
import json,re,sys
m=json.load(open(sys.argv[1])); assert m['deployment_authorization'] is False
for role in ('api','worker'):
    ref=m['images'][role]['reference']
    assert re.fullmatch('ghcr.io/arjuunb/nexus-trading-bot-'+role+'@sha256:[0-9a-f]{64}',ref)
    print(ref)
for role in ('postgres','ingress'): print(m['images'][role]['reference'])
assert 0 < m['api_uid'] < 65535
print(m['api_uid'])
PY
)
test "${#RELEASE_VALUES[@]}" = 5
API_IMAGE=${RELEASE_VALUES[0]}
WORKER_IMAGE=${RELEASE_VALUES[1]}
POSTGRES_IMAGE=${RELEASE_VALUES[2]}
INGRESS_IMAGE=${RELEASE_VALUES[3]}
API_UID=${RELEASE_VALUES[4]}
for image in "$API_IMAGE" "$WORKER_IMAGE" "$POSTGRES_IMAGE" "$INGRESS_IMAGE"; do
  docker --config "$PULL_CONFIG" pull --platform linux/amd64 "$image"
done
python3 - "$RELEASE_DIR/publication.json" <<'PY'
import json,subprocess,sys
m=json.load(open(sys.argv[1]))
for role in ('api','worker','postgres','ingress'):
    wanted=m['images'][role]
    got=json.loads(subprocess.check_output(['docker','image','inspect',wanted['reference']]))[0]
    assert wanted['reference'] in got['RepoDigests'], 'REGISTRY_IDENTITY_MISMATCH'
    assert got['Architecture']=='amd64' and got['Os']=='linux'
    if role in ('api','worker'):
        assert got['Id']==wanted['config_id'], 'CONFIG_ID_MISMATCH'
        labels=got['Config']['Labels']
        assert labels['org.opencontainers.image.revision']==m['commit']
        assert labels['nexus.source.sha256']==m['source_sha256']
print('Immutable image references/config IDs/source labels verified')
PY
python3 "$RELEASE_DIR/ops/prepare-staging-bindings.py" prepare \
  --output "$STAGING_PRIVATE" --project nexus-remote-stage-s14-ghcr \
  --api-image "$API_IMAGE" --worker-image "$WORKER_IMAGE" \
  --postgres-image "$POSTGRES_IMAGE" --ingress-image "$INGRESS_IMAGE" \
  --uid "$API_UID" --port 18443
```

Verify transferred packet hashes against the accepted Mac receipt before image
pulls and before running its helper. Install **new** staging Supabase public client
bindings and trusted `tls/tls.crt`/private `tls/tls.key` through the approved secure
process. No self-signed fixture is a trusted customer certificate. The helper's
`check` deliberately returns BLOCKED/exit 2 because deployment authorization and
customer/TLS acceptance cannot be self-certified. Do not ignore that exit or
turn it into automatic startup permission. No Stage A startup until the human
gate record explicitly accepts all prerequisites.

## Prepared target sequence — Stage A only, after separate GO

The following wrapper reuses the existing private-binding/project checks and
never prints resolved credentials. An approved operator must separately verify
the app/TLS files, customer auth, packet/image identities and all remaining gates.

```bash
PROJECT=nexus-remote-stage-s14-ghcr
staging_compose() {
  RELEASE_DIR="$RELEASE_DIR" STAGING_PRIVATE="$STAGING_PRIVATE" PROJECT="$PROJECT" \
  python3 - "$@" <<'PY'
import importlib.util,os,subprocess,sys
from pathlib import Path
root=Path(os.environ['RELEASE_DIR'])
spec=importlib.util.spec_from_file_location('b',root/'ops/prepare-staging-bindings.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
args=b.checked_compose_args(str(root),str(Path(os.environ['STAGING_PRIVATE'])/'compose.env'),os.environ['PROJECT'])
raise SystemExit(subprocess.call(args+sys.argv[1:]))
PY
}
staging_compose config --quiet
staging_compose --profile shadow config --format json | \
  docker run --rm -i --pull=never --network none --read-only \
    --memory 64m --pids-limit 16 --entrypoint python "$WORKER_IMAGE" -c \
    'import sys,json;sys.path.insert(0,"/app/automation-hub");from services.remote_staging import validate_remote_profile;print(json.dumps(validate_remote_profile(json.load(sys.stdin))))'
# STOP unless the complete target profile and all human gates are accepted.
staging_compose up -d --wait --wait-timeout 100 postgres
staging_compose run --rm --no-deps state-init
staging_compose run --rm --no-deps metadata-init
staging_compose up --no-deps -d --wait --wait-timeout 100 api
staging_compose up --no-deps -d --wait --wait-timeout 100 ingress
staging_compose ps
for service in postgres api ingress; do
  id=$(staging_compose ps -q "$service")
  test -n "$id"
  docker inspect --format '{{.Name}} {{.State.Health.Status}} {{.RestartCount}}' "$id"
  docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}} {{.PIDs}}' "$id"
done
```

This starts no research worker, shadow observer or public-feed producer.
`--profile shadow config` resolves definitions for validation only; it does not
start shadow. Record all initializer exit statuses, authority markers/restricted
role evidence and owned-volume labels without printing DSNs or querying
production databases. Authenticate using the separate customer identity and
verify actual session/RBAC/read-only diagnostics. Stage A missing worker status
must remain explicitly unavailable; it is not a healthy running worker claim.

For private browser access use an independently authorized Mac SSH tunnel:

```bash
check_window
ssh "${SSH_ARGS[@]}" -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:18443:127.0.0.1:18443 ubuntu@57.129.155.230
# In a second trusted Mac terminal, with the approved certificate identity:
: "${STAGING_TLS_NAME:?approved staging certificate SAN}"
: "${STAGING_TRUST_CA:?approved public CA certificate file}"
curl --fail --cacert "$STAGING_TRUST_CA" \
  --resolve "$STAGING_TLS_NAME:18443:127.0.0.1" \
  "https://$STAGING_TLS_NAME:18443/health"
```

No `curl -k`, public port publication or production reverse-proxy modification.
The root health endpoint proves liveness only. Keep tunnel usage inside its
authorization window and close it at expiry. Functional auth/readiness, storage
alarms, backup encryption/restore, monitoring and release acceptance are separate
checks with named owners; none is claimed by a successful curl.

## Prepared safe rollback and recovery

On an approved Stage A failure, stop only the owned staging application/ingress
after confirming the wrapper's project/binding marker and actual ownership:

```bash
staging_compose ps
staging_compose stop -t 15 ingress api
staging_compose ps
```

Retain the staging PostgreSQL container, all volumes, authority markers, dataset
bytes, spools and evidence. No `down -v`, prune, production restart, database
downgrade, table dropping, financial import or state reset. On a first deployment
there is no previous accepted GHCR image to invent. On a later release, application
rollback requires separately reviewed prior digest, schema and UID compatibility;
create new private bindings and rerun target gates before approved startup.

Use existing metadata/artifact backup and isolated restore procedures in
`NEXUS_REMOTE_STAGING_PLAN.md` and `NEXUS_STAGING_PREREQUISITES.md`. Assign encrypted
off-host backup, restore/RPO/RTO, monitoring and rollback owners before GO.
Restore into a new isolated authority and verify exact dataset bytes/references,
roles, migrations, committed results and worker recovery before human acceptance.
Local PostgreSQL tests establish fixture behavior, not a VPS restore certificate.
