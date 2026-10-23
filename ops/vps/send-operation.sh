#!/bin/sh
# Used by .github/workflows/vps-ops.yml (job "run", after approval). Run from the repository root.
# Sends one approved script to the VPS runner over SSH and streams its output back.
#
# Environment: OPERATION, ARGUMENT, EXPECTED_SHA256, GITHUB_SHA, GITHUB_RUN_ID,
#              VPS_HOST, VPS_SSH_KEY (secrets), VPS_HOST_KEY, VPS_SSH_PORT (variables)
set -eu
out=$(sh ops/vps/resolve-operation.sh "${OPERATION:-}" "${ARGUMENT:-}")
script=${out% *}
sum=${out##* }
[ "$sum" = "${EXPECTED_SHA256:-}" ] || { echo "the script differs from the one shown for approval; not sending"; exit 1; }
[ -n "${VPS_HOST:-}" ] || { echo "secret VPS_HOST is not set in the vps environment"; exit 1; }
[ -n "${VPS_SSH_KEY:-}" ] || { echo "secret VPS_SSH_KEY is not set in the vps environment"; exit 1; }
[ -n "${VPS_HOST_KEY:-}" ] || { echo "variable VPS_HOST_KEY is not set in the vps environment"; exit 1; }
port=${VPS_SSH_PORT:-22}
printf '%s' "$port" | grep -Eqx '[0-9]{1,5}' || { echo "variable VPS_SSH_PORT must be a port number"; exit 1; }

d=$(mktemp -d)
trap 'rm -rf "$d"' EXIT
printf '%s\n' "$VPS_SSH_KEY" | tr -d '\r' > "$d/key"
chmod 600 "$d/key"
if [ "$port" = 22 ]; then known=$VPS_HOST; else known="[$VPS_HOST]:$port"; fi
printf '%s %s\n' "$known" "$(printf '%s' "$VPS_HOST_KEY" | tr -d '\r')" > "$d/known_hosts"

ssh -F /dev/null -i "$d/key" -p "$port" \
    -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$d/known_hosts" -o GlobalKnownHostsFile=/dev/null \
    -o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 -o LogLevel=ERROR \
    "nexus-ops@$VPS_HOST" "run $OPERATION ${ARGUMENT:--} $sum ${GITHUB_SHA:?} ${GITHUB_RUN_ID:?}" < "$script"
