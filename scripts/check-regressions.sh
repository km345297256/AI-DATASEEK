#!/usr/bin/env bash
# Reusable local/CI gate. No deployment, commits, model calls or public ports.
set -Eeuo pipefail

task_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:---local}"
if [[ "$mode" != "--local" && "$mode" != "--containers" ]]; then
  echo "Usage: bash scripts/check-regressions.sh [--local|--containers]" >&2
  exit 2
fi

cd "$task_repo_root"
node scripts/sync-visualization-contract.mjs --check
cd "$task_repo_root/frontend"
npm run type-check
npm test
npm run build
cd "$task_repo_root/plugin-host"
npm test
cd "$task_repo_root"
./run.sh config --quiet

if [[ "$mode" == "--containers" ]]; then
  # Existing images only. Test dependencies live inside disposable containers;
  # repository mounts are read-only, and tests use their own fixtures.
  ./run.sh run --rm --no-deps --entrypoint sh \
    -v "$task_repo_root:/workspace:ro" -w /workspace/backend \
    -e TOOL_PLUGINS_DIR=/workspace/tools \
    -e PLUGIN_RUNTIME_TOOLS_DIR=/workspace/tools \
    -e PLUGIN_RUNTIME_EXECUTION_CONTRACT_DIR=/workspace/sandbox \
    backend -c 'uv pip install --python /opt/venv/bin/python --index-url https://pypi.org/simple pytest pytest-asyncio pytest-mock requests && uv run --no-sync pytest -o addopts= -o log_cli=false -p no:cacheprovider -q -ra'
  ./run.sh run --rm --no-deps --entrypoint sh \
    -v "$task_repo_root:/workspace:ro" -w /workspace/sandbox \
    -e AI_DATASEEK_REQUIRE_GEOSCIENCE_STACK=1 \
    sandbox-image -c 'uv pip install --python /app/.venv/bin/python --index-url https://pypi.org/simple pytest pytest-asyncio pytest-mock httpx requests && python -m pytest -o addopts= -o log_cli=false -p no:cacheprovider -q -ra'
else
  cd "$task_repo_root/backend"
  uv run python scripts/check_persistence_contracts.py --check
  uv run pytest
  cd "$task_repo_root/sandbox"
  AI_DATASEEK_REQUIRE_GEOSCIENCE_STACK=1 uv run pytest
fi
