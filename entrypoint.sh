#!/usr/bin/env bash
# Container entrypoint: regenerate config from the manifest (idempotent), then
# hand off to supervisord which runs Caddy + every project process.
set -euo pipefail

APP_ROOT="${GATEWAY_APP_ROOT:-/app}"
GEN_DIR="${GATEWAY_GEN_DIR:-$APP_ROOT/generated}"

python "$APP_ROOT/scripts/generate_config.py"

exec supervisord -c "$GEN_DIR/supervisord.conf" -n
