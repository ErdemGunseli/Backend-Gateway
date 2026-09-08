# Full cutover to the gateway - 2026-09-07

Every product now runs **only** on the gateway. The three standalone Render services are
suspended, and the two stale product databases are suspended but **kept** as the rollback.

## What moved

| Product | API domain | Database | Standalone service |
|---|---|---|---|
| Heard | `api.heard.cc` moved off "Heard Backend" onto the gateway (its own certificate issued within a minute) | unchanged - the gateway and the old service always read the same **Heard DB**, so nothing to migrate | **suspended** |
| In-Sight | `api.in-sight.ai` registration moved onto the gateway (still no DNS record for it; `api.insight.erdemgunseli.com` is the live host) | **migrated** - see below | **suspended** |
| SEO Rise | `api.seorise.erdemgunseli.com` (unchanged) | **migrated** - see below | **suspended** |

## The database finding, which reversed an assumption

`CLAUDE.md` had recorded that the standalone In-Sight / SEO Rise databases were "stale
copies" of data already in Gateway DB. **Measured, that was wrong**, and suspending them
without checking would have stranded real accounts:

| Schema | Before | Added | After |
|---|---|---|---|
| `in_sight.users` | 0 | 4 | 4 |
| `in_sight.messages` | 0 | 19 | 19 |
| `seo_rise.users` | 4 | 30 | 34 |
| `seo_rise.conversations` | 2 | 42 | 44 |
| `seo_rise.messages` | 8 | 206 | 214 |
| `seo_rise.function_calls` | 6 | 102 | 108 |
| `seo_rise.contacts` | 0 | 1 | 1 |

In-Sight's gateway schema was **completely empty** - every one of its accounts lived only
on the standalone database. SEO Rise's gateway schema held only the 2026 accounts; the 30
older ones (newest 2025-07-21) were still on the standalone database, left behind when
traffic moved to the previous gateway in August 2025. Three accounts existed on both sides
and were de-duplicated by email rather than doubled.

## How the migration was run, and why it is safe

`migrate_generic.py` (copied here) runs as a **Render one-off job inside a service that can
reach both databases** - this environment cannot open port 5432 at all, and the Docker
gateway service ignores a job's start command because of its ENTRYPOINT, so the native
In-Sight service was the only usable host. Credentials travelled as Render environment
variables, never in a command string, and were deleted afterwards.

The migrator is **additive**: it never updates or deletes an existing row, skips a source
user whose email is already present (re-pointing that person's rows at the account already
there), remaps every primary key past the target's current maximum so the two eras cannot
collide, drops a child row whose parent was not copied rather than leaving it dangling, and
resets the id sequences at the end. Every run was dry-run first.

Two defects the dry runs caught before anything was written, both fixed here:
- de-duplication only worked when rows were actually being written, so a dry run disagreed
  with the real run for a person present in two source schemas;
- the two eras' schemas disagree on column type - the old database stores some values as
  text holding a **Python** repr (`{'a': 1}`) where the live schema has a `json` column.
  Those are parsed and converted; anything unparseable would be stored as a JSON string and
  reported (nothing hit that path).

Both failed attempts died before their commit, so the target was untouched - confirmed by
the "before" counts on the following run.

## End-to-end verification, with every standalone suspended

`e2e.sh` (copied here) - **19 of 20 probes passed**; the twentieth was `heard.cc` returning
Vercel's apex-to-`www` 308 redirect, which follows to 200 with the title "Heard".

Covered: gateway health; unknown host 404; for each product its root, `/docs` with the spec,
an unknown-user login (proving the request reached the real database), the
`api.<project>.erdemgunseli.com` host, and the path aliases including the previous gateway's
`/in-sight` and `/seo-rise`; plus both frontends.

The migrated accounts were then proven **through the public API**: a login attempt for one
restored account on each product returns "Please check the email and password" (401) rather
than "Please log in with a valid account" (404) - the live service can see the account. That
check ran inside a Render job so no real address left Render.

The three suspended hosts (`heard.onrender.com`, `aiscreenreader.onrender.com`,
`seorise.onrender.com`) all return 503, as they should.

## Rollback

Nothing was deleted. Resume the standalone service and its database, and move `api.heard.cc`
back with `gateway domains "Heard Backend" --add api.heard.cc`. The migrated rows are
identifiable by id range (`in_sight` ids 1-4; `seo_rise` user ids 5+ and their children).
