# RC3 build and scan only

Pushes affecting this controller on `ci/rc3-isolated-image-build` start a disposable
GitHub-hosted Ubuntu amd64 job. Manual dispatch is also supported once the workflow
is registered on the default branch. The controller builds only accepted commit
`28acdbccc4f9b737c683d94e15bb16a09e50aa85`, with canonical source SHA256
`3ba76e44d9181ee1a8910667be645bfa477a7b3e4055c30538a29030e83caa4d`.

The job verifies successful validation run `38759339962`, then requires at least
4 CPUs, 8 GiB available memory and 20 GiB free disk on the relevant filesystems.
It removes only fixed unused SDK directories on the disposable GitHub VM before
measuring disk. It never cleans production storage or accesses any VPS.

The immutable RC3 Dockerfiles, pinned bases, API Python hash lock, npm integrity
locks and reviewed worker wheel preparer are reused without modification. API
Python wheels are verified against the retained lock; worker wheel hashes are
verified against public PyPI metadata and retained in a generated hash lock.
Both builds use `--no-cache`, `--pull`, `--load` and the local Docker driver.
The API's frontend package downloads use `npm ci`; worker installation is offline.

Trivy 0.69.3 is downloaded using a fixed SHA256 verified against the official
release asset. Source and final images are scanned using all built-in secret
rules and every severity, without a repository ignorefile or custom allow rules.
Image configuration secrets are included. The vulnerability gate accepts no
HIGH or CRITICAL finding, including unfixed issues. LOWER severities remain in
the sanitized report. Native Trivy built-in path exclusions still apply.
Scanner failures, malformed reports and mismatched image IDs fail closed.

The accepted source's read-only image probes verify file hashes, runtime versions,
non-root users, disabled execution flags and the Alpine package inventory. They
do not start the API or worker services. The existing Stage A limitation remains:
the worker is not certified for activation (its optional pandas dependency is
absent); a passing build cannot authorize worker execution or trading.

`rc3-image-evidence-*` retains sanitized reports, exact build commands, resource
measurements, source/input hashes, image config IDs and local BuildKit manifest
digests for seven days. Raw secret matches are never uploaded. Only a fully
passing run uploads `rc3-local-images-*` containing Docker archives and records
their SHA256 values in the evidence. Download them before the seven-day expiry.
No registry digest exists before a separately approved publication.

The workflow has read-only repository permissions. It never logs in to a registry,
pushes images, uses SSH, deploys, starts trading or changes production services.
The build controller can be checked locally with:

```bash
python3 .github/rc3-images/test_build_scan.py
actionlint .github/workflows/rc3-image-build-scan.yml
```
