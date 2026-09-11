#!/usr/bin/env bash
# Read-only triage for "the labs and instances are not placing paper orders".
#
# Issues GET requests only. It never restarts a worker, never arms a session,
# never changes configuration and never writes to any database. Safe to run on
# a live host at any time.
#
# Usage, from the deploy directory on the VPS:
#
#     ./scripts/triage_paper_orders.sh                  # reads .env for the key
#     BASE=https://trade-logx.com ./scripts/triage_paper_orders.sh
#     HUB_CONTROL_KEY=... ./scripts/triage_paper_orders.sh
#
# Answers, in the order that rules out the most:
#   1. is the process alive and which commit is it
#   2. is the primary ledger connected
#   3. how many instance workers are actually running
#   4. is the market feed synchronized (nothing fills unless it is)
#   5. is either lab actually armed
#   6. what the shadow research comparison already says about PA vs SMC

set -uo pipefail

# Transport. The app container publishes nothing to the host: compose declares
# "expose: 8000", not "ports", so http://127.0.0.1:8000 from a host shell always
# fails and would report a perfectly healthy app as dead. Port 80 is no better
# once certificates exist, because nginx then redirects it to HTTPS and the
# certificate is issued for the domain rather than for a loopback address.
#
# So talk to the app from inside its own container, the way scripts/healthcheck.sh
# already does. Python is present there; curl and wget are not. Set BASE to force
# the HTTP path instead, e.g. BASE=https://trade-logx.com from a workstation.
BASE="${BASE:-}"
TRANSPORT="http"
if [ -z "$BASE" ]; then
  if docker compose exec -T app python -c "pass" >/dev/null 2>&1; then
    TRANSPORT="docker"
  else
    BASE="https://trade-logx.com"
  fi
fi
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# The control key authenticates any endpoint via the X-Webhook-Secret header.
if [ -z "${HUB_CONTROL_KEY:-}" ] && [ -f .env ]; then
  HUB_CONTROL_KEY="$(sed -n 's/^HUB_CONTROL_KEY=//p' .env | head -1 | tr -d '"'\''')"
fi
KEY="${HUB_CONTROL_KEY:-}"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }
fail() { printf '  \033[31m%s\033[0m\n' "$*"; }
warn() { printf '  \033[33m%s\033[0m\n' "$*"; }
good() { printf '  \033[32m%s\033[0m\n' "$*"; }

# Fetch $1 into $TMP/body and echo the HTTP status code ("000" = unreachable).
fetch() {
  local out
  if [ "$TRANSPORT" = "docker" ]; then
    out="$(docker compose exec -T app python -c '
import sys, urllib.request, urllib.error
path, key = sys.argv[1], sys.argv[2]
request = urllib.request.Request("http://127.0.0.1:8000" + path)
if key:
    request.add_header("X-Webhook-Secret", key)
try:
    response = urllib.request.urlopen(request, timeout=20)
    code, body = response.getcode(), response.read().decode("utf-8", "replace")
except urllib.error.HTTPError as exc:
    code, body = exc.code, exc.read().decode("utf-8", "replace")
except Exception as exc:
    code, body = 0, str(exc)
sys.stdout.write("%s\n%s" % (code, body))
' "$1" "$KEY" 2>/dev/null)"
    printf '%s' "${out#*$'\n'}" > "$TMP/body"
    out="${out%%$'\n'*}"
    [ "$out" = "0" ] && out="000"
  elif [ -n "$KEY" ]; then
    out="$(curl -sS -o "$TMP/body" -w '%{http_code}' --max-time 20 \
           -H "X-Webhook-Secret: $KEY" "$BASE$1" 2>/dev/null)"
  else
    out="$(curl -sS -o "$TMP/body" -w '%{http_code}' --max-time 20 "$BASE$1" 2>/dev/null)"
  fi
  printf '%s' "${out:-000}"
}

printf '\033[1mPaper order triage\033[0m  %s  %s\n' \
       "$([ "$TRANSPORT" = "docker" ] && printf 'via docker compose exec app' || printf '%s' "$BASE")" \
       "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
