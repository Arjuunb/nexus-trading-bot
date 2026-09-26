#!/bin/sh
# The certificate volume is read-only. Compare its mtime and render TLS only
# after a valid certificate is present; this keeps the HTTP ACME bootstrap safe.
last=""
last_api=""
while :; do
  cert=/etc/letsencrypt/live/trade-logx.com/fullchain.pem
  current="$(stat -c %Y "$cert" 2>/dev/null || true)"
  if [ -n "$current" ] && [ "$current" != "$last" ]; then
    cp /etc/nginx/templates/https.conf /etc/nginx/conf.d/default.conf
    nginx -s reload || true
    last="$current"
  fi
  # api.trade-logx.com: switched on (and renewed) the same way, independently.
  api_cert=/etc/letsencrypt/live/api.trade-logx.com/fullchain.pem
  api_current="$(stat -c %Y "$api_cert" 2>/dev/null || true)"
  if [ -n "$api_current" ] && [ "$api_current" != "$last_api" ]; then
    cp /etc/nginx/templates/api.conf /etc/nginx/conf.d/api.conf
    nginx -s reload || true
    last_api="$api_current"
  fi
  sleep 60 & wait $!
done &
