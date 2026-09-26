#!/bin/sh
set -eu
domain=trade-logx.com
if [ -s "/etc/letsencrypt/live/$domain/fullchain.pem" ] && [ -s "/etc/letsencrypt/live/$domain/privkey.pem" ]; then
  cp /etc/nginx/templates/https.conf /etc/nginx/conf.d/default.conf
else
  cp /etc/nginx/templates/http.conf /etc/nginx/conf.d/default.conf
fi
# The API name has its own certificate. Serve it only once that exists.
api=api.trade-logx.com
if [ -s "/etc/letsencrypt/live/$api/fullchain.pem" ] && [ -s "/etc/letsencrypt/live/$api/privkey.pem" ]; then
  cp /etc/nginx/templates/api.conf /etc/nginx/conf.d/api.conf
else
  rm -f /etc/nginx/conf.d/api.conf
fi
