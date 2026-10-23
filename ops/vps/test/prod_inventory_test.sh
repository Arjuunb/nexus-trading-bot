#!/bin/sh
# Test of ops/vps/operations/prod-inventory.sh. DISPOSABLE DOCKER HOSTS ONLY.
#   NEXUS_PROD_INVENTORY_TEST=disposable sh ops/vps/test/prod_inventory_test.sh   (from the repository root, as root)
set -u
[ "${NEXUS_PROD_INVENTORY_TEST:-}" = disposable ] || { echo "refused: set NEXUS_PROD_INVENTORY_TEST=disposable (never on the VPS)"; exit 3; }
[ "$(hostname)" = vps-46469a44 ] && { echo "refused: this is the VPS"; exit 3; }
FAILS=0
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
has() { if printf '%s' "$2" | grep -q -- "$3"; then ok "$1"; else bad "$1 (no '$3' in: $(printf '%s' "$2" | tail -n 3 | tr '\n' '|'))"; fi; }
hasnt() { if printf '%s' "$2" | grep -q -- "$3"; then bad "$1 (found '$3')"; else ok "$1"; fi; }
S=$(pwd)/ops/vps/operations/prod-inventory.sh
P=nexus-trading-bot
T=$(mktemp -d)
SIG='import signal,sys,time; signal.signal(signal.SIGTERM, lambda *a: sys.exit(0)); time.sleep(99999)'

cleanup() { (cd "$T/checkout" 2>/dev/null && docker compose -p $P down -v >/dev/null 2>&1); rm -rf "$T"; }
trap cleanup EXIT

mkdir -p "$T/checkout/scripts" && cd "$T/checkout" && git init -q
cat > compose.yaml <<EOF
services:
  app:
    image: python:3.12-alpine
    command: ["python", "-c", "$SIG"]
    env_file: .env
    environment:
      HUB_ENV: production
    volumes:
      - tradexa-data:/var/lib/tradexa
volumes:
  tradexa-data:
EOF
printf 'HUB_AUTH_MODE=supabase\nSUPABASE_URL=https://prod-secret-host.supabase.co\nSUPABASE_KEY=SUPERSECRETk1\nHUB_SECRET=SUPERSECRETs2\n' > .env
echo 'echo deploy' > scripts/deploy.sh
git add compose.yaml scripts && git -c user.email=t@example.invalid -c user.name=t commit -qm init
docker compose -p $P up -d >/dev/null 2>&1 || { echo "FAIL  could not start the stand-in"; exit 1; }
cd - >/dev/null
V=$(docker volume inspect -f '{{.Mountpoint}}' ${P}_tradexa-data)
python3 - "$V" <<'EOF'
import os, sqlite3, sys
d = sys.argv[1]
sqlite3.connect(os.path.join(d, "smc_strategy_paper.db")).execute("CREATE TABLE t(x)")
db = sqlite3.connect(os.path.join(d, "price_action_paper.db"))
db.execute("PRAGMA journal_mode=WAL")
db.execute("CREATE TABLE v2_positions(symbol TEXT)"); db.execute("INSERT INTO v2_positions VALUES('BTCUSDT')")
db.execute("CREATE TABLE v2_orders(id TEXT, status TEXT)"); db.execute("INSERT INTO v2_orders VALUES('a','OPEN'),('b','FILLED')")
db.commit(); db.close()
for f in os.listdir(d): os.chown(os.path.join(d, f), 100, 101)
EOF
snap() { (cd "$V" && ls -la --time-style=+%s.%N && sha256sum ./*) | md5sum; }

out=$(sh "$S" bogus 2>&1); has "rejects an unknown argument" "$out" "usage: prod-inventory.sh show"
before=$(snap)
out=$(sh "$S" show 2>&1)
has "finds the app" "$out" "nexus-trading-bot-app-1  service=app  state=running"
has "shows the sign-in mode" "$out" "HUB_AUTH_MODE=supabase"
has "reports the PA flag unset" "$out" "PRICE_ACTION_LAB_ENABLED is not set"
hasnt "never prints a secret" "$out" "SUPERSECRET"
hasnt "never prints the Supabase address" "$out" "prod-secret-host"
has "names secrets only" "$out" "SUPABASE_KEY (set)"
has "reads the checkout" "$out" "git tracked files changed in the checkout: 0"
has "finds the deploy script" "$out" "scripts/deploy.sh present"
has "reports file owners" "$out" "file owners 100:101"
has "counts open PA positions" "$out" "v2_positions: 1 open positions"
has "counts open PA orders" "$out" "v2_orders: 1 open"
has "ends with nothing changed" "$out" "Nothing was changed."
[ "$(snap)" = "$before" ] && ok "data volume unchanged (no -wal/-shm created)" || bad "data volume changed"

# A -wal without -shm is read from a private copy, never in place.
python3 - "$V" <<'EOF'
import os, sqlite3, sys
d = sys.argv[1]
db = sqlite3.connect(os.path.join(d, "price_action_paper.db")); db.execute("PRAGMA wal_autocheckpoint=0")
db.execute("INSERT INTO v2_positions VALUES('ETHUSDT')"); db.commit()
os.system("cp %s/price_action_paper.db-wal %s/keep-wal" % (d, d)); db.close()
os.replace(os.path.join(d, "keep-wal"), os.path.join(d, "price_action_paper.db-wal"))
for f in os.listdir(d):
    os.chown(os.path.join(d, f), 100, 101)
    if f.endswith("-shm"): os.remove(os.path.join(d, f))
EOF
before=$(snap)
out=$(sh "$S" show 2>&1)
has "sees rows still in the -wal" "$out" "v2_positions: 2 open positions"
[ "$(snap)" = "$before" ] && ok "volume unchanged when only a -wal exists" || bad "volume changed (-wal case)"
ls -d /tmp/prod-inventory-* >/dev/null 2>&1 && bad "a private copy was left in /tmp" || ok "private copy removed"

[ "$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' ${P}-app-1)" = "true 0" ] && ok "app left running, never restarted" || bad "app state changed"
echo "== $FAILS failed"
[ "$FAILS" = 0 ]
