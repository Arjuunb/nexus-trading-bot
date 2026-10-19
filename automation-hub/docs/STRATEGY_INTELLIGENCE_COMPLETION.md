# Strategy Intelligence PRD completion checkpoint

This is a partial implementation checkpoint, not end-to-end acceptance or a
deployment certificate. The original 17-phase PRD was retrieved from the
referenced Adaptive MTF Strategy Review conversation on 2026-10-18.

## Source identity

GitHub main `24b67bf43e1712b5566fa33b457814ceac5ec30b` does not contain the full
Sprint 1–14 implementation. RC3 `28acdbccc4f9b737c683d94e15bb16a09e50aa85`
does. The staging image reported in the referenced conversation is based on
`f8778ed42039d415f3829e08a05225976fe0d0a5`, which also preserves the
customer-workspace integration. This patch uses that source. The currently
running source was verified over SSH: image config
`sha256:f2343e76a896ec3718328f3318adc3c2a40c93774194602858de4bdd4dce5397`,
source label SHA-256
`940df9d11aee70f2c4a8b7fc3960f23376f1537adac3d1273da10007f5f5813e`.

## Narrow corrections

Qualification policy `qualification.v2` adds explicit gates for profitable,
independent forward-paper outcomes after costs, positive stressed returns and
available Monte Carlo analysis. A successful cost calculation can still have
negative returns and cannot qualify merely because its calculation succeeded.
Historical policies and persisted reports are retained unchanged.

The existing robustness worker nests its concentration report. Qualification
now consumes that structure. Malformed, fractional, nonfinite or boolean sample
counts fail closed. In-flight, cancelled, failed and interrupted validation
records cannot supply passing evidence or resurrect an older passing result
after a newer failed or unfinished attempt. Multiple asset/classifier groups do
not silently qualify the first group, and qualification does not mutate cached
performance metrics.

No strategy identity, trading rule, leverage, risk limit, financial history,
execution route, production Compose file or live flag is changed.

## Genuine remaining integration gaps

1. Qualification currently reads validation history using the requested
   execution-mode filter, while historical research jobs persist `backtest`.
   A forward-paper cohort consequently cannot automatically consume its
   matching historical validation chain. The historical and forward evidence
   need separate exact bindings to the same frozen strategy identity and scope.
2. The standalone research worker does not produce a `forward_paper` validation
   record. The authoritative forward-paper reader explicitly reports external
   ledger/cost reconciliation limitations. Decision-only shadow observations
   cannot substitute for executed paper outcomes or establish profitability.
3. Champion/challenger designation and comparison are advisory. No operational
   regime-aware strategy selector or paper-execution integration was found.
4. The PRD's composite Edge Score, rolling 20/50/100-trade decay lifecycle and
   weekly automated research workflow are not established by the existing
   advisory comparison/degradation and manually submitted hypothesis tools.
5. Real staging APIs returned zero observed cohorts, research candidates and
   research jobs. No standalone research or shadow worker is running. There
   is no end-to-end research result to accept. An existing administrator's
   browser session is still required for real dashboard acceptance.

These are incomplete requirements, not completed features hidden behind an
activation toggle. No deployment should be represented as PRD completion
until they are implemented and the complete workflow is demonstrated.

## Runtime boundary

Staging target: `/home/ubuntu/nexus-rc3-staging-28acdbc/release`.
Expected staging project: `nexus-remote-stage-rc3-28acdbc`.
Production is a different project and remains untouched. After the user
unlocked the existing SSH identity, inspection found the staging API had
exited 137 with `OOMKilled=true` at its 768 MiB ceiling. Ingress was unhealthy.
The existing API container was restored with a 1536 MiB ceiling and no swap;
its container identity, image, mounts and execution-disable flags were retained.
The host had more than 7 GiB available RAM. This bounded recovery does not
establish the root cause of memory growth or long-term stability.

Rollback receipt and a separate future Compose memory overlay were saved at
`release/intelligence-recovery-20261018/` on staging. Original retained Compose
overlays remain unchanged. Future Compose recreation must explicitly include
`compose.memory.yaml` last to retain the adjusted ceiling.

All three staging services became healthy; loopback HTTPS returned 200.
Seven real intelligence API paths each returned 401 anonymously and 200 with
the existing internal operator credential (14 checks). Credentials were never
printed or placed in browser configuration. Production container IDs, image
IDs, start times and mounts were compared before and after; they are identical.

The qualification patch is local and has not been built into a release image
or deployed. No remote database migration, research worker activation, order
submission or trading activation occurred.

## Validation checkpoint

Final focused qualification/service/API/comparison suite: **119 passed,
0 failed, 0 skipped, 4 warnings**. Dashboard typecheck and production build:
passed (existing bundle-size warning). All existing dashboard Chromium
regressions: **154 passed**, using Brave's Chromium in isolated test profiles
against the suite's mocked APIs. These do not establish real staging browser
or end-to-end research acceptance.

Broad local regression checkpoint before the final identity regression cases:
**4136 passed, 4 failed, 76 skipped, 84 warnings**. Three failures use Linux
`/proc` process-generation health probes on macOS; one requires the unavailable
Docker CLI. No assertions were removed or skips added. A final full rerun was
started after the identity changes. The final retry-precedence change is
covered by the final focused suite; use retained logs for exact checkpoints.
