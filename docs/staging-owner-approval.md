# Explicit owner approval for isolated NEXUS staging

The owner authorized this staging-only exception on 19 October 2026 because no
verified second reviewer is available. It is not equivalent to independent
review. Production controls, live routing and strategy promotion are unchanged.

The manual staging publication workflow defaults to `independent-review.v1`.
That policy still requires a configured reviewer different from the initiating
and triggering accounts, with prevention of self-review enabled. Its existing
negative tests remain unchanged.

Selecting `staging-owner.v1` explicitly permits actual GitHub approval by
`Arjuunb` (immutable GitHub user ID `190428336`) only for the existing
`ghcr-staging-release` environment. Both run initiators must be that owner.
The environment must name that individual reviewer, allow self-review and
retain an accepted release-ref policy. Missing approval, bypasses, rejection,
other accounts/environments and reruns fail closed. A chat message, workflow
input or local receipt cannot substitute for the actual GitHub approval history.

Both modes retain exact-commit/source freezing, complete regression checks,
actual API/worker builds, native image checks, all required security scans,
sealed tested artifacts and immutable registry digest verification. The receipt
records the selected versioned policy and whether review was independent.
The workflow contains no SSH, deployment or trading operation.

Configure only the staging environment for owner review, with an explicit
`release/*` branch and `nexus-staging-*` tag policy. Arjuunb must then approve
each successful tested release in GitHub before publication can proceed. Do not
approve on the owner's behalf or simulate the approval API response.

Deploy only to the existing `nexus-remote-stage-rc3-28acdbc` project, retaining
its private bindings, all data volumes, rollback images and the 1536 MiB API
memory overlay. Replace only the explicitly selected staging services after
backup and image/source checks. Production remains outside this workflow and
retains its independent approval requirement. No live trading or automatic
promotion is authorized.

The staging API also sets `HUB_LEGACY_LAB_AUTOSTART=0`: `HUB_AUTO_ENGINE=0`
previously did not prevent the two unrelated legacy PA/SMC lab threads from
starting. Their retained staging databases total approximately 22.7 GB; no
record is deleted, reset, compacted or rewritten by this switch. The new setting
defaults to enabled outside staging, preserving existing production behavior.
It controls constructor autostart and does not alter strategy rules, persisted
session settings, explicit paper actions or the separate research worker.
When updating the existing remote project, include this setting in the final
image/memory overlay as well as retaining the 1536 MiB API limit.
