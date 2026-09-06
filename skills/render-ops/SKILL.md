---
name: render-ops
description: >-
  Migrate a Render service — together with its persistent-disk data, secret files,
  environment variables, and custom domains — from one Render account to another,
  and manage multi-account Render credentials from the command line. Use for
  cross-account migration/relocation ("move my service to my other Render account",
  "migrate this off this account") and for wiring up multiple Render API keys, plus
  inspect/suspend/resume in that multi-account context. Provisioning a factory
  backend, env sync, and day-to-day single-service operation belong to the factory
  skill at .cursor/skills/render-ops/.
---

# render-ops

> **Sibling skill:** `.cursor/skills/render-ops/` (the factory skill, auto-loaded by
> agent surfaces) owns provision-from-template, env sync, and day-to-day operation.
> This copy is gateway-local and read manually; it owns multi-account credential
> wiring and cross-account migration. Keep the split.

Operate Render services across many accounts, and migrate any service (with its
data) between accounts. Render has **no API to transfer a service between accounts
and no Blueprint API**, so provisioning and migration are done with direct REST
calls plus `render ssh` for disk data.

## Setup (once)

API keys live in an **external, git-ignored file — never inside this skill** (skills
get packaged and shared; a bundled key would leak). One key per account, by label:

```
# ~/.config/render/accounts.env   (chmod 600; override path with RENDER_ACCOUNTS_ENV)
RENDER_API_KEY_PERSONAL=rnd_xxx
RENDER_API_KEY_CLIENTB=rnd_yyy
```

Run every command through the bundled wrapper, which sources that file and injects
the keys into the subprocess **without printing them into the conversation**:

```
scripts/render.sh <command> [args]
```

Confirm the keys load: `scripts/render.sh accounts --check` (lists each label and the
workspace its key reaches). If a key is missing the CLI says which one and stops.

> Why external + wrapper: see `references/credentials.md`. Short version — a skill is
> a portable, shareable artifact, so secrets must stay outside it; the wrapper keeps
> the values out of the agent's context. For **MCP-based** access the credential
> belongs to the MCP server config, not the skill at all.

## Managing services

Accounts are referenced by their label; services by name.

```
scripts/render.sh accounts --check                 # labels + which workspace each reaches
scripts/render.sh services <account> [--type web_service]
scripts/render.sh status   <account> <service>     # state, disk, domains, secrets
scripts/render.sh deploy   <account> <service> --wait
scripts/render.sh domains  <account> <service> [--add d|--remove d|--verify d]
scripts/render.sh suspend|resume <account> <service>
```

## Migrating a service between accounts

`migrate` recreates the service in the destination **from the source's own spec**
(read via the API), then copies data, secrets, env, and domains. It runs in named
stages so it is **resumable** and has clear rollback points.

```
scripts/render.sh migrate --service <name> --src <from-account> --dst <to-account> \
    [--owner <dest-workspace>] [--plan <p>] [--region <r>] [--stage <stage> ...]
```

Stages (default: all, in order):

1. **provision** — clone the source service spec (type, repo/branch, plan, region,
   disk) into the dest, run its first deploy, wait for `live`. Idempotent.
2. **env** — copy environment variables.
3. **secrets** — copy Secret Files.
4. **data** — snapshot the source disk and stream it to the dest over `render ssh`.
   SQLite-safe by default (`PRAGMA wal_checkpoint` + `VACUUM INTO` per `*.db`); use
   `--no-vacuum` for a plain tar of arbitrary files.
5. **domains** — remove each custom domain from the source (a domain lives on one
   service at a time) and add it to the dest. **You then update DNS** to the dest and
   verify.
6. **finalize** — confirm the dest is healthy, then **suspend** (not delete) the
   source as a rollback safety net.

**Prerequisites for the data stage:** the Render CLI (`render`) installed and SSH
access working for both accounts (paid services). Verify before migrating:
`RENDER_API_KEY=$key render ssh <service-id> -- echo ok`.

**Minimal downtime:** lower the domain's DNS TTL beforehand; run `provision`,
`secrets`, `env`, and an initial `data` copy with the source still serving; then, in
a tight window, do a final `data` sync, the `domains` flip + DNS, and `finalize`.

For the full runbook, rollback details, and gotchas (the "domain in use" lock, WAL
sidecars, ephemeral-SSH-has-no-disk, wildcard parent records), read
`references/migration.md`. For exact API endpoints, fields, and rate limits, read
`references/api.md`.

## When NOT to reach for migrate

- Moving a service **within one account** between regions/plans — that's a
  redeploy/resize, not this cross-account flow.
- Apps with a managed database (Render Postgres): migrate the data with the
  database's own dump/restore, not the disk `data` stage (which is for files on a
  persistent disk).

## References

- `references/api.md` — Render REST endpoints, request fields, pagination, rate limits.
- `references/migration.md` — the cross-account runbook, data-copy mechanics, rollback.
- `references/credentials.md` — where Render keys go, the wrapper pattern, MCP vs CLI.
