#!/bin/sh
# Tests for the no-approval read-only checks: ops/vps/check-operations.txt, check-mode.sh, the
# runner's built-in copy of the list, and check-env-guard.py (against a fake GitHub API).
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

echo "== the runner refuses changes in check mode (as the runner sees them, before any script is read)"
T=$(mktemp -d)
sed -e "s|^BASE=.*|BASE=$T/state|" -e "s|^LOGDIR=.*|LOGDIR=$T/log|" -e "s|^DISABLED=.*|DISABLED=$T/disabled|" \
    -e 's|^\[ "$(id -u)" = 0 \] .*||' ops/vps/nexus-ops-runner > "$T/runner"
printf 'echo check-ran\n' > "$T/s.sh"; S=$(sha256sum "$T/s.sh" | cut -d' ' -f1); C=0000000000000000000000000000000000000000
out=$(sh "$T/runner" --check "run hello - $S $C 101" < "$T/s.sh" 2>&1)
printf '%s' "$out" | grep -q '^check-ran$' && ok "check mode runs an allowed check" || bad "check mode did not run hello: $out"
printf '%s' "$out" | grep -q ', check)' && ok "check mode is shown in the output" || bad "mode not shown"
grep -q 'run=101 op=hello .* mode=check started' "$T/log/runs.log" && ok "check mode recorded in runs.log" || bad "mode not in runs.log"
for req in "staging-pa-off-deploy apply" "p1b-stop-staging stop" "staging-pa-delete delete" "hello x" "ops-log abc" "selftest -"; do
    out=$(sh "$T/runner" --check "run $req $S $C 102" < "$T/s.sh" 2>&1)
    printf '%s' "$out" | grep -q "only start read-only checks" && ok "check mode refuses: $req" || bad "check mode accepted: $req ($out)"
done
grep -q 'run=102' "$T/log/runs.log" && bad "a refused check-mode request started a run" || ok "refused requests never start a run"
out=$(sh "$T/runner" --check < "$T/s.sh" 2>&1); printf '%s' "$out" | grep -q "exactly one request" && ok "check mode needs a request" || bad "$out"
out=$(sh "$T/runner" "run p1b-stop-staging verify $S $C 103" < "$T/s.sh" 2>&1)
printf '%s' "$out" | grep -q ', approved)' && ok "main key path still runs (approved mode)" || bad "approved mode: $out"
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
