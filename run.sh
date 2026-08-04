#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"

if docker compose version >/dev/null 2>&1; then
  exec docker compose -f docker-compose.yml "$@"
fi
if command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose -f docker-compose.yml "$@"
fi
echo "Docker Compose is not installed" >&2
exit 1