if [ -z "$KEY" ]; then
  warn "No HUB_CONTROL_KEY found: only /health and /version will answer."
fi

# ---------------------------------------------------------------- 1. process
bold "1. Process and deployed commit"
CODE="$(fetch /version)"
if [ "$CODE" != "200" ]; then
  fail "GET /version returned ${CODE}"
  fail "The app is not serving. Nothing downstream can trade."
  dim  "  docker compose ps"
  dim  "  docker compose logs --tail=80 app | grep -i 'REFUSING TO BOOT'"
  dim  "  Nginx will not start at all while the app container is unhealthy."
  exit 1
fi
good "app is serving"
# Compare against this checkout rather than a commit hard-coded when the script
# was written. What matters is whether the running container matches the source
# you are about to reason about, and a pinned SHA goes stale on the next merge.
CHECKOUT="$(git rev-parse HEAD 2>/dev/null || printf '')"
python3 - "$TMP/body" "$CHECKOUT" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("  could not parse /version"); raise SystemExit
short = (d.get("commit_short") or "").strip()
checkout = (sys.argv[2] or "").strip()
print("  running  = " + (short or "unknown"))
print("  checkout = " + (checkout[:7] if checkout else "unknown"))
if not short or short == "unknown":
    print("  (GIT_COMMIT was not passed at build time; identify the image another way)")
elif checkout and not checkout.startswith(short):
    print("  \033[33m^ the container is NOT running this checkout.\033[0m")
    print("  \033[33m  Rebuild before trusting anything below: ./scripts/deploy.sh\033[0m")
elif checkout:
    print("  \033[32mcontainer matches the checkout\033[0m")
PY

# ---------------------------------------------------------------- 2. ledger
bold "2. Primary ledger"
CODE="$(fetch /health)"
if [ "$CODE" != "200" ]; then
  warn "GET /health returned $CODE"
else
  python3 - "$TMP/body" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("  could not parse /health"); raise SystemExit
p = d.get("persistence") or {}
mode = p.get("mode") or "unreported"
print("  persistence mode = " + str(mode))
if mode == "read_only_degraded":
    print("  \033[31mSupabase is configured but its connection probe failed.\033[0m")
    print("  The startup hook returns before restoring ANY instance worker,")
    print("  and every ledger write is denied, including logging. So the system")
    print("  cannot even record why it is rejecting. Fix this before anything else.")
elif mode == "primary":
    print("  \033[32mprimary ledger connected\033[0m")
elif mode == "local":
    print("  \033[32mlocal SQLite ledger (Supabase not configured)\033[0m")
PY
fi

# ---------------------------------------------------------------- 3. workers
bold "3. Instance workers actually running"
CODE="$(fetch /instances)"
if [ "$CODE" != "200" ]; then
  warn "GET /instances returned $CODE (needs a valid HUB_CONTROL_KEY)"
else
  python3 - "$TMP/body" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("  could not parse /instances"); raise SystemExit
rows = d.get("instances") if isinstance(d, dict) else d
rows = rows or []
running = [r for r in rows if str(r.get("state")) == "running"]
desired = [r for r in rows if r.get("desired_running")]
print("  {} running / {} desired / {} configured".format(
      len(running), len(desired), len(rows)))
if desired and not running:
    print("  \033[31mZERO workers running while some are desired.\033[0m")
    print("  That is the blocker: no instance can place an order.")
for r in rows:
    state = str(r.get("state"))
    mark = "ok  " if state == "running" else "DOWN"
    name = r.get("name") or r.get("id") or "?"
    print("    [{}] {}  state={}  blocker={}".format(
          mark, name, state, r.get("last_blocker") or "-"))
    err = (r.get("last_error") or "").strip()
    if err:
        print("           last_error: " + err[:160])
        if "slot" in err.lower():
            print("           ^ slot cap: the default is 1 and the maximum is 3")
PY
fi

