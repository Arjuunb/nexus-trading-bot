# Local observer image. Explicit code-only copies exclude .env, secrets, live
# databases, app/webhook entrypoints, financial execution package and UI assets.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app/automation-hub
# Named build context prepared over verified HTTPS by the bounded operator
# helper. Build requires no network and rechecks every dependency hash.
RUN --mount=type=bind,target=/source \
    --mount=from=verified_wheels,target=/verified-wheels \
    set -eu; \
    cp /source/pyproject.toml /source/README.md /source/LICENSE /app/; \
    cp -a /source/bot /source/tradexa /app/; cd /app; \
    pip install --no-cache-dir --no-index --find-links=/verified-wheels --require-hashes -r /verified-wheels/requirements-verified.txt; \
    pip install --no-cache-dir --no-index --no-deps --no-build-isolation .; \
    pip check; \
    mkdir -p automation-hub/services automation-hub/data automation-hub/migrations/shadow automation-hub/migrations/workers ops; \
    cp /source/automation-hub/services/*.py automation-hub/services/; \
    cp /source/automation-hub/data/*.py /source/automation-hub/data/pr6_real_paper_freeze.json automation-hub/data/; \
    cp -a /source/automation-hub/strategies automation-hub/; \
    cp /source/automation-hub/migrations/shadow/*.sql automation-hub/migrations/shadow/; \
    cp /source/automation-hub/migrations/workers/*.sql automation-hub/migrations/workers/; \
    cp /source/ops/staging-initialize.py ops/; \
    chmod -R a+rX /app
# Checkout files can be owner-only in cloud workspaces. Grant read/traversal
# within this code-only image, never writable application or financial state.
ENV PYTHONPATH=/app
USER 1000:1000
ENTRYPOINT ["python", "-m", "services.shadow_worker_main"]
