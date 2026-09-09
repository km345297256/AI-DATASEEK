#!/bin/sh
set -eu
case "${ONLYOFFICE_GATEWAY_SECRET:-}" in
  ''|*[!a-f0-9]*) echo 'Office gateway needs a generated secret' >&2; exit 1;;
esac
[ "${#ONLYOFFICE_GATEWAY_SECRET}" -eq 64 ] || { echo 'Invalid Office gateway secret' >&2; exit 1; }
envsubst '${ONLYOFFICE_GATEWAY_SECRET}' < /etc/nginx/office.conf.template > /tmp/office-nginx.conf
nginx -t -c /tmp/office-nginx.conf
if [ "${1:-}" = "--check" ]; then exit 0; fi
exec nginx -c /tmp/office-nginx.conf -g 'daemon off;'
