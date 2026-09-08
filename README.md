# Gateway

Host many independent FastAPI backends on **one always-on Render Starter
instance** - to save cost and eliminate free-tier cold starts for low-traffic /
showcase projects. Serious projects graduate to their own instance.

## How it works

```
            Render Web Service (Starter, always-on)
   Internet ─► Caddy  (binds $PORT, the only public port; routes by Host)
                 ├─► 127.0.0.1:8001  uvicorn  project-a  (own venv, own .env)
                 ├─► 127.0.0.1:8002  uvicorn  project-b
                 └─► ...
              supervisord supervises Caddy + one process per project
              secrets: /etc/secrets/<name>.env  (Render Secret Files)
              data:    the project's own Postgres, or /data/<name>.db (SQLite)
```

- **One process per project** (own interpreter + venv). Forced by the factory
  backends sharing top-level package names; also gives secret isolation,
  independent dependencies, and load isolation.
- **Caddy** routes by Host (`api.<name>.erdemgunseli.com` plus any `extra_hosts`),
  and by path prefix (`/<name>/…`, prefix stripped) on any other host - so the
  service's own onrender.com address reaches every project before DNS exists, and
  the previous gateway's `/in-sight`, `/seo-rise` URLs keep working. Render
  terminates TLS at its edge, so Caddy is plain HTTP. Gateway health
  (`/__gateway/health`) passes whenever Caddy is up - one bad project never
  restarts the container.
- **supervisord** runs `launch.sh <name>` per project with `autorestart`, giving
  startup isolation.
- **`launch.sh`** injects only that project's secret file into its environment
  (works for any app - pydantic-settings, `os.getenv`, or `load_dotenv`), derives
  the SQLite `DATABASE_URL` for sqlite projects, picks the runner (uvicorn for a
  single worker, gunicorn+UvicornWorker for several), and prefixes its logs.
- **Manifest-driven**: `gateway.toml` is the single source of truth;
  `generate_config.py` renders the Caddyfile, supervisord config, and per-project
  runspecs. Onboarding touches only the manifest, a submodule, and a secret file.

## Layout

```
gateway.toml              manifest (single source of truth)
Dockerfile                caddy + supervisord + per-project venvs
entrypoint.sh             regenerate config -> exec supervisord
render.yaml               Render blueprint (disk, health check)
scripts/
  manifest.py             manifest loader + project resolution
  generate_config.py      manifest -> Caddyfile, supervisord.conf, run.d/<name>.env
  build_venvs.py          one venv per project (build time)
  launch.sh               per-project launcher (secret injection, runner, prefix logs)
  test_generate_config.py unit tests for the loader + generator
tools/
  render_api.py           minimal Render REST client
  db_pump.py              model-driven cross-engine data pump (PG <-> SQLite)
  gateway_cli.py          add/remove, up, status, domains, secrets-push, env-unset,
                          provision-sqlite, suspend/resume, deploy, wait, migrate
skill/                    agent skills: swap (SKILL.md), deploy.md, migrate.md
projects/<name>/          git submodule per project (+ its .venv, built in image)
secrets.example/          example secret file (real ones are Render Secret Files)
```

## Onboard a project

1. `git submodule add -b main <repo> projects/<name>` (or `gateway add <name> --repo ...`).
2. Add a `[[project]]` block to `gateway.toml` (name, subdomain, backend_dir, app,
   db, optional extra_hosts / path_prefixes). The factory layout is **not**
   assumed - these are per-project; every key is documented at the top of the file.
3. Create its Render Secret File: `gateway secrets-push <name> --file <path>`.
4. `gateway deploy --wait`. `generate_config.py` re-runs at boot and the new
   process joins.

## Swap a project in/out

See `skill/SKILL.md`. In gateway = SQLite, standalone = Postgres; swaps convert
data with `tools/db_pump.py`. Both Render API keys must be present or the tooling
warns and stops.

## Resolved design decisions

Domain: `api.<name>.erdemgunseli.com`, one DNS-only CNAME per project on Cloudflare
(swap never touches DNS), with path-prefix routing on the service's own host until a
project's DNS exists. DB: SQLite in
gateway / Postgres standalone - the three current projects keep their existing
Postgres (`db = "external"`). Env injection: universal via supervisord +
`launch.sh`. Entrypoint: fully per-project (no factory assumption). Resources:
soft (worker count) only. Health: Caddy-up. Backups: out of scope. Full table and
the reasoning: `CLAUDE.md` §5.

## Status

Live since 2026-09-06 on the Render service "Gateway Backend"
(`egunseli4@gmail.com`, frankfurt, starter), hosting Heard, In-Sight and SEO Rise
against their production databases. Reachable today by path on
`https://backend-gateway-zyu0.onrender.com/{heard,insight,seorise}/…` (legacy
`/in-sight`, `/seo-rise` too) and, since the erdemgunseli.com zone went active on
Cloudflare the same day, at `https://api.{heard,insight,seorise}.erdemgunseli.com`. `CLAUDE.md` §7–8 carry the verified state and what is left.
