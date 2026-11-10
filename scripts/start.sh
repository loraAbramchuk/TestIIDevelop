#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
command -v python3 >/dev/null || { echo 'Python 3 is required' >&2; exit 1; }
docker compose version >/dev/null
python3 scripts/init_env.py
compose=(docker compose -f compose.yaml)
if [[ -n "${CODEX_PROXY_CERT:-}${BUILD_CA_BUNDLE:-}" ]]; then
    compose+=(-f compose.cloud.yaml)
fi
"${compose[@]}" up --build --detach --wait --wait-timeout 180
echo 'Services are ready. See README.md for local access and development commands.'
