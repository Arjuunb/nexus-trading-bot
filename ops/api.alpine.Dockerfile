# Separately authorized LOCAL-ONLY API evaluation. Debian remains the default.
# Backend + bundled React apps for the Automation Hub.
# Single image: FastAPI + autonomous engine + two SPAs on one origin —
#   • the public landing / auth / settings site (tradexa-landing) at  "/"
#   • the session-gated trading dashboard (automation-hub-dashboard) at "/app"
# Build context is the repo ROOT.

# Empty default; an operator can supply retained public package artifacts as a
# named BuildKit context. It never supplies runtime credentials or app state.
FROM scratch AS build_inputs
COPY ops/build-inputs.md /NOTICE

# Pinned, previously verified Node 20 base. Read-only BuildKit mounts keep
# temporary npm trees out of intermediate layers, including on VFS storage.
FROM node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS frontend
RUN --mount=type=bind,target=/source \
    --mount=type=secret,id=build_ca \
    --mount=from=build_inputs,target=/build-inputs \
    set -eu; \
    if [ -f /run/secrets/build_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/build_ca; fi; \
    node --version; npm --version; \
    mkdir -p /tmp/frontend /out/dashboard /out/landing; \
    cp -a /source/automation-hub-dashboard /tmp/frontend/dashboard; \
    cp -a /source/tradexa-landing /tmp/frontend/landing; \
    if [ -d /build-inputs/npm-cache ]; then cp -a /build-inputs/npm-cache /tmp/npm-cache; fi; \
    for project in dashboard landing; do \
      cd /tmp/frontend/$project; \
      if [ -d /tmp/npm-cache ]; then npm ci --offline --cache /tmp/npm-cache; else npm ci; fi; \
      if [ "$project" = dashboard ]; then DASHBOARD_BASE=/app/ npm run build; else VITE_APP_URL=/app npm run build; fi; \
      cp -a dist/. /out/$project/; \
      cd /; rm -rf /tmp/frontend/$project; \
    done; \
    rm -rf /tmp/frontend /tmp/npm-cache /root/.npm

# --- Stage 3: Python backend (serves both builds) ---
FROM python:3.11-alpine3.24@sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929
ARG GIT_COMMIT=""
ARG GIT_BRANCH=""
ARG DEPLOYED_AT=""
ARG NEXUS_SOURCE_SHA256=""
ENV GIT_COMMIT=${GIT_COMMIT}
ENV GIT_BRANCH=${GIT_BRANCH}
ENV DEPLOYED_AT=${DEPLOYED_AT}
LABEL org.opencontainers.image.revision=${GIT_COMMIT} nexus.source.sha256=${NEXUS_SOURCE_SHA256}
WORKDIR /app/automation-hub
# Install the stdlib trading engine (root package) + the hub web dependencies.
RUN --mount=type=bind,target=/source \
    --mount=type=secret,id=build_ca \
    --mount=from=build_inputs,target=/build-inputs \
    --mount=from=frontend,source=/out,target=/frontend \
    set -eu; \
    addgroup -S -g 101 tradexa; adduser -S -D -H -u 100 -G tradexa -h /nonexistent tradexa; \
    cp -a /source/. /app/; cd /app; \
    if [ -f /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    if [ -d /build-inputs/python ]; then export PIP_NO_INDEX=1 PIP_FIND_LINKS=/build-inputs/python; fi; \
    pip install --no-cache-dir --require-hashes -r ops/api-requirements.lock; \
    pip install --no-cache-dir --no-deps --no-build-isolation -e ".[worker-postgres]"; \
    python -c "import psycopg; assert psycopg.pq.__impl__ == 'binary'"; \
    pip check; \
    mkdir -p /app/automation-hub/webui /app/automation-hub/landing /var/lib/tradexa; \
    cp -a /frontend/dashboard/. /app/automation-hub/webui/; \
    cp -a /frontend/landing/. /app/automation-hub/landing/; \
    chmod -R a+rX /app; \
    chown -R tradexa:tradexa /var/lib/tradexa /app

# Trading Instances are the authoritative paper runtime. Keep the retired
# deployment-wide engine fail-safe OFF unless an operator explicitly opts in.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HUB_AUTO_ENGINE=0 HUB_ENABLE_EXTERNAL_LIVE=0 HUB_USE_LIVE_DATA=0 \
    HUB_RESEARCH_AUTOSTART=0 HUB_MONITOR_AGENT=0 NEXUS_SHADOW_SUPERVISED=0
ENV HUB_DATA_DIR=/var/lib/tradexa
EXPOSE 8000
USER tradexa

# Honour an operator-provided port while remaining self-contained on a VPS.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips=*"]
