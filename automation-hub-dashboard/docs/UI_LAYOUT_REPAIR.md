# UI layout repair

Branch: `codex/fix-ui-layout`, based on `8530844`.

The UI audit reproduced horizontal overflow on phone screens, header overlays
clipped by the header's blur, and incorrect footer connection messages.

## Changes

| Area | Cause | Corrected behavior |
| --- | --- | --- |
| Cards, tables, Market Data controls | Flex/grid minimum widths and non-wrapping controls | Cards stay inside the viewport; tables scroll within their own containers; controls wrap |
| Paper terminal | Hard-coded four-column account summary and narrow title/control rows | Account summary uses four, two, or one column; title and controls wrap on phones |
| Dashboard and PA headings | Missing warning padding and competing fixed-width header items | Warning text has padding; PA title occupies its own row on phones |
| Pet | Desktop-sized launcher rose above the phone footer | Mobile launcher fits inside the reserved footer area; details open on click |
| Modal and command palette | Blurred header established a fixed-position containing block | Both render through a document-body portal; the backdrop covers the viewport |
| Fullscreen charts | Stacking order below the persistent header | Paper and SMC fullscreen views render above app chrome |
| Notifications | Tray extended to the right of a right-side trigger | Desktop tray aligns to the trigger's right edge; phone trays fit the viewport |
| Footer | Loading, authentication and server errors shared one fallback; cached success survived failed polls | Distinct connection states, explicit stale data, recovery after successful polling, and retained lab scope |

Footer states: `LOADING`, `DATA_UNAVAILABLE`, `AUTH_REQUIRED` (401),
`ACCESS_DENIED` (403), `RATE_LIMITED` (429), `API_ERROR` (including HTTP status),
and `BACKEND_UNREACHABLE` for recognized fetch/network failures. Cached snapshots
show `STALE` / `DEGRADED` when refresh fails. API error messages preserve HTTP
status even when the response includes a JSON detail message.

## Verification

Result: final layout audit **45 passed**; the additional navigation, pet, lab
dashboard and SMC checks **11 passed**. TypeScript checking and the production
build passed. Vite still reports its existing large-chunk warning.

The audit derives its 18 primary routes from the current navigation table, checks
the actual route title, catches render errors, and tests whether another element
covers visible controls. PA journal/learning, Journal trade provenance, and Forward
Validation fixtures now match the current response shapes.

Run from `automation-hub-dashboard`:

```sh
npm run typecheck
npx playwright test e2e/audit.spec.ts e2e/responsive-layout.spec.ts --reporter=line
npx playwright test e2e/stage1-navigation.spec.ts e2e/nexus-pet.spec.ts e2e/lab-dashboard.spec.ts e2e/smc-strategy-lab.spec.ts --reporter=line
```

Coverage includes phone containment for all primary routes, desktop hit testing,
notification/account panels at 1440/390/320px, modal/palette backdrop coverage,
pet placement, HTTP error classification, stale lab status and polling recovery.
Phone screenshots for Dashboard, Paper Trading and PA are produced in
`test-results` by the responsive-layout test.

These checks use Chromium and a mocked API. They do not establish production or
Safari verification. The older `clicks.spec.ts` and `flows.spec.ts` contain legacy
navigation/control expectations; this repair does not claim the entire legacy
E2E suite is green. Trading logic, live routing, VPS deployment and database
contents are outside this patch.
