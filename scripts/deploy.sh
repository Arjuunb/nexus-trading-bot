#!/usr/bin/env sh
set -eu
[ -f .env ] || { echo 'Missing .env. Copy .env.example and set unique secrets.' >&2; exit 1; }

# Compose's env_file is allowed to contain blank deployment metadata. Export
# authoritative values from the checked-out revision so those blanks cannot
# hide the version that is actually being deployed.
GIT_COMMIT="${GIT_COMMIT:-$(git rev-parse HEAD 2>/dev/null || printf unknown)}"
GIT_BRANCH="${GIT_BRANCH:-$(git branch --show-current 2>/dev/null || printf unknown)}"
DEPLOYED_AT="${DEPLOYED_AT:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
export GIT_COMMIT GIT_BRANCH DEPLOYED_AT

# Say what is about to be built, and refuse a checkout that is not what the
# operator thinks it is. A failed "git pull" before this script does not stop
# it: the pull exits non-zero, the shell moves to the next command, and this
# builds whatever is still checked out. That has already shipped a branch
# predating the fixes it was meant to deploy, from a session whose pull had
# aborted with "Not possible to fast-forward". Aborting here costs one command;
# not aborting costs a full rebuild and a wrong diagnosis afterwards.
printf 'Deploying %s at %s\n' "${GIT_BRANCH}" "$(printf '%s' "${GIT_COMMIT}" | cut -c1-7)"
if [ "${SKIP_REVISION_CHECK:-0}" != "1" ] && git rev-parse --git-dir >/dev/null 2>&1; then
    if ! git diff --quiet HEAD 2>/dev/null; then
        echo 'Refusing to deploy: the working tree has uncommitted changes.' >&2
        echo 'Commit or stash them, or re-run with SKIP_REVISION_CHECK=1.' >&2
        exit 1
    fi
    upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || printf '')"
    if [ -n "$upstream" ]; then
        git fetch --quiet "${upstream%%/*}" "${upstream#*/}" 2>/dev/null || true
        behind="$(git rev-list --count "HEAD..${upstream}" 2>/dev/null || printf 0)"
        if [ "${behind:-0}" -gt 0 ]; then
            echo "Refusing to deploy: this branch is ${behind} commit(s) behind ${upstream}." >&2
            echo 'Pull first, or re-run with SKIP_REVISION_CHECK=1 to deploy it anyway.' >&2
            exit 1
        fi
    fi
fi

docker compose config --quiet
docker compose up -d --build --remove-orphans --wait --wait-timeout 180
docker compose ps
./scripts/healthcheck.sh
