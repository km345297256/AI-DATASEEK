#!/usr/bin/env bash
set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
secret_dir="$root_dir/.local"
secret_file="$secret_dir/office-viewer.env"
umask 077
if [[ -e "$secret_file" ]]; then
  [[ -f "$secret_file" && ! -L "$secret_file" ]] || { echo "Office secret target is not a regular file" >&2; exit 1; }
  chmod 600 "$secret_file"
  echo "Office secrets already exist; preserved."
  exit 0
fi
mkdir -p "$secret_dir"
temporary="$(mktemp "$secret_dir/office-viewer.env.XXXXXXXX")"
trap '[[ ! -f "$temporary" ]] || rm -- "$temporary"' EXIT
jwt_secret="$(openssl rand -hex 32)"
gateway_secret="$(openssl rand -hex 32)"
printf 'ONLYOFFICE_JWT_SECRET=%s\nONLYOFFICE_GATEWAY_SECRET=%s\n' "$jwt_secret" "$gateway_secret" > "$temporary"
chmod 600 "$temporary"
# A concurrent setup cannot replace keys that have already been installed.
ln "$temporary" "$secret_file"
echo "Office secrets prepared locally (mode 600). No secret values were printed."
