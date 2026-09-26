#!/bin/sh
# Re-render the proxy's config from the mounted nginx/templates, validate it,
# and reload without dropping connections. Run inside the nginx container by
# scripts/deploy.sh (piped on stdin, so nothing new has to be mounted).
#
# nginx copies the templates into conf.d only when its container starts, or
# when a certificate renews. The image is stock and deploy.sh never rebuilds
# it, so template changes used to sit in the repo, unapplied, until nginx
# happened to restart. If the new config fails `nginx -t` the previous one is
# put back and nothing is reloaded, so a bad template cannot take the site down.
set -eu
conf=${NGINX_CONF_DIR:-/etc/nginx/conf.d}
render=${NGINX_RENDER:-/docker-entrypoint.d/10-render-config.sh}
nginx_bin=${NGINX_BIN:-nginx}

backup=$(mktemp -d)
log=$(mktemp)
cp -a "$conf"/. "$backup"/

if sh "$render" && "$nginx_bin" -t >"$log" 2>&1; then
    "$nginx_bin" -s reload
    echo "Nginx config: re-rendered from nginx/templates and reloaded"
else
    cat "$log" >&2
    rm -f "$conf"/*
    cp -a "$backup"/. "$conf"/
    echo "Nginx config: the new templates failed validation; the previous config is still in use" >&2
    exit 1
fi
