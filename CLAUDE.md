# CLAUDE.md - Gateway

This file is the **single source of truth** for any agent working on the gateway.
It is self-contained: read only this and you have the full picture - what the
gateway is, why it exists, how it works, its history, what's done, and what to do
next. This repo (`ErdemGunseli/Backend-Gateway`) is the canonical home; the copy
that was staged inside QUANTSOC's `gateway/` folder is retired.

---

## 1. What this is

The gateway hosts **many independent FastAPI backends on one always-on Render
Starter instance** (~$7/mo, 512 MB, ~0.5 CPU). Goal: cheaply host low-traffic /
showcase / personal projects with **no free-tier cold starts**, each with isolated
secrets and isolated data. Serious or high-traffic projects "graduate" to their own
Render service + their own Postgres.

One Render web service runs a Docker container in which **Caddy** reverse-proxies,
by hostname (and, until the custom domains resolve, by path prefix), to **one
isolated uvicorn process per project**, each with its own virtualenv, its own `.env`
(a Render Secret File), and its own database. **supervisord** supervises Caddy + all
project processes. A declarative manifest (`gateway.toml`) drives everything.

```
            Render Web Service "Gateway Backend" (Starter, always-on, frankfurt)
   Internet ─► Caddy  (binds $PORT - the only public port; routes by Host, else by path)
                 ├─► 127.0.0.1:8001  uvicorn  heard    api.heard.erdemgunseli.com    (own venv, own .env, Heard DB)
                 ├─► 127.0.0.1:8002  uvicorn  insight  api.insight.erdemgunseli.com  (own venv, own .env, Gateway DB schema in_sight)
                 └─► 127.0.0.1:8003  uvicorn  seorise  api.seorise.erdemgunseli.com  (own venv, own .env, Gateway DB schema seo_rise)
              supervisord: supervises Caddy + one process per project
              secrets: /etc/secrets/<name>.env   (Render Secret Files, one per project)
              data:    the project's own Postgres (db = "external"), or /data/<name>.db (db = "sqlite")
```

Hosted today: **Heard**, **In-Sight**, **SEO Rise** (§7).

---

## 2. Why it's built this way (the load-bearing constraint)

