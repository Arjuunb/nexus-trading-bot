# Separately approved PostgreSQL publication

The user approved publishing only the already scanned PostgreSQL support image
from run 38788079327, after reviewing its build/test/security results. This
controller loads and publishes that exact archive without rebuilding or executing
the image. API/worker publication workflows are untouched.

The archive SHA256, size, config ID, original tag, provenance labels, each layer
and the sole replacement binary are reverified. Ten original evidence files are
individually hash-pinned. The original workflow status/controller and artifact
IDs/ZIP digests must match. Scanner findings are reparsed, including all secret
severities and the patched helper inventory; summary flags cannot hide findings.

The job runs only on the disposable GitHub Linux amd64 runner and dedicated
branch, with first-attempt and local-socket guards. It uses the existing
`ghcr-staging-release` environment and job-scoped package write permission. A
conflicting existing target tag blocks publication even if package metadata is
unavailable. No production aliases, package visibility changes, builds, container
starts, SSH or VPS operations are present.

After publication, the job pulls by immutable digest and checks the same config,
layers and labels. Direct registry reads independently hash the manifest/config
bytes and confirm the tag, platform and source identity. The workflow token is
used only at the approved GitHub/GHCR origins; registry credentials are never
forwarded to signed CDN blob redirects or retained in evidence. Disposable Docker
credentials are removed. Actual package visibility is recorded without changes.

Evidence includes partial/failure states and remains available for 30 days.
Publication does not grant configuration/deployment, worker or trading approval.
