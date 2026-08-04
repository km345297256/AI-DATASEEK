#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
image_registry="${IMAGE_REGISTRY:-}"
image_namespace="${IMAGE_NAMESPACE:-}"
image_tag="${IMAGE_TAG:-latest}"
platforms="${PLATFORMS:-linux/amd64}"
push=false
dry_run=false

usage() {
  cat <<'EOF'
Build the AI-DataSeek frontend, backend, and sandbox images.

Usage: ./build.sh [--registry HOST] [--namespace NAME] [--tag TAG]
                  [--platforms LIST] [--push] [--dry-run]

Without --push, images are loaded into the local Docker daemon and only one
platform may be selected. With --push, IMAGE_REGISTRY is required.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --registry) image_registry="$2"; shift 2 ;;
    --namespace) image_namespace="$2"; shift 2 ;;
    --tag) image_tag="$2"; shift 2 ;;
    --platforms) platforms="$2"; shift 2 ;;
    --push) push=true; shift ;;
    --dry-run) dry_run=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$push" == true && -z "$image_registry" ]]; then
  echo "--push requires --registry or IMAGE_REGISTRY" >&2
  exit 2
fi
if [[ "$push" == false && "$platforms" == *,* ]]; then
  echo "Local --load builds support one platform; use --push for multiple platforms" >&2
  exit 2
fi

prefix="${image_namespace#/}"
prefix="${prefix%/}"
if [[ -n "$image_registry" ]]; then
  prefix="${image_registry%/}${prefix:+/${prefix}}"
fi

run() {
  printf ' +'
  printf ' %q' "$@"
  printf '\n'
  if [[ "$dry_run" == false ]]; then
    "$@"
  fi
}

output_flag=--load
if [[ "$push" == true ]]; then
  output_flag=--push
fi

for component in frontend backend sandbox; do
  image_name="ai-dataseek-${component}"
  if [[ -n "$prefix" ]]; then
    image_name="${prefix}/${image_name}"
  fi
  run docker buildx build \
    --platform "$platforms" \
    --provenance=false \
    --tag "${image_name}:${image_tag}" \
    "$output_flag" \
    "$root_dir/$component"
done

echo "AI-DataSeek images completed with tag ${image_tag}."
