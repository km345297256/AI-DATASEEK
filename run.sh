#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"

# Generated locally, never sourced as shell code and never printed. The normal
# stack remains usable when the optional Office profile has not been prepared.
office_secrets_file="$root_dir/.local/office-viewer.env"
if [[ -f "$office_secrets_file" ]]; then
  while IFS='=' read -r office_key office_value; do
    [[ "$office_key" == ONLYOFFICE_JWT_SECRET || "$office_key" == ONLYOFFICE_GATEWAY_SECRET ]] || { echo "Invalid Office secret file" >&2; exit 1; }
    [[ "$office_value" =~ ^[a-f0-9]{64}$ ]] || { echo "Invalid Office secret format" >&2; exit 1; }
    if [[ -z "${!office_key:-}" ]]; then export "$office_key=$office_value"; fi
  done < "$office_secrets_file"
fi

if docker compose version >/dev/null 2>&1; then
  exec docker compose -f docker-compose.yml "$@"
fi
if command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose -f docker-compose.yml "$@"
fi
echo "Docker Compose is not installed" >&2
exit 1
