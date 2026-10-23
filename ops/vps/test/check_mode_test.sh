#!/bin/sh
# Tests for the no-approval read-only checks: ops/vps/check-operations.txt, check-mode.sh, the
# runner's built-in copy of the list, the owner-trusted fingerprints (trust-checks), and
# check-env-guard.py (against a fake GitHub API).
# Safe anywhere (no root, no Docker). Run from the repository root:  sh ops/vps/test/check_mode_test.sh
set -u
FAILS=0
ok() { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
yes_() { if sh ops/vps/check-mode.sh "$1" "$2"; then ok "check: $1 ${2:--}"; else bad "should be a check: $1 ${2:--}"; fi; }
no_() { if sh ops/vps/check-mode.sh "$1" "$2"; then bad "must need approval: $1 ${2:--}"; else ok "needs approval: $1 ${2:--}"; fi; }

echo "== the list and check-mode.sh"
yes_ hello ""
yes_ ops-log 40007923856
yes_ staging-inventory show
yes_ staging-backup status
yes_ staging-pa-off-check measure
yes_ staging-pa-off-stop verify
no_ staging-pa-off-stop stop
no_ p1b-stop-staging stop
no_ staging-pa-off-deploy apply
no_ staging-pa-off-deploy rollback
no_ staging-pa-off-deploy plan
no_ staging-backup start
no_ pa-backup start
no_ staging-pa-delete delete
no_ docker-cleanup apply
no_ staging-pa-off-images load
no_ hello x
no_ ops-log abc
no_ ops-log ""
no_ "hello;id" ""
no_ "" ""

echo "== the runner's built-in list is identical to check-operations.txt"
file_list=$(grep -v '^#' ops/vps/check-operations.txt | grep -v '^$' | sort)
runner_list=$( { sed -n '/^check_allowed() {/,/^}/p' ops/vps/nexus-ops-runner | grep -o '"[a-z0-9-]* [a-z0-9-][a-z0-9-]*"' | tr -d '"'
                 sed -n '/^check_allowed() {/,/^}/p' ops/vps/nexus-ops-runner | grep -o '"[a-z0-9-]* "\*)' | sed 's/"\(.*\) "\*)/\1 #/'; } | sort)
[ "$file_list" = "$runner_list" ] && ok "runner and file list the same $(printf '%s\n' "$file_list" | wc -l) checks" \
    || bad "runner and file differ: $(printf '%s\n' "$file_list" "---" "$runner_list" | tr '\n' '|')"
for op in $(printf '%s\n' "$file_list" | cut -d' ' -f1 | sort -u); do
    [ -e "ops/vps/operations/$op.sh" ] && ok "listed check exists: $op" || bad "listed check has no script: $op"
done

echo "== the runner in check mode: read-only list AND owner-trusted script fingerprints"
T=$(mktemp -d)
grep -q -- '-type f -user 0 ! -perm /022' ops/vps/nexus-ops-runner && ok "runner requires a root-owned, not group/world-writable trust file" || bad "trust file ownership check missing"
sed -e "s|^BASE=.*|BASE=$T/state|" -e "s|^LOGDIR=.*|LOGDIR=$T/log|" -e "s|^DISABLED=.*|DISABLED=$T/disabled|" \
    -e "s|^TRUSTED=.*|TRUSTED=$T/trusted|" -e "s|-user 0 |-user $(id -u) |" \
    -e 's|^\[ "$(id -u)" = 0 \] .*||' ops/vps/nexus-ops-runner > "$T/runner"
C=0000000000000000000000000000000000000000
H=$(sha256sum ops/vps/operations/hello.sh | cut -d' ' -f1)
creq() { sh "$T/runner" --check "run $1 $2 $(sha256sum "$3" | cut -d' ' -f1) $C $4" < "$3" 2>&1; }
out=$(creq hello - ops/vps/operations/hello.sh 101)
printf '%s' "$out" | grep -q "is not trusted for the check key" && ok "nothing runs before the owner trusts it" || bad "ran untrusted: $out"
printf 'hello %s\n' "$H" > "$T/trusted"; chmod 644 "$T/trusted"
out=$(creq hello - ops/vps/operations/hello.sh 102)
printf '%s' "$out" | grep -q ', check)' && printf '%s' "$out" | grep -q 'finished\|disk /:' && ok "trusted hello runs in check mode" || bad "trusted hello: $out"
grep -q 'run=102 op=hello .* mode=check started' "$T/log/runs.log" && ok "check mode recorded in runs.log" || bad "mode not in runs.log"
printf 'touch %s/pwned\necho payload-ran\n' "$T" > "$T/payload.sh"
out=$(creq hello - "$T/payload.sh" 103)
printf '%s' "$out" | grep -q "is not trusted for the check key" && [ ! -e "$T/pwned" ] \
    && ok "a replacement script under an allowed name is refused and never runs (Codex P1)" || bad "payload ran or was accepted: $out"
printf 'staging-inventory %s\n' "$H" > "$T/trusted"
out=$(creq hello - ops/vps/operations/hello.sh 104)
printf '%s' "$out" | grep -q "not trusted" && ok "a fingerprint trusted for another check does not count" || bad "cross-check trust: $out"
printf 'hello %s\n' "$H" > "$T/trusted"; chmod 664 "$T/trusted"
out=$(creq hello - ops/vps/operations/hello.sh 105)
printf '%s' "$out" | grep -q "not trusted" && ok "a group-writable trust file is ignored" || bad "writable trust file accepted: $out"
chmod 644 "$T/trusted"; mv "$T/trusted" "$T/real"; ln -s "$T/real" "$T/trusted"
out=$(creq hello - ops/vps/operations/hello.sh 106)
printf '%s' "$out" | grep -q "not trusted" && ok "a symlinked trust file is ignored" || bad "symlink accepted: $out"
rm -f "$T/trusted"; mv "$T/real" "$T/trusted"
printf 'p1b-stop-staging %s\nstaging-pa-delete %s\ntrust-checks %s\n' "$H" "$H" "$H" >> "$T/trusted"
for req in "staging-pa-off-deploy apply" "p1b-stop-staging stop" "staging-pa-delete delete" "trust-checks apply" "hello x" "ops-log abc" "selftest -"; do
    set -- $req
    out=$(creq "$1" "$2" ops/vps/operations/hello.sh 107)
    printf '%s' "$out" | grep -q "only start read-only checks" && ok "check mode refuses even when trusted: $req" || bad "check mode accepted: $req ($out)"
done
grep -q 'run=101\|run=103\|run=104\|run=105\|run=106\|run=107' "$T/log/runs.log" && bad "a refused check-mode request started a run" || ok "refused requests never start a run"
out=$(sh "$T/runner" --check < /dev/null 2>&1); printf '%s' "$out" | grep -q "exactly one request" && ok "check mode needs a request" || bad "$out"
out=$(sh "$T/runner" "run p1b-stop-staging verify $H $C 108" < ops/vps/operations/hello.sh 2>&1)
printf '%s' "$out" | grep -q ', approved)' && ok "main key path still runs anything approved (no trust file needed)" || bad "approved mode: $out"

echo "== trust-checks: generated list is current, and apply/show work"
sh ops/vps/make-trust-checks.sh "$T/fresh.sh" >/dev/null
cmp -s "$T/fresh.sh" ops/vps/operations/trust-checks.sh && ok "trust-checks.sh matches the current check scripts" \
    || bad "trust-checks.sh is out of date: run sh ops/vps/make-trust-checks.sh"
n=$(grep -cE '^[a-z0-9-]+ [0-9a-f]{64}$' ops/vps/operations/trust-checks.sh)
[ "$n" = "$(grep -v '^#' ops/vps/check-operations.txt | awk 'NF {print $1}' | sort -u | wc -l)" ] && ok "one fingerprint per listed check ($n)" || bad "fingerprints: $n"
if sh ops/vps/check-mode.sh trust-checks apply || sh ops/vps/check-mode.sh trust-checks show; then bad "trust-checks must need approval"; else ok "trust-checks always needs the owner's approval"; fi
out=$(NEXUS_OPS_TEST_TRUSTED=$T/applied sh ops/vps/operations/trust-checks.sh show); printf '%s' "$out" | grep -q "nothing trusted yet" && ok "show before apply" || bad "$out"
out=$(NEXUS_OPS_TEST_TRUSTED=$T/applied sh ops/vps/operations/trust-checks.sh apply)
[ "$(grep -c . "$T/applied")" = "$n" ] && [ "$(stat -c %a "$T/applied")" = 644 ] && ok "apply writes $n entries, mode 644" || bad "apply: $out"
grep -qxF "hello $H" "$T/applied" && ok "applied list holds the current hello fingerprint" || bad "hello missing"
cp "$T/applied" "$T/trusted"
out=$(creq hello - ops/vps/operations/hello.sh 109); printf '%s' "$out" | grep -q ', check)' && ok "after apply, the check key runs hello" || bad "$out"
out=$(sh ops/vps/operations/trust-checks.sh nope); printf '%s' "$out" | grep -q usage && ok "trust-checks rejects unknown argument" || bad "$out"
rm -rf "$T"

echo "== check-env-guard.py against a fake GitHub API"
T=$(mktemp -d)
cat > "$T/api.py" <<'PY'
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
case = json.load(open(sys.argv[1]))
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        assert self.headers.get("Authorization") == "Bearer t0ken"
        if self.path.endswith("/environments/vps-check"): body = case.get("env")
        elif self.path.endswith("/environments/vps-check/deployment-branch-policies"): body = case.get("policies")
        else: body = None
        if body is None:
            self.send_response(404); self.end_headers(); return
        b = json.dumps(body).encode(); self.send_response(200); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
s = HTTPServer(("127.0.0.1", 0), H); print(s.server_port, flush=True); s.serve_forever()
PY
guard() {  # guard <case json> -> prints exit code and message
    printf '%s' "$1" > "$T/case.json"
    python3 -I "$T/api.py" "$T/case.json" > "$T/port" & pid=$!
    i=0; while [ ! -s "$T/port" ] && [ $i -lt 50 ]; do sleep 0.1; i=$((i + 1)); done
    out=$(GITHUB_API_URL="http://127.0.0.1:$(cat "$T/port")" GITHUB_REPOSITORY=o/r GH_TOKEN=t0ken python3 -I ops/vps/check-env-guard.py); rc=$?
    kill $pid 2>/dev/null; wait $pid 2>/dev/null; : > "$T/port"
    printf '%s %s' "$rc" "$out"
}
SEL='{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}'
r=$(guard "{\"env\":$SEL,\"policies\":{\"branch_policies\":[{\"name\":\"main\",\"type\":\"branch\"}]}}")
case "$r" in "0 "*) ok "guard passes when vps-check is limited to main" ;; *) bad "guard: $r" ;; esac
r=$(guard "{\"env\":$SEL,\"policies\":{\"branch_policies\":[{\"name\":\"main\",\"type\":\"branch\"},{\"name\":\"claude/*\",\"type\":\"branch\"}]}}")
case "$r" in "1 "*"not only from main"*) ok "guard refuses main + claude/*" ;; *) bad "guard: $r" ;; esac
r=$(guard "{\"env\":$SEL,\"policies\":{\"branch_policies\":[{\"name\":\"main\",\"type\":\"tag\"}]}}")
case "$r" in "1 "*) ok "guard refuses a tag named main" ;; *) bad "guard: $r" ;; esac
r=$(guard "{\"env\":$SEL,\"policies\":{\"branch_policies\":[]}}")
case "$r" in "1 "*"no branch"*) ok "guard refuses an empty branch list" ;; *) bad "guard: $r" ;; esac
r=$(guard '{"env":{"deployment_branch_policy":null},"policies":{"branch_policies":[{"name":"main"}]}}')
case "$r" in "1 "*"not limited"*) ok "guard refuses 'all branches'" ;; *) bad "guard: $r" ;; esac
r=$(guard '{"env":{"deployment_branch_policy":{"protected_branches":true,"custom_branch_policies":false}}}')
case "$r" in "1 "*"not limited"*) ok "guard refuses 'protected branches'" ;; *) bad "guard: $r" ;; esac
r=$(guard '{}')
case "$r" in "1 "*"HTTP 404"*) ok "guard refuses when vps-check does not exist" ;; *) bad "guard: $r" ;; esac
r=$(GITHUB_API_URL=http://127.0.0.1:9 GITHUB_REPOSITORY=o/r GH_TOKEN=x python3 -I ops/vps/check-env-guard.py; echo " rc=$?")
case "$r" in *"rc=1") ok "guard refuses when GitHub cannot be reached" ;; *) bad "guard: $r" ;; esac
rm -rf "$T"

echo "check_mode_test: $FAILS failed"
[ "$FAILS" = 0 ]