# ---------------------------------------------------------------- 4. feed
bold "4. Feed and arming (nothing fills unless the feed is SYNCHRONIZED)"
for entry in "Price Action|/research/price-action/bot-status" "SMC|/research/smc/bot-status"; do
  NAME="${entry%%|*}"
  ROUTE="${entry#*|}"
  CODE="$(fetch "$ROUTE")"
  if [ "$CODE" = "503" ]; then
    fail "$NAME status 503"
    dim  "    Read the body: PERSISTENCE_BLOCKED means SQLite, not strategy."
    head -c 300 "$TMP/body"; echo
    continue
  fi
  if [ "$CODE" != "200" ]; then
    warn "$NAME status returned $CODE"
    continue
  fi
  python3 - "$TMP/body" "$NAME" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("  " + sys.argv[2] + ": unparseable"); raise SystemExit
name = sys.argv[2]
session = d.get("session") or {}
sid = d.get("session_id") or session.get("id")
mode = d.get("operating_mode") or session.get("operating_mode")
armed = d.get("execution_armed")
feed = d.get("market_data_health") or d.get("feed_status") or d.get("connection")
blockers = d.get("blockers") or []
print("  " + name)
print("    session_id      = " + (str(sid) if sid
      else "\033[31mMISSING — not armed, whatever the badge says\033[0m"))
print("    operating_mode  = " + str(mode))
print("    execution_armed = " + str(armed) + "   (true only when mode is automatic)")
print("    feed            = " + str(feed))
print("    blockers        = " + (", ".join(map(str, blockers)) if blockers else "none"))
if mode == "manual_approval":
    print("    \033[33mmanual_approval places nothing until you approve each proposal,\033[0m")
    print("    \033[33mand the dashboard badge renders it identically to automatic.\033[0m")
PY
done
dim "  SYNCHRONIZED needs BOTH Binance sockets up and kline, bookTicker and"
dim "  markPrice all fresher than 15s at the same moment. Any one of them going"
dim "  stale stops fills on both labs and every instance simultaneously."

# ---------------------------------------------------------------- 5. research
bold "5. PA vs SMC standings (shadow research, needs no arming)"
CODE="$(fetch /research/observatory/comparison)"
if [ "$CODE" != "200" ]; then
  warn "GET /research/observatory/comparison returned $CODE"
else
  python3 - "$TMP/body" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("  could not parse the comparison"); raise SystemExit
rows = d.get("comparisons") or d.get("strategies") or []
if not rows:
    print("  No measurements recorded yet.")
    print("  The observer evaluates every closed candle, but resolves outcomes")
    print("  only while the feed is SYNCHRONIZED. Clear section 4 first.")
    raise SystemExit
print("  {:<5}{:<34}{:>6}{:>9}{:>8}{:>9}  {}".format(
      "rank", "strategy", "n", "exp R", "PF", "maxDD R", "state"))
for r in rows:
    pf = r.get("profit_factor")
    pf = "inf" if pf is None else "{:.2f}".format(float(pf))
    print("  {:<5}{:<34}{:>6}{:>9.3f}{:>8}{:>9.2f}  {}".format(
          r.get("rank", "?"),
          str(r.get("strategy_id"))[:33],
          int(r.get("sample_size", 0)),
          float(r.get("expectancy_r", 0)),
          pf,
          float(r.get("max_drawdown_r", 0)),
          r.get("validation_state")))
print()
print("  Ranked by expectancy in R, then profit factor, then drawdown.")
print("  Any verdict other than INSUFFICIENT_SAMPLE needs 100 closed trades.")
print("  PA_H_SR_REJECTION is the support/resistance rejection variant.")
PY
fi

bold "Summary"
dim "Work the sections in order. The first red one is the blocker, and the"
dim "sections below it cannot be judged until it is cleared."
dim ""
dim "This script changed nothing. Acting on what it finds is a separate,"
dim "deliberate step: fix env and redeploy, repair the ledger, free a slot,"
dim "or arm a session by setting its operating mode to automatic."
