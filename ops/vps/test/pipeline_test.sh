#!/bin/sh
# End-to-end test of the VPS operations pipeline. DISPOSABLE TEST HOSTS ONLY (it touches
# /etc/nexus-ops and the nexus-ops runner). Run from the repository root, as root, after:
#   sudo sh ops/vps/setup-vps-ops.sh ops/vps/nexus-ops-runner > setup.out   (save the key + host key)
#   /usr/sbin/sshd -p 2222
#   P1B_REPLICA_TEST=disposable sh validation/p1b-stop-staging/replica-test/setup.sh   (for the P1-B part)
# Then:
#   NEXUS_OPS_TEST=disposable KEY_FILE=key HOST_KEY_FILE=hostkey PORT=2222 sh ops/vps/test/pipeline_test.sh
set -u
[ "${NEXUS_OPS_TEST:-}" = disposable ] || { echo "refused: set NEXUS_OPS_TEST=disposable (never on the VPS)"; exit 3; }
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 3; }
FAILS=0
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
expect() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$3', got '$2')"; fi; }
has() { if printf '%s' "$2" | grep -q -- "$3"; then ok "$1"; else bad "$1 (no '$3' in: $(printf '%s' "$2" | tail -n 3 | tr '\n' '|'))"; fi; }

export VPS_HOST=127.0.0.1 VPS_SSH_PORT="${PORT:?}" GITHUB_SHA
VPS_SSH_KEY=$(cat "${KEY_FILE:?}"); VPS_HOST_KEY=$(cat "${HOST_KEY_FILE:?}"); export VPS_SSH_KEY VPS_HOST_KEY
GITHUB_SHA=$(git rev-parse HEAD)
RUN=$(date +%s)000
send() {  # operation, argument, run id: exactly what the workflow's "run" job does
    out=$(sh ops/vps/resolve-operation.sh "$1" "$2" 2>&1) || { printf '%s\n' "$out"; return 9; }
    OPERATION=$1 ARGUMENT=$2 EXPECTED_SHA256=${out##* } GITHUB_RUN_ID=$3 sh ops/vps/send-operation.sh 2>&1
}
raw() {  # direct ssh with the same key, to try things the workflow would never send
    k=$(mktemp); printf '%s\n' "$VPS_SSH_KEY" > "$k"; kh=$(mktemp); printf '[%s]:%s %s\n' "$VPS_HOST" "$PORT" "$VPS_HOST_KEY" > "$kh"
    ssh -F /dev/null -i "$k" -p "$PORT" -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile="$kh" \
        -o StrictHostKeyChecking=yes -o LogLevel=ERROR "$@" 2>&1
    r=$?; rm -f "$k" "$kh"; return $r
}

echo "== hello through the full path (workflow script -> ssh -> forced command -> sudo -> runner)"
out=$(send hello "" $((RUN + 1))); code=$?
expect "hello exit 0" "$code" 0
has "hello shows disk" "$out" "^disk /:"
has "hello shows containers" "$out" "nexus-trading-bot-app-1: Up"
has "runner reports exit code" "$out" "nexus-ops: finished with exit code 0"

echo "== P1-B through the pipeline (replica containers)"
out=$(send p1b-stop-staging check $((RUN + 2))); has "P1-B check says GO" "$out" "^GO: preflight passed"
out=$(send p1b-stop-staging stop $((RUN + 3))); code=$?
expect "P1-B stop exit 0" "$code" 0
has "P1-B stop says DONE" "$out" "^DONE: staging API stopped"
expect "staging replica stopped" "$(docker inspect -f '{{.State.Status}}' nexus-remote-stage-rc3-28acdbc-api-1)" exited
expect "production replica running" "$(docker inspect -f '{{.State.Status}}' nexus-trading-bot-app-1)" running
out=$(send p1b-stop-staging verify $((RUN + 4))); has "P1-B verify unchanged" "$out" "ok: staging lab files unchanged"
out=$(send ops-log "$((RUN + 3))" $((RUN + 5))); has "ops-log shows earlier run" "$out" "finished, exit code 0"

echo "== refusals"
out=$(OPERATION=hello ARGUMENT= EXPECTED_SHA256=0000 GITHUB_RUN_ID=$((RUN + 6)) sh ops/vps/send-operation.sh 2>&1); code=$?
has "client refuses a script that differs from the approved one" "$out" "differs from the one shown for approval"
out=$(printf 'echo tampered\n' | raw nexus-ops@$VPS_HOST "run hello - $(sha256sum ops/vps/operations/hello.sh | cut -d' ' -f1) $GITHUB_SHA $((RUN + 7))")
has "runner refuses content that does not match the sha256" "$out" "does not match the approved"
out=$(raw nexus-ops@$VPS_HOST 'run hello - $(id) x y'); has "runner refuses shell characters" "$out" "characters that are not allowed"
out=$(raw nexus-ops@$VPS_HOST "id"); has "arbitrary command is refused" "$out" "expected: run <operation>"
out=$(raw nexus-ops@$VPS_HOST < /dev/null); has "plain login is refused" "$out" "refused"
raw -N -L 127.0.0.1:2299:127.0.0.1:"$PORT" nexus-ops@$VPS_HOST > /dev/null &
fwd=$!; sleep 3
banner=$(python3 -I -c '
import socket
s = socket.create_connection(("127.0.0.1", 2299), timeout=5)
s.settimeout(5)
try:
    print(s.recv(64).decode(errors="replace").strip())
except Exception:
    print("")
' 2>/dev/null)
kill $fwd 2>/dev/null; pkill -f "2299:127.0.0.1" 2>/dev/null
case "$banner" in SSH-*) bad "port forwarding should be refused (got through: $banner)" ;; *) ok "port forwarding refused (restrict)" ;; esac
out=$(send hello "" $((RUN + 1))); has "same run id twice is refused" "$out" "already happened"
out=$(send 'Bad;Name' "" $((RUN + 8))); has "bad operation name refused before sending" "$out" "invalid operation name"
out=$(send hello 'a b' $((RUN + 9))); has "bad argument refused before sending" "$out" "invalid argument"

