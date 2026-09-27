#!/bin/sh
# Journal post-implementation audit: rebuild the evidence dataset from nothing
# in a fresh scratch directory, then check it.
#
#   sh scripts/journal_audit/run_all.sh [scratch-dir]
#
# Every script refuses to run unless HUB_DATA_DIR holds the marker created
# here, and unless every database the settings resolve sits inside it, so this
# cannot write into the app's own data. Nothing here is live: the forward
# engine fills from quotes the scripts supply, and no exchange client exists
# on this path.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
HUB=$(cd "$HERE/../.." && pwd)
RUN=${1:-$(mktemp -d /tmp/journal-audit.XXXXXX)}
if [ -e "$RUN/ledger.db" ]; then echo "refusing: $RUN already holds data"; exit 2; fi
mkdir -p "$RUN/out" && touch "$RUN/.journal-audit-scratch"
cd "$HUB"
export HUB_DATA_DIR="$RUN" HUB_ECON_FEED=off
OUT="$RUN/out"
step() { echo "== $*"; }
step "1 real strategy signal blocked by the Decision Brain"
AUDIT_INST=inst-audit-brain python "$HERE/e2e_real.py" long_tp > "$OUT/1_brain.txt" 2>&1
step "2 real strategy long -> take-profit"
AUDIT_QUALITY_BYPASS=1 python "$HERE/e2e_real.py" long_tp > "$OUT/2_long.txt" 2>&1
step "3 real strategy short -> stop-loss"
AUDIT_QUALITY_BYPASS=1 AUDIT_INST=inst-audit-short AUDIT_SESS=sess-audit-2 \
  python "$HERE/e2e_real.py" short_sl > "$OUT/3_short.txt" 2>&1
step "4-5 record vs execution truth"
python "$HERE/truth.py" "$RUN" inst-audit-3cr > "$OUT/truth_long.txt" 2>&1 || echo "   TRUTH LONG: MISMATCH"
python "$HERE/truth.py" "$RUN" inst-audit-short > "$OUT/truth_short.txt" 2>&1 || echo "   TRUTH SHORT: MISMATCH"
step "6 crash, restart and duplicate scenarios"
python "$HERE/e2e_crash.py" > "$OUT/crash.txt" 2>&1 || echo "   CRASH CHECKS: FAILURES"
step "7 material non-trade decisions"
python "$HERE/e2e_decisions.py" > "$OUT/decisions.txt" 2>&1
step "8 SMC and PA labs"
python "$HERE/e2e_labs.py" > "$OUT/labs.txt" 2>&1
step "9 simulation, agent and legacy sources"
python "$HERE/e2e_sources.py" > "$OUT/sources.txt" 2>&1
step "10 lab records vs broker fills"
python "$HERE/lab_truth.py" "$RUN" > "$OUT/truth_labs.txt" 2>&1 || echo "   LAB TRUTH: MISMATCH"
step "11 weekly reviews, scheduler and memory (on a copy)"
python "$HERE/weekly_audit.py" "$RUN" "$RUN/weekly_copy.db" > "$OUT/weekly.txt" 2>&1 || true
tail -1 "$OUT/truth_long.txt"; tail -1 "$OUT/truth_short.txt"; tail -1 "$OUT/truth_labs.txt"
grep "checks passed" "$OUT/crash.txt" "$OUT/weekly.txt" || true
echo "dataset: $RUN   (serve it: HUB_DATA_DIR=$RUN python -m uvicorn app:app --port 8777,"
echo "then: python $HERE/api_vs_db.py $RUN and node $HERE/ui_real.cjs <screenshot-dir>)"