Factory-derived backends all root at the **same top-level package names**
(`platform`, `product`, `infrastructure`) and even manipulate `sys.modules`. Two of
them **cannot coexist in one Python interpreter** - they clobber each other. So
in-process sub-app mounting (what the previous attempt did) is a dead end. Hence
**one OS process per project**, which also gives - for free - secret isolation,
independent dependency sets, and load isolation (one project's blocking work can't
stall another's event loop).

Render terminates TLS at its edge and forwards plain HTTP to the container's single
`$PORT`, so **Caddy does no TLS** - it's a pure HTTP Host-router.

---

## 3. History & context (how we got here)

- **Previous attempt** (this repo before 2026-09-06, "lackluster"): single
  process, in-process `app.mount()`, path routing (`/heard`, `/in-sight`,
  `/seo-rise`), one shared `TENANT_CONFIG` env blob, one shared venv, one shared
  Postgres ("Gateway DB") with a schema per project. It failed secret isolation,
  composition-agnosticism, load isolation, and the module-name collision, and could
  not host a factory backend. We kept its good ideas - **git submodules**,
  **schema/DB-per-project**, and the **old path URLs as aliases** - and fixed the
  rest with process-per-project.

- **Designed and validated inside the QUANTSOC repo** (this repo wasn't reachable
  from that session), then lifted here on 2026-09-06 with the §9 cleanups applied.
  QUANTSOC itself was made gateway-ready first, and is the template every hosted
  project should follow:
  1. **Dependency trim** - a `pip freeze` carrying numpy/pandas/scipy/boto3/grpc
     that nothing imported cost ~400–550 MB RSS per worker; trimmed, ~120–160 MB.
     **RAM is the binding constraint, not CPU.**
  2. **Dialect-aware migration runner** - the PostgreSQL advisory lock is only taken
     on Postgres; other backends run migrations directly.
  3. **Event-loop fix** - handlers doing sync DB work are plain `def` so Starlette
     threadpools them.
  4. **Portable column types** - `JSONColumn` (JSONB on Postgres, JSON elsewhere)
     and `Uuid`, so the schema builds on SQLite too.
  Heard, In-Sight and SEO Rise each received the dependency trim; Heard also got
  the portable types + dialect-aware runner ("Make backend portable to SQLite for
  gateway hosting"). In-Sight and SEO Rise use plain SQLAlchemy types already.

- **Vault sync is not here, by design.** An Obsidian-Sync-to-git bridge once lived
  in the gateway tree (`gateway/vault-bridge/`, git-lfs, obsidian-headless). It was
  retired when the standalone **VaultSync** repo (`ErdemGunseli/VaultSync`, a
  Render background worker on its own account) took over; the two shared only "an
  always-on box", and co-locating vault media commits with the API tier put both
  under one memory limit. The gateway carries **none** of that logic - no git-lfs,
  no obsidian-headless, no vault manifest keys - and must not grow it back.

- **All blocking design decisions were resolved** with the owner - see §5.

---

## 4. How it works (flows)

- **Boot:** entrypoint runs `generate_config.py` (manifest → Caddyfile +
  supervisord config + per-project runspecs) → `supervisord` starts Caddy + each
  project via `launch.sh <name>` → each process loads its `.env`, runs its own
  startup (Heard: `create_and_migrate`; In-Sight/SEO Rise: `create_all`), serves on
  its internal port → Caddy routes.
- **Routing, two layers, both generated from the manifest:**
  1. **Host routing** (the design): each project owns the hostname the manifest's
     `host_template` renders - `api.<name>.erdemgunseli.com` - plus any
     `extra_hosts` (a product's own API domain, e.g. `api.heard.cc`).
  2. **Path-prefix routing** on any other Host: `/<name>/…` (plus legacy aliases
     such as `/in-sight`, `/seo-rise`) is stripped and proxied to the project. This
     is how the service's own `backend-gateway-zyu0.onrender.com` address reaches
     every project before the custom domains resolve, and how clients built
     against the previous gateway's URLs keep working. FastAPI's `/docs` fetches
     `/openapi.json` from the host root, so under a prefix that one request is
     routed by its `Referer` - a browser-only convenience for the interactive
     docs; API clients never depend on it.
  Caddy trusts `X-Forwarded-*` from any source because only Render's edge can reach
  `$PORT`; apps therefore see the real client IP and `https` (measured), which
  OAuth redirect URIs (`request.url_for`) and secure cookies need.
- **Runner:** `workers = 1` (the norm here) runs `uvicorn` directly - supervisord
  restarts a crashed process, so a gunicorn master would only cost ~20 MB per
  project. `workers > 1` uses gunicorn + UvicornWorker when the project ships
  gunicorn, else uvicorn's `--workers`. `start_cmd` overrides all of it.
- **Request:** client → Render edge (TLS) → `$PORT` → Caddy → `127.0.0.1:<port>`
  → project app. Projects are mutually invisible.
- **Onboard:** add submodule + create Secret File + add a `[[project]]` block →
  deploy. Config regenerates; the new process joins.
- **Deploy:** `gateway deploy --wait` (autoDeploy is off). Every deploy rebuilds
  the image and **restarts all projects** (a few seconds - the accepted cost of
  consolidation; a project that deploys often should graduate out).
- **Swap in/out:** see `skill/SKILL.md`.

---

## 5. Resolved design decisions (do not relitigate without the owner)

| Topic | Decision |
|---|---|
| Routing | **`api.<name>.erdemgunseli.com`** (owner, 2026-09-06, replacing the earlier `<name>.api…` wildcard plan). The factory convention is frontend at `<project>.erdemgunseli.com`, API at `api.<project>.erdemgunseli.com`: the two share a project-specific parent (cookies scope to it, never to the shared apex) and graduating to a real domain (`heard.cc` / `api.heard.cc`) is a suffix swap. One DNS-only CNAME per project on Cloudflare points the API host at this service; no wildcard. Render's edge routes each hostname to whichever service registered it, so swaps never touch DNS. Path-prefix routing on the service's own host is the access path until a project's DNS exists and stays as the alias layer afterwards. |
| Process model | One process per project, own venv. Forced by package-name collision. |
| Reverse proxy | Caddy on `$PORT`, Host-routing, no TLS (Render terminates). |
| Process mgr | supervisord; `autorestart` gives startup isolation. |
| Secrets | Render Secret Files, one per project; injected into only that process by `launch.sh`. Logical isolation (not hardened vs same-UID). Service-level env vars are inherited by every process, so nothing project-specific ever goes there. |
| Database | **SQLite is the default; Postgres is the exception** - see §10 for the conditions, the readiness gate and the conversion. A managed Postgres costs more per month than the whole instance it hangs off, so a project keeps one only by failing a stated condition. The three current projects still run `db = "external"` against Postgres (Heard against **Heard DB**; In-Sight and SEO Rise against **Gateway DB** schemas `in_sight` / `seo_rise`) - measured SQLite-ready on 2026-09-07 but not yet converted, because converting needs a disk, a backup mechanism and the owner's go-ahead. |
| Hosting service | The pre-existing Render service **"Gateway Backend"** (`srv-d28cecuuk2gs73f5b5qg`, `egunseli4@gmail.com`, frankfurt, starter) was reused - same account and region as the projects' Postgres instances, which are reachable only by their internal hostnames. No persistent disk attached (no sqlite projects yet). |
| Env injection | Universal via supervisord + `launch.sh` (works for any app, not just `load_dotenv`). |
| Entrypoint | **Factory layout NOT assumed** - `backend_dir`, `app`, `start_cmd` are per-project in the manifest. |
| Resources | Soft only (worker count). No watchdog; container OOM restarts all (accepted). |
| Health | Gateway health = Caddy up (`/__gateway/health`); never gated on individual projects. |
| Migrations on SQLite | New SQLite projects: `create_all` + `alembic stamp head` (skip PG-only history). Future migrations must be portable (Alembic ops + `sa.func.now()`) and use `op.batch_alter_table` for alters. |
| Backups | Out of scope. |
| Name | `gateway`. |

---

## 6. Repository layout

```
CLAUDE.md                 this file (single source of truth)
gateway.toml              manifest - the single source of truth for projects (keys documented inline)
Dockerfile                caddy + supervisord + per-project venvs
.dockerignore             keeps frontends, node_modules, .git, secrets and venvs out of the image
entrypoint.sh             regenerate config -> exec supervisord
render.yaml               Render blueprint (documentation; Render has no Blueprint API)
scripts/
  manifest.py             manifest loader + project resolution (hosts, prefixes, ports, paths)
  generate_config.py      manifest -> Caddyfile, supervisord.conf, run.d/<name>.env
  build_venvs.py          one venv per project (build time)
  launch.sh               per-project launcher (secret injection, runner choice, log prefix)
  test_generate_config.py unit tests for the loader + generator (`python -m unittest scripts/test_generate_config.py`)
  sqlite_readiness.sh     boots the real stack on SQLite and exercises register/login
                          per project - the gate a project passes before db = "sqlite" (§10)
tools/
  render_api.py           minimal Render REST client
  db_pump.py              model-driven cross-engine data pump (PG <-> SQLite)
  gateway_cli.py          add/remove, up, status, domains, secrets-push, env-unset,
                          provision-sqlite, suspend/resume, deploy, wait, migrate
skill/SKILL.md            agent skill: swap-in / swap-out playbooks
skill/deploy.md           agent skill: stand up / redeploy / manage the Render service
skill/migrate.md          agent skill: move the whole gateway between Render accounts
skills/render-ops/        gateway-local skill: multi-account credentials + cross-account
                          migration (distinct from the factory render-ops skill in
                          QUANTSOC - that one owns provisioning + daily ops)
secrets.example/          example secret file (real ones are Render Secret Files)
projects/heard            git submodule -> ErdemGunseli/Heard        (backend: fastapi_backend/)
projects/insight          git submodule -> ErdemGunseli/In-Sight-AI  (backend: fastapi_backend/)
projects/seorise          git submodule -> ErdemGunseli/SEORise      (backend: fastapi_backend/)
```

The hosting contract a project must satisfy: a backend dir with a requirements
file and an importable ASGI app (`module:attr`) that ships uvicorn; all config from
the environment; **never self-bind a port** (the launcher binds it); portable column
types if it uses SQLite; a fast dependency-free health endpoint.

---

## 7. Status

**Live (2026-09-06):** the process-per-project gateway runs on "Gateway Backend"
from this repo, hosting all three projects, each against its real production
database. Access paths today:

| Project | Host-routed (live over HTTPS since 2026-09-06) | Path alias on the service host | Product's own API domain |
|---|---|---|---|
| heard | `https://api.heard.erdemgunseli.com` | `https://backend-gateway-zyu0.onrender.com/heard/…` | **`api.heard.cc` - moved onto the gateway 2026-09-07**; heard.cc's frontend calls it |
| insight | `https://api.insight.erdemgunseli.com` (the extension's `BASE_URL`) | `…/insight/…` and the legacy `…/in-sight/…` | `api.in-sight.ai` registered here 2026-09-07, but no DNS record for it exists |
| seorise | `https://api.seorise.erdemgunseli.com` | `…/seorise/…` and the legacy `…/seo-rise/…` | - |

**Every standalone service is suspended (2026-09-07)** - Heard Backend, In-Sight AI Backend
and SEO Rise Backend - so the gateway is the only thing serving these products. Their two
stale databases are suspended but kept as the rollback. Full evidence, including the
account migration that turned out to be necessary, is in `verification/2026-09-07/`.

Host routing was verified live the same day: `/healthz` or `/`, `/docs` with its spec,
and a DB-backed login lookup on each of the three hosts, from this environment and
from an outside vantage point.

**Validated locally before deploy** (real Caddy 2.10 + supervisord 4.2.5 + the
three venvs, throwaway SQLite secrets): Host routing for every host incl. extra
hosts (200), path-prefix routing incl. legacy aliases (200), bare prefix → 308,
`/docs` under a prefix with its spec routed by Referer (200), `/__gateway/health`
(200), unknown host/path (404), X-Forwarded-Proto/For reaching the app as
`scheme=https` + real client IP, graceful SIGTERM shutdown of all three. Memory
idle: **~385 MB PSS total** (heard ~129, seorise ~110, insight ~90, caddy ~27) on
a 512 MB instance - the headroom is thin; see §8.

**The database migration (2026-09-07).** The note that once stood here - that the
standalone In-Sight / SEO Rise databases were stale copies of what Gateway DB already held
- was **wrong when measured**. In-Sight's `in_sight` schema was empty (every account lived
only on the standalone database) and SEO Rise's held only its 2026 accounts, with 30 older
ones stranded on the standalone. Both were migrated in additively (4 users + 19 messages;
30 users, 42 conversations, 206 messages, 102 function calls, 1 contact), de-duplicating
by email and remapping primary keys, and then proven through the public API. Never assume a
sibling database is a copy - count the rows. Method, dry-run discipline, the two defects the
dry runs caught, and rollback: `verification/2026-09-07/`.

**Not exercised yet:** a real SQLite project on a persistent disk, a live
`db_pump` swap, and `gateway migrate` (all need a case to arise). Note that
`db_pump.py` was NOT the tool used above - it needs the project's own models and a
reachable database, and this environment cannot open port 5432; the migration ran as a
Render one-off job instead.

---

## 8. Remaining work

1. **erdemgunseli.com's registrar transfer to Cloudflare** (owner-paced). DNS is
   done: the zone went active on Cloudflare on 2026-09-06 and the three `api.<name>`
   hosts are verified and serving. What remains is the registration itself - unlock
   + auth code at GoDaddy, then Cloudflare's transfer page (runbook handed over
   2026-09-06) - after which the factory's domain tooling sets auto-renew, the
   registrar lock and DNSSEC. The In-Sight extension points at
   `api.insight.erdemgunseli.com`, which now resolves; a store release is what
   ships it to users.
2. ~~Move `api.heard.cc` onto the gateway~~ **done 2026-09-07** - moved with the owner's
   go-ahead; the certificate issued in under a minute and heard.cc's frontend, which calls
   that host, was verified afterwards.
3. **Convert the three projects to SQLite and retire both Postgres instances** (§10).
   All three were measured SQLite-ready on 2026-09-07 and their data is tiny (Heard
   9.7 MB / 663 rows; In-Sight 4 users; SEO Rise 34 users), so the two managed Postgres
   instances are now the bulk of the running cost for no capability the projects use.
   Blocked on three owner decisions, not on code: attaching the disk (which ends
   zero-downtime deploys), replacing the managed backups Postgres was providing, and a
   window for the cutover. This is the single biggest remaining saving.
4. **Memory headroom** - ~385 MB idle of 512 MB. If a project grows or a fourth
   joins, either trim its dependencies (the QUANTSOC lesson: the big SDKs - openai,
   anthropic, boto3 - are the cost) or upgrade the plan; both are owner calls.
5. **Delete the three suspended standalone services and the two suspended databases**
   once the owner is satisfied with the cutover. They are the rollback and cost nothing
   suspended, so there is no hurry - but note their databases are NOT redundant copies:
   their contents were migrated into Gateway DB on 2026-09-07, so deleting them discards
   the only second copy of those rows. Take a dump first if that matters.
6. **Wire deploy** - per-project CI bumps the submodule pointer + calls `gateway
   deploy`; today it's manual (`git submodule update --remote projects/<name>`,
   commit, `gateway deploy --wait`).
7. **Point the service at `main`** once this branch merges (`gateway up` reconciles
   `branch`); it currently deploys `claude/gateway-setup-integration-q2gen4`.
8. **Frontends at `<project>.erdemgunseli.com`** - the factory default for new
   instances without a bought domain; nothing points there yet (Heard is on
   `heard.cc`, SEO Rise's Vercel project has no custom domain). Add the CNAME to
   Vercel per project when wanted.

---

## 9. Cleanups applied when lifting `gateway/` out of QUANTSOC (done 2026-09-06)

Kept for the record; nothing here is pending.

- **QUANTSOC inheritance dropped** - this `CLAUDE.md` is the only rule set here.
- **Staging language removed** from `README.md`; QUANTSOC's `gateway/` folder now
  only points at this repo.
- **Projects added as real submodules** under `projects/` (`.gitmodules` tracks
  `main` for each).
- **Vault leftovers removed** (`git-lfs`, "vault media" - see §3).
- **Render env/secrets set out of band:** per-project Secret Files pushed; the old
  `TENANT_CONFIG` / `DATABASE_URL` service env vars removed (every process would
  inherit them). Render's GitHub App fetches the private submodules (Heard, SEO
  Rise) at build time, so no build-time token is needed.
- **Commit hygiene** (carried from QUANTSOC): never add `Co-authored-by` trailers
  naming agents/bots/AI, and never write "Generated with …". Write commit messages
  as a human author would.

---

## 10. Storage policy: SQLite by default, Postgres by exception

The gateway exists to run many low-traffic backends on **one** cheap always-on instance.
A managed Postgres per project quietly undoes that - each one costs more per month than
the instance itself - so the storage default is **SQLite on the persistent disk**, and a
project earns a Postgres only by failing one of the conditions below.

### When a project may NOT use SQLite

Any one of these is enough to keep it on Postgres. Record which one applies in the
project's `[[project]]` block, so the exception is a stated decision rather than drift:

1. **It needs writers in more than one process** - `workers > 1`, a background worker, or
   a second service reading the same data. SQLite takes one writer at a time.
2. **It uses PostgreSQL-only storage that cannot be made portable** - `JSONB` operators
   (not merely a JSON column), array columns, full-text search, extensions such as
   pgvector, or SQL that names `search_path` / schemas. A *portable* equivalent is
   usually cheap: Heard stores embeddings as a JSON list of floats and computes cosine
   similarity in Python precisely so it does not need pgvector.
3. **It needs managed backups or point-in-time recovery** and nothing else provides them
   - see the backup obligation below, which is the real cost of this policy.
4. **It is not low-traffic** - sustained write concurrency, or data heading past roughly a
   gigabyte. For scale, the answer is graduation to its own service, not a bigger disk.

Everything else is SQLite. Note what is *not* on that list: having a Postgres today is not
a reason to keep one.

### The readiness gate - run it, do not assume

`scripts/sqlite_readiness.sh` boots the real gateway against throwaway SQLite files and
exercises the write path that actually breaks: register an account, reject the duplicate,
log in with the right password and the wrong one. Enum columns, JSON columns, timestamp
defaults, unique constraints and cascade foreign keys all sit on that path. A project may
not be switched to `db = "sqlite"` until it passes.

Measured on 2026-09-07, all three hosted projects pass (11 of 12 probes; the twelfth is
Heard's registration returning 500 because the local SendGrid key is a dummy - the account
is created and logs in, so the database layer is fine). Their data is small enough that the
question is not close: Heard 9.7 MB / 663 rows, In-Sight 4 users, SEO Rise 34 users.

### The backup obligation

This is what Postgres was buying, and it is not optional. Render's managed Postgres does
daily backups and point-in-time recovery; a SQLite file on a disk does not, and the disk is
a single point of loss. **Before real user data moves to SQLite, a project must have a
backup that leaves the instance** - the natural fit is a periodic
`sqlite3 <db> "VACUUM INTO '<snapshot>'"` uploaded to the org's R2 bucket, since the
factory already holds Cloudflare credentials. A project whose data cannot be protected
that way keeps its Postgres under condition 3.

### The conversion

One-time, per project, and never a silent side effect of a deploy:

1. Attach the disk (`gateway up` without `--no-disk`) and confirm `data_dir` is mounted.
   Note the cost: a Render service with a disk cannot do zero-downtime deploys, so every
   gateway deploy becomes a short hard restart rather than a swap.
2. Provision the schema: `gateway provision-sqlite <name>` (`create_all` + `alembic stamp
   head`, so a Postgres-authored migration history is not replayed).
3. Copy the data with `tools/db_pump.py`, run **in the project's own venv** so its models
   drive the conversion, from inside the gateway container - `entrypoint.sh` runs a command
   passed to it instead of the gateway, so a Render one-off job can do this and is the only
   place that reaches both the project's Postgres and this disk.
4. Verify by row count per table, and by logging in as a real migrated account through the
   public API - not by the pump's own report.
5. Flip `db = "external"` to `db = "sqlite"`, remove `DATABASE_URL` from the project's
   secret file (the launcher owns it for sqlite projects), deploy, re-verify.
6. Only then suspend the Postgres - and keep it suspended, not deleted, until the first
   backup has been restored successfully at least once.

Reverse the same way: the pump is bidirectional.

## 11. Conventions for agents working here

- The **manifest is the only place** you register/configure a project; never
  hand-edit generated files (`generated/*`, `run.d/*`) - they're rebuilt at boot.
- Keep the gateway **un-opinionated** about project internals (env injection and
  entrypoint are per-project for this reason). Don't bake project-specific logic
  into the gateway scripts.
- **Nothing vault-related belongs here** (§3). If a task mentions Obsidian, vault
  sync, or git-lfs media, it belongs in `ErdemGunseli/VaultSync`.
- Risky/stateful operations (Render API, data pump) stay as **deterministic,
  idempotent** scripts; the agent skill orchestrates them. Never half-complete a
  swap - if a required API key is missing, warn and stop.
- **Binaries with file capabilities do not exec on Render.** The official Caddy
  image ships `/usr/bin/caddy` with `cap_net_bind_service` set; copied as-is,
  supervisord got `EPERM` on every spawn and the deploy failed its port scan while
  every project process was already up (2026-09-06). The Dockerfile re-copies the
  binary with plain `cp` to drop the xattr. Apply the same to any other binary
  lifted from a vendor image.
- **Render certificate issuance can stall for one hostname.** Two of the three
  `api.<name>` domains got their certificates within ten minutes of verification;
  `api.heard` sat at "verified" with no certificate for 25 minutes (TLS handshake
  failure from Render's edge, `no peer certificate`). Removing and re-adding the
  custom domain (`gateway domains --remove` / `--add` / `--verify`) issued it within
  30 seconds (2026-09-06). Check with `openssl s_client -servername <host>` before
  assuming DNS is at fault. A brief 403 HTML page from the edge right after issuance
  is the hostname finishing activation, not the app.
- Validate orchestration changes locally before relying on a deploy: run the unit
  tests, then boot the real thing (Caddy binary + `pip install supervisor` + the
  project venvs, `GATEWAY_APP_ROOT` pointed at a copy of the checkout, throwaway
  secret files, `PORT=18080`) and probe Host + path routing exactly as §7 lists.
- Credentials: `RENDER_API_KEY_GATEWAY` (the `egunseli4@gmail.com` account) in the
  environment or `~/.config/gateway/render.env`; never in the repo. The Secret File
  contents are assembled from the projects' existing Render env - never commit or
  print them.