if [ -n "${CHECK_KEY_FILE:-}" ]; then
    echo "== check key (GitHub environment vps-check, no per-run approval): read-only checks only"
    MAIN_KEY=$VPS_SSH_KEY; VPS_SSH_KEY=$(cat "$CHECK_KEY_FILE"); export VPS_SSH_KEY
    rm -f /etc/nexus-ops/trusted-checks
    out=$(send hello "" $((RUN + 29))); has "check key runs nothing before the owner trusts it" "$out" "is not trusted for the check key"
    VPS_SSH_KEY=$MAIN_KEY; export VPS_SSH_KEY
    out=$(send trust-checks apply $((RUN + 28))); has "owner-approved trust-checks apply (main key)" "$out" "Only these exact scripts can run without approval"
    expect "trust file is root-owned 644" "$(stat -c '%U %a' /etc/nexus-ops/trusted-checks)" "root 644"
    VPS_SSH_KEY=$(cat "$CHECK_KEY_FILE"); export VPS_SSH_KEY
    out=$(send trust-checks apply $((RUN + 27))); has "check key cannot change the trust list" "$out" "only start read-only checks"
    printf 'touch /tmp/nexus-ops-pwned\necho payload-ran\n' > /tmp/nexus-ops-payload.sh; rm -f /tmp/nexus-ops-pwned
    out=$(raw nexus-ops@$VPS_HOST "run hello - $(sha256sum /tmp/nexus-ops-payload.sh | cut -d' ' -f1) $GITHUB_SHA $((RUN + 26))" < /tmp/nexus-ops-payload.sh)
    has "check key: replacement script under an allowed name refused (Codex P1)" "$out" "is not trusted for the check key"
    [ ! -e /tmp/nexus-ops-pwned ] && ok "the replacement script never ran" || bad "the replacement script RAN"
    rm -f /tmp/nexus-ops-payload.sh /tmp/nexus-ops-pwned
    out=$(send hello "" $((RUN + 30))); has "check key runs trusted hello" "$out" "finished with exit code 0"
    has "runner shows check mode" "$out" ", check)"
    out=$(send p1b-stop-staging verify $((RUN + 31))); has "check key runs a read-only P1-B verify" "$out" "Nothing was changed."
    out=$(send ops-log "$((RUN + 30))" $((RUN + 32))); has "check key runs ops-log" "$out" "finished, exit code 0"
    before=$(docker inspect -f '{{.State.Status}}' nexus-remote-stage-rc3-28acdbc-api-1)
    for req in "p1b-stop-staging stop" "staging-pa-off-deploy apply" "staging-pa-delete delete" "docker-cleanup apply" "staging-backup start"; do
        set -- $req
        out=$(send "$1" "$2" $((RUN + 33))); has "check key refused: $req" "$out" "only start read-only checks"
    done
    expect "nothing changed by the refused requests" "$(docker inspect -f '{{.State.Status}}' nexus-remote-stage-rc3-28acdbc-api-1)" "$before"
    grep -q "run=$((RUN + 33))" /var/log/nexus-ops/runs.log && bad "a refused check-key request started a run" || ok "refused check-key requests never start a run"
    out=$(raw nexus-ops@$VPS_HOST "id"); has "check key: arbitrary command refused" "$out" "refused"
    out=$(raw nexus-ops@$VPS_HOST < /dev/null); has "check key: plain login refused" "$out" "refused"
    keys=$(grep -c '^restrict,command=' /var/lib/nexus-ops/.ssh/authorized_keys)
    expect "authorized_keys holds the main and the check key" "$keys" 2
    grep -q 'nexus-ops-runner --check \\"\$SSH_ORIGINAL_COMMAND\\"' /var/lib/nexus-ops/.ssh/authorized_keys \
        && ok "check key is forced into check mode" || bad "check key line is not in check mode"
    VPS_SSH_KEY=$MAIN_KEY; export VPS_SSH_KEY
    out=$(send hello "" $((RUN + 34))); has "main key still runs in approved mode" "$out" ", approved)"
