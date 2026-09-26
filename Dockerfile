# Backend + bundled React apps for the Automation Hub.
# Single image: FastAPI + autonomous engine + two SPAs on one origin —
#   • the public landing / auth / settings site (tradexa-landing) at  "/"
#   • the session-gated trading dashboard (automation-hub-dashboard) at "/app"
# Build context is the repo ROOT.

# --- Stage 1: build the trading dashboard (served under /app) ---
FROM node:20-slim AS ui
WORKDIR /ui
COPY automation-hub-dashboard/package*.json ./
RUN npm ci
COPY automation-hub-dashboard/ ./
# base "/app/" so the dashboard's assets resolve while the landing owns "/assets"
ENV DASHBOARD_BASE=/app/
RUN npm run build          # -> /ui/dist

# --- Stage 2: build the landing / auth / settings site (served at /) ---
FROM node:20-slim AS landing
WORKDIR /landing
COPY tradexa-landing/package*.json ./
RUN npm ci
COPY tradexa-landing/ ./
# "Launch Bot" and post-login point at the dashboard on the same origin
ENV VITE_APP_URL=/app
RUN npm run build          # -> /landing/dist

# --- Stage 3: Python backend (serves both builds) ---
FROM python:3.11-slim
ARG GIT_COMMIT=""
ARG GIT_BRANCH=""
ARG DEPLOYED_AT=""
ENV GIT_COMMIT=${GIT_COMMIT}
ENV GIT_BRANCH=${GIT_BRANCH}
ENV DEPLOYED_AT=${DEPLOYED_AT}
WORKDIR /app
RUN addgroup --system tradexa && adduser --system --ingroup tradexa --home /nonexistent tradexa
COPY --chown=tradexa:tradexa . .

# Install the stdlib trading engine (root package) + the hub web dependencies.
RUN pip install --no-cache-dir -e . \
 && pip install --no-cache-dir -r automation-hub/requirements.txt

# Bundle both React builds so the backend serves them on one origin.
COPY --from=ui /ui/dist /app/automation-hub/webui
COPY --from=landing /landing/dist /app/automation-hub/landing

RUN mkdir -p /var/lib/tradexa && chown -R tradexa:tradexa /var/lib/tradexa /app

# Trading Instances are the authoritative paper runtime. Keep the retired
# deployment-wide engine fail-safe OFF unless an operator explicitly opts in.
ENV HUB_AUTO_ENGINE=0
ENV HUB_DATA_DIR=/var/lib/tradexa
WORKDIR /app/automation-hub
EXPOSE 8000
USER tradexa

# Honour an operator-provided port while remaining self-contained on a VPS.
# "exec" matters: without it sh stays PID 1 with uvicorn as its child, and sh
# as PID 1 ignores the SIGTERM "docker stop" sends. Every deploy then ended in
# SIGKILL after the grace period, so the shutdown hook never ran -- no entry
# gate checkpoint, and every worker lease left held for the next container.
# The graceful-shutdown timeout ends open dashboard streams (SSE) so that hook
# is reached well inside compose.yaml's stop_grace_period.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips=* --timeout-graceful-shutdown 5"]
