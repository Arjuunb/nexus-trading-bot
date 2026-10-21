# Task 6 approved API acceptance delta

The user replied **continue** after PR #43's exact application proposal at
`b5ccdd418eedd0e151c72ac2f8093e7be8c48fcf` was presented for source approval.
Application code is unchanged from that reviewed proposal. This records session
authorization; it does not invent a GitHub reviewer or registry approval.

The original `pr6_real_paper_freeze.json` remains byte-for-byte unchanged.
`source-delta.json` declares exactly three successor file hashes and five
constructor hashes. Freeze tests require the original predecessor hashes,
check every other method, and pin all eleven reviewed application files and the
new acceptance module. Negative tests reject an unapproved record, extra fill
waivers, changed predecessors, altered historical pins and post-review runtime
drift. The native image matrix now also runs the new acceptance and delta tests.

## Local verification

- Source/evidence/financial focused suite: 107 passed.
- Final acceptance, delta rejection and read-only inventory suite: 28 passed.
- Full available application/repository suite: 4,572 passed, 76 skipped,
  7 failed. All seven failures reproduce on approved source `4eceb97`:
  this Mac sandbox lacks Linux `/proc` process generation and Docker, and
  prohibits loopback binding. No changed-code failure remains in that run.
- Original operator-helper/release checks: 122 passed, 31 failures from the
  unchanged Linux resource-limit helpers on this Mac. Their downloaded bytes
  match the existing approved GitHub blobs. Linux CI remains required.

The full local run preceded materializing the unchanged operator evidence
files; the helper tests were run separately. These counts are separate scopes,
not an invented combined pass count. See `local-validation.json`.

## Remaining target evidence

The research worker rollout already passed on the VPS, including restart,
research-state integrity and PostgreSQL detached restore. Current API financial
state recovery and authenticated PRD/restart acceptance remain unproved. A new
API image has not been built, scanned or deployed by these local checks.

`vps-financial-inventory.py` is the next bounded, read-only target check. It
requires the observed existing API image, the deployed worker image and the
isolated API state volume. It reads only selected file/page sizes and reports
configuration-presence booleans. It never imports the application, starts or
stops services, creates backups, reads financial rows or prints credentials.
Its result must not be represented as backup/restore or release acceptance.

After current inventory, prove current financial recovery, complete fresh Linux
release and image checks, and prepare an API-only rollout retaining the deployed
worker overlay and 1536 MiB memory override last. Production, stored credentials,
financial data and execution boundaries stay within the existing authorized
staging scope. Task 6 remains incomplete until target checks actually pass.
