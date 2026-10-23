# Separately authorized LOCAL worker-only evaluation. Debian remains the default.
# The reviewed official Python 3.12.15 / Alpine 3.24 index is immutable.
FROM python:3.12-alpine3.24@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app/automation-hub
RUN --mount=type=bind,target=/source \
    --mount=from=verified_wheels,target=/verified-wheels \
    set -eu; \
    cp /source/pyproject.toml /source/README.md /source/LICENSE /app/; \
    cp -a /source/bot /source/tradexa /app/; cd /app; \
    pip install --no-cache-dir --no-index --find-links=/verified-wheels --require-hashes -r /verified-wheels/requirements-verified.txt; \
    pip install --no-cache-dir --no-index --no-deps --no-build-isolation .; \
    python -c "import psycopg; assert psycopg.pq.__impl__ == 'binary'"; \
    pip check; \
    mkdir -p automation-hub/services automation-hub/data automation-hub/migrations/shadow automation-hub/migrations/workers ops; \
    cp /source/automation-hub/services/*.py automation-hub/services/; \
    cp /source/automation-hub/data/*.py /source/automation-hub/data/pr6_real_paper_freeze.json automation-hub/data/; \
    cp -a /source/automation-hub/strategies automation-hub/; \
    cp /source/automation-hub/migrations/shadow/*.sql automation-hub/migrations/shadow/; \
    cp /source/automation-hub/migrations/workers/*.sql automation-hub/migrations/workers/; \
    cp /source/ops/staging-initialize.py ops/; \
    chmod -R a+rX /app
ENV PYTHONPATH=/app \
    HUB_AUTO_ENGINE=0 HUB_ENABLE_EXTERNAL_LIVE=0 HUB_USE_LIVE_DATA=0 \
    HUB_RESEARCH_AUTOSTART=0 HUB_MONITOR_AGENT=0 NEXUS_SHADOW_SUPERVISED=0
USER 1000:1000
ENTRYPOINT ["python", "-m", "services.research_worker_main"]
