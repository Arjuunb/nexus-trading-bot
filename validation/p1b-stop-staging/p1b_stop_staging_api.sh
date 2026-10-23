#!/bin/sh
# Nexus P1-B: stop the staging API container so the staging lab databases stop growing.
#
#   sudo sh p1b_stop_staging_api.sh check    read-only preflight; ends with GO or NO-GO
#   sudo sh p1b_stop_staging_api.sh stop     re-runs the preflight, then stops ONLY the staging API container
#   sudo sh p1b_stop_staging_api.sh verify   read-only; confirms staging stayed stopped and production is healthy
#
# What "stop" changes: the running state of one container, nexus-remote-stage-rc3-28acdbc-api-1.
# If Docker would start that container again after a reboot, it also sets that container's
# restart policy to "no" (and prints the command that undoes it). Nothing else.
#
# What it never does: touch any production container, delete or move any file or database,
# change any configuration file, stop the other staging containers, or print secret values.
set -u
umask 077

PROJECT=nexus-remote-stage-rc3-28acdbc
API="$PROJECT-api-1"
STATE_VOLUME="${PROJECT}_api-state"
EXPECTED_REVISION=f8778ed42039d415f3829e08a05225976fe0d0a5
PROD=nexus-trading-bot-app-1
PROD_PROJECT=nexus-trading-bot
STOP_TIMEOUT=120
WORK=/root/p1b-staging
LAB_FILES="price_action_paper.db price_action_paper.db-wal smc_strategy_paper.db smc_strategy_paper.db-wal"

