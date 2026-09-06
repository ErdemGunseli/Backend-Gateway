#!/usr/bin/env bash
# render.sh — run render_ops.py with account keys injected from an EXTERNAL file.
#
# Why this wrapper exists: the API keys must never live inside the skill (skills get
# packaged and shared) and never be printed into the agent's transcript. This script
# sources them from a git-ignored file OUTSIDE the skill, exports them into the
# subprocess environment, and execs the CLI — so commands can use the keys without
# the key values ever appearing in context.
#
# Set up once:
#   mkdir -p ~/.config/render && chmod 700 ~/.config/render
#   printf 'RENDER_API_KEY_PERSONAL=rnd_xxx\nRENDER_API_KEY_CLIENTB=rnd_yyy\n' \
#       > ~/.config/render/accounts.env && chmod 600 ~/.config/render/accounts.env
#
# Override the path with RENDER_ACCOUNTS_ENV. Real env vars take precedence over the
# file, so CI can inject keys without it.
set -euo pipefail

ENV_FILE="${RENDER_ACCOUNTS_ENV:-$HOME/.config/render/accounts.env}"
if [[ -f "$ENV_FILE" ]]; then
	set -a
	# shellcheck disable=SC1090
	. "$ENV_FILE"
	set +a
fi

exec python3 "$(dirname "$0")/render_ops.py" "$@"
