#!/bin/sh
# Used by .github/workflows/vps-ops.yml. Run from the repository root.
#   sh ops/vps/check-mode.sh <operation> [argument]
# Exit 0 if this operation + argument is a read-only check listed in ops/vps/check-operations.txt,
# exit 1 otherwise. Prints nothing.
set -eu
op=${1:-}
arg=${2:-}
[ -n "$arg" ] || arg=-
printf '%s' "$op" | grep -Eqx '[a-z0-9][a-z0-9-]{0,40}' || exit 1
[ "$arg" = - ] || printf '%s' "$arg" | grep -Eqx '[a-z0-9][a-z0-9-]{0,30}' || exit 1
while read -r lop larg rest; do
    case "$lop" in ''|'#'*) continue ;; esac
    [ "$lop" = "$op" ] || continue
    if [ "$larg" = "#" ]; then
        printf '%s' "$arg" | grep -Eqx '[0-9]{1,20}' && exit 0
    elif [ "$larg" = "$arg" ]; then
        exit 0
    fi
done < ops/vps/check-operations.txt
exit 1
