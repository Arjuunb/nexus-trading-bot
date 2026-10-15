# Only the privilege-drop helper is replaced. PostgreSQL, entrypoint and all
# upstream packages/configuration remain inherited from this reviewed digest.
FROM postgres@sha256:1a66d744c1b459e13b05a8fca341da84cb63383e99ce262210efee5a319d4551
ARG CONTROLLER_COMMIT
ARG GOSU_SHA256
LABEL org.opencontainers.image.title="NEXUS staging PostgreSQL 16.15 with rebuilt gosu" \
      org.opencontainers.image.source="https://github.com/Arjuunb/nexus-trading-bot" \
      org.opencontainers.image.revision="${CONTROLLER_COMMIT}" \
      org.opencontainers.image.base.digest="sha256:1a66d744c1b459e13b05a8fca341da84cb63383e99ce262210efee5a319d4551" \
      nexus.scope="STAGING_SUPPORT_ONLY; NO_DEPLOYMENT_AUTHORIZATION" \
      nexus.gosu.source.commit="6456aaa0f3c854d199d0f037f068eb97515b7513" \
      nexus.gosu.go.version="1.27.2" \
      nexus.gosu.binary.sha256="${GOSU_SHA256}"
COPY --chmod=0755 gosu /usr/local/bin/gosu
