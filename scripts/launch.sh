#!/usr/bin/env bash
# Per-project launcher. supervisord runs `launch.sh <name>` for each project.
#
# Responsibilities (deliberately un-opinionated about the app):
#   1. Load the project's non-secret runspec (paths, port, db mode).
#   2. Inject ONLY this project's secret file into the environment. Works for any
#      app - pydantic-settings, os.getenv, or load_dotenv all see their config.
#   3. For sqlite-backed projects the gateway owns DATABASE_URL (a file on the
#      persistent disk); external projects get DATABASE_URL from their secret file.
#   4. Exec the app, prefixing every log line with the project name and forwarding
#      termination signals for a clean shutdown.
set -euo pipefail

NAME="${1:?usage: launch.sh <project-name>}"
APP_ROOT="${GATEWAY_APP_ROOT:-/app}"
GEN_DIR="${GATEWAY_GEN_DIR:-$APP_ROOT/generated}"
SECRETS_DIR="${SECRETS_DIR:-/etc/secrets}"

# 1. Runspec (generated from the manifest; non-secret).
# shellcheck disable=SC1090
source "$GEN_DIR/run.d/$NAME.env"

# 2. This project's secrets only -> environment.
SECRET_FILE="$SECRETS_DIR/$NAME.env"
if [[ -f "$SECRET_FILE" ]]; then
	set -a
	# shellcheck disable=SC1090
	source "$SECRET_FILE"
	set +a
else
	echo "[$NAME] WARNING: secret file $SECRET_FILE not found; starting with runspec env only" >&2
fi

# 3. Gateway owns the DB URL for sqlite projects (overrides any stale value).
if [[ "${PROJECT_DB:-}" == "sqlite" ]]; then
	mkdir -p "$(dirname "$PROJECT_DB_PATH")"
	export DATABASE_URL="sqlite:///${PROJECT_DB_PATH}"
fi

cd "$PROJECT_DIR"

# 3b. Seed from the Postgres this project used to run on. The seeder is the guard,
#     not this condition: it fills the target only when the target holds no rows, and
#     never contacts the source otherwise - so this is a no-op on every boot after the
#     conversion, including once that Postgres is suspended. Keying on the database
#     FILE instead was wrong: a seed that copied nothing still left a file behind,
#     which then looked seeded forever (2026-09-08).
#     A failed seed must NOT start the app - serving an empty database would look like
#     every account had been deleted - so the launcher exits and lets supervisord retry.
if [[ "${PROJECT_DB:-}" == "sqlite" && -n "${SEED_FROM_DATABASE_URL:-}" ]]; then
	if ! SEED_APP="$PROJECT_APP" "$PROJECT_VENV/bin/python" "$APP_ROOT/tools/pg_to_sqlite.py"; then
		echo "[$NAME] ERROR: seed failed; refusing to start on an unproven database" >&2
		exit 1
	fi
fi

# 4. Build the start command. A manifest `start_cmd` overrides everything (it is
#    word-split by the shell, so it may reference $PROJECT_PORT, $PROJECT_VENV,
#    $PROJECT_APP and $PROJECT_WORKERS). Otherwise:
#      workers = 1  -> uvicorn directly. supervisord already restarts a crashed
#                      process, so a gunicorn master would only cost ~20 MB RSS per
#                      project on a 512 MB instance for nothing.
#      workers > 1  -> gunicorn + UvicornWorker when the project ships gunicorn,
#                      else uvicorn's own --workers.
#    The hosting contract asks for an importable ASGI app, not a particular server;
#    every ASGI project ships uvicorn. Both servers honour Render's
#    FORWARDED_ALLOW_IPS so the X-Forwarded-* headers Caddy passes through are
#    trusted (measured: apps see scheme=https and the real client IP).
if [[ -n "${PROJECT_START_CMD:-}" ]]; then
	eval "CMD=( $PROJECT_START_CMD )"
elif [[ "${PROJECT_WORKERS:-1}" -gt 1 && -x "$PROJECT_VENV/bin/gunicorn" ]]; then
	CMD=(
		"$PROJECT_VENV/bin/gunicorn"
		-w "$PROJECT_WORKERS"
		-k uvicorn.workers.UvicornWorker
		"$PROJECT_APP"
		-b "127.0.0.1:$PROJECT_PORT"
	)
elif [[ -x "$PROJECT_VENV/bin/uvicorn" ]]; then
	CMD=(
		"$PROJECT_VENV/bin/uvicorn"
		"$PROJECT_APP"
		--host 127.0.0.1
		--port "$PROJECT_PORT"
		--workers "$PROJECT_WORKERS"
		--proxy-headers
	)
else
	echo "[$NAME] ERROR: no uvicorn (or gunicorn) in $PROJECT_VENV; add one to the project's requirements or set start_cmd in the manifest" >&2
	exit 1
fi

# 5. Run with a per-project log prefix; forward SIGTERM/SIGINT to the app so
#    supervisord stop / container shutdown terminates it gracefully.
"${CMD[@]}" > >(while IFS= read -r line; do printf '[%s] %s\n' "$NAME" "$line"; done) 2>&1 &
child=$!
forward() { kill -TERM "$child" 2>/dev/null || true; }
trap forward TERM INT
wait "$child"
