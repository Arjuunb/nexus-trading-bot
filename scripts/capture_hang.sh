#!/usr/bin/env bash
# Capture where a hanging endpoint is actually stuck.
#
# A hung request leaves no evidence. Nothing is raised, so the app log stays
# clean, and the caller sees only a gateway timeout. That is the state the
# Price Action status route is in: nginx cuts it off at ninety seconds while
# the log says nothing at all.
#
# So this starts the slow request, waits for it to be properly stuck, and then
# dumps every thread's stack from a second connection. The frame each thread is
# sitting in is the answer.
#
#     ./scripts/capture_hang.sh
#     ROUTE=/research/smc/bot-status ./scripts/capture_hang.sh
#
# Read-only. It issues GET requests and inspects stacks; it places no order,
# changes no configuration and writes to no database.

set -uo pipefail

ROUTE="${ROUTE:-/research/price-action/bot-status}"
WAIT="${WAIT:-20}"

if [ -z "${HUB_CONTROL_KEY:-}" ] && [ -f .env ]; then
  HUB_CONTROL_KEY="$(sed -n 's/^HUB_CONTROL_KEY=//p' .env | head -1 | tr -d '"'\''')"
fi
KEY="${HUB_CONTROL_KEY:-}"

if [ -z "$KEY" ]; then
  echo "No HUB_CONTROL_KEY found. Run this from the deploy directory." >&2
  exit 1
fi
if ! docker compose exec -T app python -c "pass" >/dev/null 2>&1; then
  echo "Cannot exec into the app container. Is the stack up?" >&2
  exit 1
fi

printf '\033[1mCapturing a hang on %s\033[0m\n' "$ROUTE"
echo "Starting the request, then dumping thread stacks after ${WAIT}s."
echo

docker compose exec -T -e ROUTE="$ROUTE" -e KEY="$KEY" -e WAIT="$WAIT" app python - <<'PY'
import json, os, threading, time, urllib.request, urllib.error

route, key, wait = os.environ["ROUTE"], os.environ["KEY"], float(os.environ["WAIT"])
BASE = "http://127.0.0.1:8000"
result = {}


def call(path, timeout, into):
    request = urllib.request.Request(BASE + path)
    request.add_header("X-Webhook-Secret", key)
    started = time.monotonic()
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
        into["code"] = response.getcode()
        into["body"] = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        into["code"] = exc.code
        into["body"] = exc.read().decode("utf-8", "replace")
    except Exception as exc:
        into["code"] = 0
        into["body"] = "%s: %s" % (type(exc).__name__, exc)
    into["seconds"] = time.monotonic() - started


# Start the slow call and leave it running while we look at the process.
worker = threading.Thread(target=call, args=(route, 180, result), daemon=True)
worker.start()
time.sleep(wait)

dump = {}
call("/system/threads?frames=16", 30, dump)
if dump.get("code") != 200:
    print("Could not read /system/threads: %s %s" % (dump.get("code"), dump.get("body")[:200]))
    raise SystemExit(1)

threads = json.loads(dump["body"])
print("observed_at %s   %d threads   (the slow call is still running)"
      % (threads["observed_at"], threads["thread_count"]))
print()

# Anything sitting in a lock, a socket read or a sleep is what matters; the
# rest are idle workers parked in the same place and only add noise.
INTERESTING = ("acquire", "wait", "join", "recv", "read", "select", "poll",
               "sleep", "lock", "urlopen", "connect")
for row in threads["threads"]:
    tail = row["stack"][-1] if row["stack"] else ""
    parked = "threading.py" in tail and "wait" in tail and "Thread-" not in row["name"]
    if parked and not any(word in tail.lower() for word in ("lock", "acquire")):
        continue
    print("\033[1m%s\033[0m  (daemon=%s)" % (row["name"], row["daemon"]))
    for entry in row["stack"]:
        mark = "  -> " if entry is tail else "     "
        print(mark + entry)
    print()

worker.join(5)
print("=" * 72)
print("the %s call: HTTP %s after %.1fs" % (route, result.get("code"), result.get("seconds", 0)))
body = (result.get("body") or "")[:300]
if body:
    print(body)
PY
