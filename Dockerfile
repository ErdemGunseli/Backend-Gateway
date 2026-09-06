# syntax=docker/dockerfile:1

# Caddy binary lifted from the official image (no apt repo dance).
FROM caddy:2 AS caddy

FROM python:3.11-slim

# supervisord (process manager) + bash (launch.sh) + curl (health probes).
RUN apt-get update \
    && apt-get install -y --no-install-recommends supervisor bash ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# The official image marks its binary with the cap_net_bind_service file capability.
# Render's unprivileged runtime refuses to exec a binary carrying file capabilities
# (supervisord logged "couldn't exec caddy: EPERM" on the first deploy, 2026-09-06),
# and Caddy binds an unprivileged port anyway - so re-copy it with plain `cp`, which
# drops the security.capability xattr and leaves an ordinary executable behind.
COPY --from=caddy /usr/bin/caddy /tmp/caddy-with-caps
RUN cp /tmp/caddy-with-caps /usr/bin/caddy && chmod 0755 /usr/bin/caddy && rm /tmp/caddy-with-caps

ENV GATEWAY_APP_ROOT=/app \
    GATEWAY_GEN_DIR=/app/generated \
    PYTHONUNBUFFERED=1
WORKDIR /app

# Bring in the orchestrator (manifest, scripts, tooling) and the project code,
# which arrives as git submodules under projects/<name> (Render checks them out
# before the build). Frontends and other non-backend trees are excluded by
# .dockerignore so the image only carries what the processes need.
COPY . /app

RUN chmod +x scripts/launch.sh entrypoint.sh

# One venv per project (reads the manifest). Kept as its own layer so it only
# re-runs when project requirements change.
RUN python scripts/build_venvs.py

# Pre-generate config (also regenerated at boot by the entrypoint).
RUN python scripts/generate_config.py

# Only Caddy is public; it binds Render's injected $PORT (default 10000).
EXPOSE 10000
ENTRYPOINT ["/app/entrypoint.sh"]
