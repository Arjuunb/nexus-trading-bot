#!/bin/sh
# Used by .github/workflows/vps-ops.yml. Run from the repository root.
# Checks the requested operation and argument, and prints "<script path> <sha256>".
set -eu
op=${1:-}
arg=${2:-}
printf '%s' "$op" | grep -Eqx '[a-z0-9][a-z0-9-]{0,40}' || { echo "invalid operation name: '$op'" >&2; exit 1; }
if [ -n "$arg" ]; then
    printf '%s' "$arg" | grep -Eqx '[a-z0-9][a-z0-9-]{0,30}' || { echo "invalid argument: '$arg'" >&2; exit 1; }
fi
root=$(pwd -P)
file="$root/ops/vps/operations/$op.sh"
[ -f "$file" ] || { echo "no such operation: ops/vps/operations/$op.sh" >&2; exit 1; }
real=$(realpath "$file")
case "$real" in
    "$root"/*) ;;
    *) echo "operation points outside the repository" >&2; exit 1 ;;
esac
printf '%s %s\n' "${real#"$root"/}" "$(sha256sum "$real" | cut -d' ' -f1)"
