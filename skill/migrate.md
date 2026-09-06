---
name: gateway-migrate
description: Move the entire deployed gateway — the Render service, its persistent-disk SQLite data, its Secret Files, and its wildcard custom domain — from one Render account to another, with minimal downtime. Use to relocate the gateway between Render accounts/workspaces.
---

# Gateway cross-account migration skill

Render **cannot transfer a service between accounts** (no API, no dashboard action).
The supported pattern — implemented by `gateway migrate` — is: **recreate** the
service in the destination account from the same repo, **copy the SQLite data** over
`render ssh`, **copy the Secret Files** via the API, then **cut the wildcard domain
over** and flip DNS. The command is staged so it's resumable, with explicit rollback
points.

## Prerequisites

- **Both account keys** in `render.env`, by label: `RENDER_API_KEY_<SRC>` and
  `RENDER_API_KEY_<DST>` (see `skill/deploy.md`).
- **Render CLI** (`render`) installed and **SSH access** working for both accounts
  (SSH is available on paid web services). The data legs shell out to `render ssh`;
  each leg sets `RENDER_API_KEY` to the right account. Verify first:
  `RENDER_API_KEY=$SRC render ssh <src-service-id> -- 'echo ok'`.
- **You control DNS** for the base domain, and have lowered its TTL (e.g. 60s) at
  least one old-TTL before cutover.

## Why SQLite + this approach

Both sides are SQLite, so data moves as a **file copy**, not a cross-engine pump. The
data stage makes a consistent snapshot of each DB with `PRAGMA wal_checkpoint` +
`VACUUM INTO`, tars it down through `render ssh`, and restores it into the dest disk.
A Render disk is reachable **only** from its own running service, so SSH is the only
way data leaves; one-off jobs can't see the disk and snapshots can't cross-restore.

## Run the migration

Full run (all stages, in order):
```
python tools/gateway_cli.py migrate --src <SRC> --dst <DST> --owner <dest-workspace>
```

Stages (run a subset with repeated `--stage`, e.g. `--stage data --stage domains`):

1. **provision** — create the dest service (disk inline) from the same repo, run its
   first deploy, wait for `live`. Idempotent: an existing dest service is reused.
2. **secrets** — copy every Secret File from source → dest via the API.
3. **data** — snapshot each `/data/*.db` on the source (`VACUUM INTO`), stream the
   tarball down via `render ssh`, and restore into the dest disk. Re-runnable.
4. **domains** — remove `*.api.<base>` + `api.<base>` from the **source** (a domain
   can only live on one service), add them to the **dest**. Then **manually update
   DNS** to the dest service and verify:
   `gateway domains gateway --account <DST> --verify '*.api.<base>'`.
5. **finalize** — wait for the dest to be healthy at `https://<base>/__gateway/health`;
   only then **suspend** the source (kept for rollback, not deleted).

## Minimal-downtime sequence

Pre-stage everything except the domain while the source still serves: run
`provision`, `secrets`, and an initial `data` copy with no downtime. Then, in a tight
window: brief read-only/quiesce on the source → final `data` sync → `domains` flip +
DNS → `finalize`. The only user-visible gap is the domain-rebind + TLS-reissue, kept
small by the low DNS TTL.

## Rollback

- Before **domains**: nothing has moved user-facing; abort freely.
- After **domains** (highest-commitment step): re-remove the domain from dest,
  re-add to source, revert DNS. Expect another "in use" lock + cert-reissue delay.
- The source stays **suspended, not deleted**, after `finalize` — resume it to roll
  back. Delete it manually only once the dest is confirmed good.

## Gotchas

- A custom domain stuck "in use" on the old service can need Render support to
  release — budget for it.
- `render ssh --ephemeral` does **not** attach the disk; the data stage must hit the
  running service.
- Copy/checkpoint WAL sidecars (the snapshot handles this via `VACUUM INTO`).
- New dest service = new `onrender.com` target **and** new `_acme-challenge` value —
  don't reuse the old DNS records.