fi

echo "== off switch"
touch /etc/nexus-ops/disabled
out=$(send hello "" $((RUN + 10))); has "off switch refuses" "$out" "switched off"
rm -f /etc/nexus-ops/disabled
out=$(send hello "" $((RUN + 11))); has "works again after the switch is removed" "$out" "finished with exit code 0"

echo "== one at a time, and a dropped connection does not stop the script"
mkdir -p ops/vps/operations
cat > ops/vps/operations/test-slow.sh <<'EOF'
#!/bin/sh
echo "slow: started"; sleep 8; echo done > /tmp/nexus-ops-slow-marker; echo "slow: finished"
EOF
cat > ops/vps/operations/test-bg.sh <<'EOF'
#!/bin/sh
sleep 30 > /dev/null 2>&1 &
echo "bg: left a background process"
EOF
rm -f /tmp/nexus-ops-slow-marker
send test-slow "" $((RUN + 12)) > /tmp/nexus-ops-slow.out &
client=$!; sleep 3
out=$(send hello "" $((RUN + 13))); has "second operation refused while one runs" "$out" "another operation is still running"
kill $client 2>/dev/null; pkill -f "ssh -F /dev/null -i .* nexus-ops@127.0.0.1 run test-slow" 2>/dev/null
sleep 9
expect "script kept running after the connection dropped" "$(cat /tmp/nexus-ops-slow-marker 2>/dev/null)" done
expect "its exit code was recorded" "$(cat /var/log/nexus-ops/$((RUN + 12))-test-slow.rc 2>/dev/null)" 0
out=$(send test-bg "" $((RUN + 14))); has "operation that leaves a background process" "$out" "finished with exit code 0"
out=$(send hello "" $((RUN + 15))); has "lock released despite the leftover background process" "$out" "finished with exit code 0"
rm -f ops/vps/operations/test-slow.sh ops/vps/operations/test-bg.sh

echo "== time limit (runner copy with a 3 s limit, run directly as root)"
sed 's/^TIME_LIMIT=1800$/TIME_LIMIT=3/' ops/vps/nexus-ops-runner > /tmp/nexus-ops-runner-fast
printf 'sleep 20\n' > /tmp/nexus-ops-sleep.sh
s=$(sha256sum /tmp/nexus-ops-sleep.sh | cut -d' ' -f1)
out=$(sh /tmp/nexus-ops-runner-fast "run test-timeout - $s $GITHUB_SHA $((RUN + 16))" < /tmp/nexus-ops-sleep.sh); code=$?
expect "time limit stops the script (exit 124)" "$code" 124
rm -f /tmp/nexus-ops-runner-fast /tmp/nexus-ops-sleep.sh /tmp/nexus-ops-slow-marker /tmp/nexus-ops-slow.out

echo "== audit trail"
grep -q "run=$((RUN + 3)) op=p1b-stop-staging arg=stop commit=$GITHUB_SHA sha256=" /var/log/nexus-ops/runs.log \
    && ok "start of the P1-B stop recorded with commit and sha256" || bad "start record missing"
grep -q "run=$((RUN + 3)) op=p1b-stop-staging finished exit=0" /var/log/nexus-ops/runs.log \
    && ok "end of the P1-B stop recorded with exit code" || bad "end record missing"
grep -q "run=$((RUN + 7))" /var/log/nexus-ops/runs.log \
    && bad "a refused request was recorded as a run" || ok "refused requests never start a run"
[ -f "/var/lib/nexus-ops/state/scripts/$(sha256sum ops/vps/operations/hello.sh | cut -d' ' -f1).sh" ] \
    && ok "script kept by its sha256" || bad "script copy missing"

echo "failures: $FAILS"
[ "$FAILS" = 0 ]
