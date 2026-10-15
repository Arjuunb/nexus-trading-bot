# PostgreSQL staging support-image repair

This separate controller rebuilds gosu 1.19 from upstream commit
`6456aaa0f3c854d199d0f037f068eb97515b7513` with Go 1.27.2, then replaces only
`/usr/local/bin/gosu` in the pinned official PostgreSQL 16.15 Alpine image.
The accepted RC3 API/worker images and source are not changed. PostgreSQL
packages, entrypoint, command, environment, user, volumes and signal remain
inherited unchanged. Original base layers remain present; the final filesystem
contains the rebuilt helper. The old binary's Go 1.24.6 vulnerability findings
are not waived, suppressed or patched in the scanner report.

The official compiler archive, source archive, go.mod and go.sum are hash-pinned.
The Go proxy's module revision matches the pinned commit, source files match the
independently downloaded archive, and module checksums are verified through
sum.golang.org. The binary retains its actual module pseudo-version for this
commit; no fake tag/version metadata is injected. No dependency upgrades are
introduced. CGO is disabled, and the compiler is excluded from the final image.

Execution is restricted to the disposable GitHub-hosted Linux amd64 runner,
the dedicated branch and local Docker socket, with >=4 CPUs, >=8 GiB available
RAM and >=20 GiB free disk after removal of fixed unused CI SDK directories.
There are no registry credentials, push permissions, SSH calls or VPS actions.

Disposable tests use a newly created, uniquely named Docker volume and a
container without networking, published ports or host bind mounts. They verify
gosu privilege transitions, PostgreSQL 16.15, the database process running as
uid 70, fresh initialization, correct/incorrect TCP password authentication and
data persistence across restart. Only those newly created CI objects are removed.
These tests do not certify Supabase, the NEXUS API, remote TLS, production data
or a VPS deployment.

Trivy 0.75.0 is checksum-pinned and scans vulnerabilities, filesystem secrets and
image-configuration secrets. All severities are retained, no unfixed issues are
ignored, and the gate requires zero HIGH/CRITICAL vulnerabilities and zero
detected secrets at every severity. Default Trivy built-in path exclusions apply;
there are no custom exclusions. The report must include the exact final config
ID, Alpine package results and Go 1.27.2 inventory for the replaced helper.
Raw secret matches are withheld from retained reports, including failed runs.

Evidence records the compiler/source/module hashes, rebuilt binary hash,
controller SHA, base config ID, unchanged base layers/runtime settings,
BuildKit metadata, tests, scanner/database versions, local tag/config ID and
Docker archive hash. Every archive layer is verified, and the added layer may
contain only the replacement gosu regular file. Only passing runs upload the
image archive. Artifacts expire after seven days and should be retained locally.
There is no registry manifest digest until separately approved publication.

```sh
python3 .github/rc3-postgres/test_build.py
actionlint .github/workflows/rc3-postgres-build-scan.yml
```
