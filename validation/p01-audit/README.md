# Proposal 01 audit evidence (read-only)

Evidence behind `NEXUS_P01_PERFORMANCE_AND_RETIREMENT_AUDIT.md`. Nothing here was
run against production or staging. All runs used a sandbox (4 vCPU Intel Xeon
2.80 GHz) with a throwaway data directory; outbound Binance access was blocked,
so live market-data processing is **not** represented.

| File | What it measures |
|---|---|
| `pa_eval_bench.py` | Guardian `PAEvaluationReader` query and Journal Recorder full pass over `pa_evaluations`, using the exact DDL from the audited source and synthetic rows. Output: `sqlite-benchmark-output.txt`. |
| `rowvalue.py` | Same Guardian query rewritten as a row-value seek with an `updated_at` index. |
| `boot_measure.py` | Boots one API process, samples RSS/threads/CPU over an idle window, times key endpoints on empty databases. Expects a virtualenv at `../venv` relative to the script. Results: `boot-results/`. |
| `poll_rates.py` | Per-open-tab request rates computed from the dashboard poller inventory. Output: `poll-rates.txt`. |
| `frontend-build-215c96c.txt` | Vite production build chunk sizes for the audited dashboard. |

Audited sources: `215c96c` (Guardian lineage, matches the production sidebar),
`f8778ed` (staging API revision per operator evidence), `c13a439` (PA retirement
branch, contains `f9ebd32` and `c0c0cf0`).