say() { printf '%s\n' "$*"; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
nogo() { say "NO-GO: $*"; say "Nothing was changed."; exit 2; }
insp() { docker inspect -f "$2" "$1" 2>/dev/null; }
data_volume() { insp "$1" '{{range .Mounts}}{{if eq .Destination "/var/lib/tradexa"}}{{.Name}}{{end}}{{end}}'; }
data_dir() { insp "$1" '{{range .Mounts}}{{if eq .Destination "/var/lib/tradexa"}}{{.Source}}{{end}}{{end}}'; }

# Host part of a container's SUPABASE_URL, lower-cased. Held in memory only; never printed.
supabase_host() {
    docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" 2>/dev/null \
        | sed -n 's/^SUPABASE_URL=//p' | head -n 1 \
        | sed -e 's#^[A-Za-z][A-Za-z0-9+.-]*://##' -e 's#[/?#].*$##' | tr 'A-Z' 'a-z'
}

lab_sizes() {  # one line per file: name size mtime
    dir=$(data_dir "$API")
    for f in $LAB_FILES; do
        if [ -n "$dir" ] && [ -e "$dir/$f" ]; then
            say "$f $(stat -c %s "$dir/$f") $(date -u -r "$dir/$f" +%Y-%m-%dT%H:%M:%SZ)"
        else
            say "$f absent -"
        fi
    done
}

production_status() {
    ip=$(insp "$PROD" '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' | awk '{print $1}')
    if [ -z "$ip" ]; then say "production endpoints: no container address"; return; fi
    python3 -I - "$ip" <<'PY'
import json, sys, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(path):
    with opener.open("http://%s:8000%s" % (sys.argv[1], path), timeout=30) as r:
        return json.load(r)
try:
    h = get("/health")
    print("production /health: %s, commit %s" % (h.get("status"), h.get("commit_short")))
except Exception as e:
    print("production /health: no answer (%s)" % type(e).__name__)
try:
    s = get("/status/public")
    parts = ["%s %s (%s)" % (c.get("id"), c.get("state"), c.get("detail")) for c in s.get("components", [])]
    print("production status: %s; %s" % (s.get("overall"), "; ".join(parts)))
except Exception as e:
    print("production status: no answer (%s)" % type(e).__name__)
PY
}

show_state() {
    say "time:         $(now)"
    say "staging api:  $(insp "$API" 'status={{.State.Status}} exit_code={{.State.ExitCode}} restart_policy={{.HostConfig.RestartPolicy.Name}}:{{.HostConfig.RestartPolicy.MaximumRetryCount}}')"
    say "production:   $(insp "$PROD" 'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{end}} started={{.State.StartedAt}}')"
    production_status
    say "staging lab files (bytes, last modified):"
    lab_sizes | sed 's/^/  /'
    df -h / | awk 'NR==2 {print "disk /:       size " $2 ", used " $3 ", free " $4 " (" $5 " used)"}'
}

preflight() {
    [ "$(id -u)" = 0 ] || nogo "run it with sudo"
    command -v docker >/dev/null 2>&1 || nogo "docker command not found"
    docker info >/dev/null 2>&1 || nogo "cannot reach the Docker daemon"

    # 1. The target is exactly the staging API container measured in P0.
    [ "$(insp "$API" '{{index .Config.Labels "com.docker.compose.project"}}')" = "$PROJECT" ] \
        || nogo "$API not found, or not part of $PROJECT"
    [ "$(insp "$API" '{{index .Config.Labels "com.docker.compose.service"}}')" = api ] \
        || nogo "$API is not the staging 'api' service"
    [ "$(insp "$API" '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$EXPECTED_REVISION" ] \
        || nogo "$API is not running the staging build $EXPECTED_REVISION"
    [ "$(data_volume "$API")" = "$STATE_VOLUME" ] \
        || nogo "$API does not use the data volume $STATE_VOLUME"
    say "ok: target is $API (staging build f8778ed, data volume $STATE_VOLUME)"

    # 2. Production is a different container with its own data volume.
    [ "$(insp "$PROD" '{{index .Config.Labels "com.docker.compose.project"}}')" = "$PROD_PROJECT" ] \
        || nogo "production container $PROD not found"
    prod_volume=$(data_volume "$PROD")
    [ -n "$prod_volume" ] && [ "$prod_volume" != "$STATE_VOLUME" ] \
        || nogo "could not confirm production uses a separate data volume"
    say "ok: production $PROD uses its own data volume ($prod_volume)"

    # 3. Staging and production use different Supabase projects, so staging's shutdown
    #    cannot write to production's ledger or trading-instance tables.
    staging_host=$(supabase_host "$API")
    prod_host=$(supabase_host "$PROD")
    [ -n "$staging_host" ] || nogo "staging SUPABASE_URL not found; send this output to Claude"
    [ -n "$prod_host" ] || nogo "production SUPABASE_URL not found; send this output to Claude"
    [ "$staging_host" != "$prod_host" ] \
        || nogo "staging and production use the SAME Supabase project; do not stop it, send this output to Claude"
    say "ok: staging and production use different Supabase projects (addresses compared, not shown)"
}

cmd_check() {
    preflight
    show_state
    say "GO: preflight passed. Nothing was changed."
}

cmd_stop() {
    preflight
    mkdir -p "$WORK"
    say "--- before"
    show_state
    status=$(insp "$API" '{{.State.Status}}')
    if [ "$status" = running ]; then
        lab_sizes > "$WORK/sizes-before-stop.txt"
        insp "$API" '{{.HostConfig.RestartPolicy.Name}}:{{.HostConfig.RestartPolicy.MaximumRetryCount}}' \
            > "$WORK/restart-policy-before.txt"
        say "--- $(now) stopping $API (waits up to ${STOP_TIMEOUT}s for a clean shutdown)"
        started=$(date +%s)
        if ! docker stop -t "$STOP_TIMEOUT" "$API" >/dev/null; then
            say "ERROR: docker stop failed; state below"
            show_state
            exit 1
        fi
        say "stopped after $(( $(date +%s) - started ))s"
    else
        say "--- $API is already '$status'; not stopping anything"
    fi

    # Docker restarts a stopped container after a reboot or daemon restart when its policy is
    # "always", or "on-failure" and it exited with a non-zero code. Prevent that.
    policy=$(insp "$API" '{{.HostConfig.RestartPolicy.Name}}')
    code=$(insp "$API" '{{.State.ExitCode}}')
    if [ "$policy" = always ] || { [ "$policy" = on-failure ] && [ "$code" != 0 ]; }; then
        if docker update --restart=no "$API" >/dev/null; then
            say "restart policy changed to 'no' (exit code $code would have restarted it after a reboot)"
        else
            say "WARNING: could not change the restart policy; it may start again after a reboot"
        fi
    else
        say "restart policy left as '$policy' (exit code $code: it stays stopped after a reboot)"
    fi

    lab_sizes > "$WORK/sizes-after-stop.txt"
    say "--- after"
    show_state
    if [ "$(insp "$API" '{{.State.Status}}')" = exited ]; then
        say "DONE: staging API stopped. Run 'verify' in 10 minutes."
    else
        say "WARNING: staging API is '$(insp "$API" '{{.State.Status}}')', not 'exited'. Send this output to Claude."
    fi
}

cmd_verify() {
    [ "$(id -u)" = 0 ] || nogo "run it with sudo"
    show_state
    status=$(insp "$API" '{{.State.Status}}')
    if [ "$status" = exited ]; then
        say "ok: staging API is still stopped"
    else
        say "WARNING: staging API is '$status'"
    fi
    if [ -f "$WORK/sizes-after-stop.txt" ]; then
        lab_sizes > "$WORK/sizes-verify.txt"
        if cmp -s "$WORK/sizes-after-stop.txt" "$WORK/sizes-verify.txt"; then
            say "ok: staging lab files unchanged since the stop"
        else
            say "CHANGED since the stop:"
            diff "$WORK/sizes-after-stop.txt" "$WORK/sizes-verify.txt" \
                | sed -n -e 's/^< /  at stop: /p' -e 's/^> /  now:     /p'
        fi
    else
        say "no record from the stop step in $WORK"
    fi
    say "Nothing was changed."
}

case "${1:-}" in
    check) cmd_check ;;
    stop) cmd_stop ;;
    verify) cmd_verify ;;
    *) say "usage: sudo sh $0 check|stop|verify"; exit 64 ;;
esac
