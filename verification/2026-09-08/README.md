# 2026-09-08 — the SQLite conversion, and the two defects that made it a 32-minute outage

All three hosted projects now run on **SQLite files on the gateway's persistent disk**.
Every managed Postgres behind them is suspended. This folder is the evidence, including
the parts that went wrong.

## What changed

| | before | after |
|---|---|---|
| heard | Heard DB (managed Postgres) | `/data/heard.db` — 663 rows, 9 tables |
| insight | Gateway DB, schema `in_sight` | `/data/insight.db` — 23 rows, 4 tables |
| seorise | Gateway DB, schema `seo_rise` | `/data/seorise.db` — 401 rows, 6 tables |

A 1 GB disk (`dsk-dafqc31t0dsc73f9ehv0`) is mounted at `/data`. Attaching it first
required disabling autoscaling — Render refuses a disk on a scaled service — and it
ends zero-downtime deploys, which is what turned the next defect into an outage.

Suspended, in this order and only after the verification below passed: **Heard DB**
(`dpg-d6e8tj9r0fns73db6350-a`) and **Gateway DB** (`dpg-d28cf66uk2gs73f5but0-a`). The two
already-suspended standalone databases are untouched. Nothing was deleted.

## The two defects

**1. The health route only answered on unclaimed Hosts.** Caddy's `/__gateway/health`
matcher lived only in the catch-all site block. Render's health check carries a Host
header of its own choosing, and when that is one of the project hostnames the request
lands in that project's site block and reaches the app, which 404s. The service log
shows twelve minutes of `[heard] GET /__gateway/health 404` while every process was
healthy; the deploy timed out and, with a disk attached, took production down with it
rather than rolling back cleanly. The health route is now emitted in **every** site
block, verified against a real Caddy 2.10 for all six hostnames plus the service host.

**2. The seeder reported success after copying nothing.** It looked source tables up by
a key whose shape depends on the schema arguments — `public.users` when reflected,
`users` when searched — so for a project on the `public` schema every lookup missed,
every table was logged "absent in source, skipped", and it exited 0. It also treated
"the database file exists" as "already seeded", so the empty file it left behind looked
seeded forever. **Heard booted on an empty database.** The seeder now reflects into bare
table names, refuses to report success when it matched no table at all, verifies each
count against the source's own `count(*)` rather than against what it believed it sent,
and decides by rows in the target rather than by the file. That last change also means it
never contacts the source once a project is converted — which is why suspending Postgres
does not stop a project booting.

Both were caught only because the deploy failed loudly. The first defect is what a
health check is for; the second would have been silent. `scripts/test_generate_config.py`
now pins the health route in every routed block, and the seeder's guards were exercised
against a synthetic source before the deploy: it seeds an empty target, no-ops on a
populated one **with the source file moved away** (proving it is not contacted), and
exits non-zero when the source holds none of the model tables.

## Timeline

| UTC | |
|---|---|
| 06:09 | first cutover deploy starts; superseded |
| 06:19 | second deploy starts — production down from here (a disk means no zero-downtime swap) |
| 06:36 | `update_failed`: health check timed out |
| 06:51 | fixed deploy live; all three projects seeded and serving |
| 06:53 | service restarted deliberately — data survived, seeder no-oped |
| 06:56 | Heard DB and Gateway DB suspended |
| 06:58 | full suite re-run green with no Postgres running anywhere |

**Total downtime: 32 minutes**, against the hour the owner allowed.

## Verification, all of it after the cutover

- `e2e.sh` — 19 of 20 probes pass. The twentieth is `heard.cc` answering 308 to
  `https://www.heard.cc/`, which is Vercel's apex redirect and not the gateway;
  `www.heard.cc` itself is 200.
- `sqlite_proof.sh` — the migrated rows are really there. For each product, a login with
  a **real migrated address** and a wrong password is rejected as a bad password (401),
  while an address that never existed is rejected as unknown (404). The two answers
  differ only because the app read the row. Run twice: after the deploy, and again after
  the restart and the Postgres suspension. Addresses are read from a `0600` file and
  never printed.
- `write_proof.sh` — the production **write** path, end to end on SEO Rise: register
  (201) → duplicate rejected (409) → login with the right password (200) → with the
  wrong one (401) → delete (204) → gone (404). It creates and deletes the same account,
  so it leaves no residue.
- Restart persistence — the service was restarted on purpose at 06:53. The seeder logged
  `target already holds 663 / 401 / 23 rows; nothing to seed` for the three projects,
  which is both the disk surviving and the idempotence guard working.
- `screenshots/` — every project's Swagger docs **on its own hostname**
  (`api.heard.cc`, `api.heard.erdemgunseli.com`, `api.insight.erdemgunseli.com`,
  `api.seorise.erdemgunseli.com`) with the spec loaded, plus the path aliases and the
  legacy `/in-sight` and `/seo-rise` prefixes, gateway health, and the unknown-path 404.

## What the timezone outage added, later the same day

The conversion looked verified and was not. Hours after the cutover the owner reported
Heard broken, and the cause was a second portability defect this folder's checks could
not see: **SQLite ignores `DateTime(timezone=True)`** and returns naive datetimes, so
every `stored < datetime.now(UTC)` comparison raised. It took session expiry, email
verification, password reset, and the profile response the frontend fetches right after
login. SEO Rise carried the identical defect, latent, on its verification path; In-Sight
has no timezone-aware columns and was unaffected. Fixed as a column type in each product
repo (`UtcDateTime`), deployed and proven through the real login path.

The lesson is about the gate, not the bug: **`sqlite_readiness.sh` walks the write path
and never the clock.** Register, reject a duplicate, log in - nothing there compares a
stored timestamp with now, which is exactly why it certified all three projects while two
of them were broken. `write_proof.sh` above has the same blind spot and passed for the
same reason. A readiness run needs at least one flow that reads a timestamp back.

Two further findings came out of the follow-up:

- **One-off jobs on this Docker service do not execute at all.** A job whose only action
  was an HTTP request to the gateway never made it - and the access log, unlike job logs,
  is readable. So the `succeeded` status three earlier jobs returned meant nothing, and
  **the persistent disk is reachable only from the running service**.
- **Heard's SendGrid account is out of credits** (`Maximum credits exceeded`, returned as
  a 401). New accounts cannot verify. This predates the gateway move.

## What was NOT checked

- **No backup has been restored.** Render snapshots the disk daily, but that promise is
  untested here; §10 says keep the Postgres instances suspended rather than deleted until
  one restore has succeeded, and that still stands.
- Render does not surface this Docker service's one-off job logs, so an independent
  `sqlite3` count on the disk could not be read back. An attempt to use the job's exit
  code instead was abandoned when a deliberately-wrong control run also reported
  "succeeded" — the status does not reflect the exit code, so it proves nothing. The row
  counts above rest on the seeder's source-verified copy, the post-restart recount, and
  the public-API probes.
- A SQL NULL in a JSON column arrives as the JSON value `null` rather than SQL NULL,
  because that is how SQLAlchemy's JSON type binds None. Both read back through the ORM
  as None; only raw `IS NULL` SQL would tell them apart. Not exercised against these apps.
