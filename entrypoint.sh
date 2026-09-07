#!/usr/bin/env bash
# Container entrypoint: regenerate config from the manifest (idempotent), then
# hand off to supervisord which runs Caddy + every project process.
set -euo pipefail

APP_ROOT="${GATEWAY_APP_ROOT:-/app}"
GEN_DIR="${GATEWAY_GEN_DIR:-$APP_ROOT/generated}"

# A command passed to the container runs INSTEAD of the gateway. Without this a
# Render one-off job on this service silently starts supervisord and ignores what it
# was asked to run, which is what blocked the 2026-09-07 database work: nothing else
# in the account could reach both a project's Postgres and this container's disk.
if [ "$#" -gt 0 ]; then
	exec "$@"
fi

python "$APP_ROOT/scripts/generate_config.py"

exec supervisord -c "$GEN_DIR/supervisord.conf" -n
