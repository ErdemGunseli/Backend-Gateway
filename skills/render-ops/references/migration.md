# Cross-account migration runbook

Render **cannot transfer a service between accounts** (no API, no dashboard action —
confirmed by Render's own guidance and an open feature request). The supported pattern,
which `render-ops migrate` automates, is: **recreate** the service in the destination
from the source's spec, **copy the data** over `render ssh`, then **cut the custom
domains over** and flip DNS.

## Mental model

| What | How it moves |
|---|---|
| Service definition | Read source `serviceDetails` via the API, recreate in dest (`clone_kwargs`) |
| Env vars | API copy (`GET` source → bulk `PUT` dest) |
| Secret files | API copy (`GET` each → `PUT` dest); land at `/etc/secrets/<name>` |
| Persistent-disk data | `render ssh` tar stream (snapshot on source → restore on dest) |
| Custom domains | Remove from source, add to dest, then DNS cutover |

Anything the create-safe API path doesn't return — **autoscaling, build filters,
pre-deploy commands, health-check tweaks** — won't be cloned; reconcile those by hand
on the dest after `provision` (check `status`).

## Prerequisites

- Both account keys in `~/.config/render/accounts.env` (labels `--src`/`--dst`).
- **Render CLI** + **SSH access** for both accounts (paid services). Verify:
  `RENDER_API_KEY=$key render ssh <service-id> -- echo ok`.
- You control DNS for any custom domains; lower their TTL (e.g. 60s) one old-TTL ahead.

## Data copy — why `render ssh`

A Render disk is reachable **only from its own running service**; one-off jobs can't
see it and snapshots can't be cross-restored. So the data must leave through a process
running on the service. `migrate`'s `data` stage:

1. On the source, makes a **consistent snapshot** of each `*.db`
   (`PRAGMA wal_checkpoint(TRUNCATE)` + `VACUUM INTO`) and tars it together with any
   non-db files. (`--no-vacuum` → plain tar, for non-SQLite disks.)
2. Streams the tarball down through `render ssh` to `--workdir`.
3. Streams it up into the dest disk's mount path through `render ssh`.

Both sides being SQLite means a **file copy**, not a cross-engine pump. `render ssh`
must hit the **running** service — `--ephemeral` instances have no disk attached.

For non-SQLite disks with active writers, a live tar can be torn; quiesce writers
(app read-only mode or a brief stop) before the final `data` pass, or accept an
initial warm copy followed by a final sync during a stop window.

## Minimal-downtime sequence

```
# pre-stage, source still serving (no downtime):
render.sh migrate --service S --src A --dst B --stage provision --stage env --stage secrets
render.sh migrate --service S --src A --dst B --stage data          # warm copy

# cutover window (brief):
render.sh migrate --service S --src A --dst B --stage data          # final sync
render.sh migrate --service S --src A --dst B --stage domains        # frees + rebinds domain
#   -> update DNS to the dest service, then:
render.sh domains B S --verify <domain>
render.sh migrate --service S --src A --dst B --stage finalize       # health-check + suspend source
```

## Rollback

- **Before `domains`:** nothing user-facing moved — abort freely.
- **After `domains`** (highest commitment): re-remove the domain from dest, re-add to
  source, revert DNS (CNAME + `_acme-challenge`). Expect another "in use" lock and a
  cert-reissue delay.
- The source is left **suspended, not deleted**, after `finalize` — resume it to roll
  back. Delete it only once the dest is confirmed good.

## Gotchas

- **Domain "in use" lock** is the critical path: you can't pre-bind a domain to the
  dest; it must be removed from the source first. Stuck domains occasionally need Render
  support to release.
- **New dest = new `onrender.com` target and new `_acme-challenge`** value — don't reuse
  the old DNS records.
- **Wildcard needs its parent on Render too** — move `*.api.example.com` and
  `api.example.com` together.
- **WAL sidecars:** a raw copy that omits `-wal`/`-shm` loses recent writes; the default
  `VACUUM INTO` path folds them in, so prefer it for SQLite.
- **Rate limits:** service-create 20/hr, custom-domains 50/hr — fine for one service,
  relevant if scripting many.
