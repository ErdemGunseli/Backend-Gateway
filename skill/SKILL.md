---
name: gateway-swap
description: Move a backend project between the shared gateway instance (SQLite) and its own standalone Render service (Postgres). Use for onboarding a project into the gateway, graduating one out to its own service, or migrating its data between the two.
---

# Gateway swap skill

Orchestrates the deterministic primitives in `tools/` to move a project in or out
of the gateway with minimal downtime. The primitives do the risky, stateful work;
this skill sequences them and makes the judgement calls.

## Preconditions (check first)

- **Both Render API keys must be in the environment** (decision #6):
  `RENDER_API_KEY_GATEWAY` and `RENDER_API_KEY_STANDALONE`. If either is missing,
  **warn the user and stop** - do not run a partial swap.
- The project must satisfy the hosting contract: a backend dir with a
  requirements file and an importable ASGI app that ships uvicorn, config read from
  the environment, no self-bound port, portable column types if it will use SQLite.
- The data pump must run with the **project's own venv** (it needs the project's
  models). In the gateway image that is `projects/<name>/.venv/bin/python`.

## Domain model (decision #1)

Projects always use `<name>.api.erdemgunseli.com` for their API, in or out of the
gateway. Swaps therefore **do not touch DNS** - only routing/process state changes.

## Database model (decision #2)

In gateway = **SQLite**; standalone = **Postgres**. Swaps **convert** data between
engines via `tools/db_pump.py` (model-driven, so JSON/UUID/timestamp conversion is
automatic). Preserve primary keys; the pump resyncs Postgres sequences on swap-out.

## Swap IN  (standalone Postgres → gateway SQLite)

1. `gateway add <name> --repo <url> [--backend-dir ...] [--app ...] [--db sqlite]`
2. Create the project's secret file locally (app secrets only; the gateway owns
   `DATABASE_URL` for SQLite) and `gateway secrets-push <name> --file <path>`.
3. `gateway deploy` and `gateway wait https://<name>.api.erdemgunseli.com/<health>`.
   The new SQLite DB is provisioned on first boot (schema via create_all; see
   "Migrations" below).
4. Copy data in: run `db_pump.py` with `--from <standalone Postgres URL>`
   `--to sqlite:////data/<name>.db` using the project venv. (For near-zero
   downtime: brief read-only window on the standalone, then final pump.)
5. Verify the gateway endpoint serves real data.
6. `gateway suspend <standalone-service> --account standalone`.

## Swap OUT  (gateway SQLite → standalone Postgres)

1. `gateway resume <standalone-service> --account standalone` (or create it).
2. Pump data out: `db_pump.py --from sqlite:////data/<name>.db --to <Postgres URL>`
   with the project venv. The pump resyncs Postgres sequences automatically.
3. Verify the standalone endpoint; confirm DNS/Caddy now point traffic there.
4. `gateway remove <name>` and `gateway deploy` to drop it from the gateway.

## Migrations on SQLite (decision #9)

A new SQLite project is provisioned with `create_all` + `alembic stamp head` - it
does **not** replay the Postgres-only historical migrations. From then on
`alembic upgrade head` applies only **new** revisions, which work on SQLite as long
as they follow the portable-migration rule (Alembic ops + `sa.func.now()`) and use
`op.batch_alter_table` for column alters/drops (SQLite can't ALTER in place).

## Notes

- Every gateway deploy restarts all projects (~seconds). A project that deploys
  often is a signal it should graduate out to its own service.
- Resource limits are soft (worker count); there is no watchdog (decision #5).
