#!/bin/sh
set -e

if [ -z "$DOCQA_PASSWORD" ]; then
  echo "ERROR: APP_PASSWORD environment variable is not set." >&2
  exit 1
fi

DOCQA_USER="${DOCQA_USER:-admin}"
printf '%s:%s\n' "$DOCQA_USER" "$(openssl passwd -apr1 "$DOCQA_PASSWORD")" > /etc/nginx/.htpasswd

exec nginx -g 'daemon off;'
