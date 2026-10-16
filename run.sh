#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"

if [[ "${1:-}" == production ]]; then
  for entry_directory in "$root_dir" "$(pwd -P)"; do
    case "$entry_directory" in
      /root/AI-DataSeek|/root/AI-DataSeek/*|/root/ai-dataseek-releases|/root/ai-dataseek-releases/*|/root/ai-dataseek-current)
        echo "Legacy production entry is retired; publish from /data2/current using ./run.sh production" >&2
        exit 1
        ;;
    esac
  done
  shift
  exec python3 "$root_dir/deploy/production/deploy.py" "$@"
fi

for entry_directory in "$root_dir" "$(pwd -P)"; do
  case "$entry_directory" in
    /data2/releases|/data2/releases/*|/data2/current|/root/AI-DataSeek|/root/AI-DataSeek/*|/root/ai-dataseek-releases|/root/ai-dataseek-releases/*|/root/ai-dataseek-current)
      python3 "$root_dir/deploy/production/deploy.py" --check-compose-entry "$root_dir" "$@"
      break
      ;;
  esac
done

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

# Snap's launcher cannot keep a working directory on the /data2 disk. Use the
# same installed package's Compose client directly after the production guard.
for entry_directory in "$root_dir" "$(pwd -P)"; do
  case "$entry_directory" in
    /data2/releases|/data2/releases/*|/data2/current)
      if [[ "$(command -v docker)" == /snap/bin/docker && -x /snap/docker/current/usr/libexec/docker/cli-plugins/docker-compose ]]; then
        exec /snap/docker/current/usr/libexec/docker/cli-plugins/docker-compose -f docker-compose.yml "$@"
      fi
      ;;
  esac
done

if docker compose version >/dev/null 2>&1; then
  exec docker compose -f docker-compose.yml "$@"
fi
if command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose -f docker-compose.yml "$@"
fi
echo "Docker Compose is not installed" >&2
exit 1
