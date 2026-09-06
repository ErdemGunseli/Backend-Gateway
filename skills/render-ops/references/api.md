# Render REST API reference

Base URL `https://api.render.com/v1`. Official docs: https://api-docs.render.com/ and
https://render.com/docs. This is the subset `render_ops.py` uses, with the gotchas
that matter for provisioning and migration.

## Table of contents
- [Auth & workspaces](#auth--workspaces)
- [Services](#services)
- [Env vars & secret files](#env-vars--secret-files)
- [Custom domains](#custom-domains)
- [Deploys & lifecycle](#deploys--lifecycle)
- [Disks](#disks)
- [Pagination & rate limits](#pagination--rate-limits)

## Auth & workspaces
- **Auth:** `Authorization: Bearer <api-key>`. Keys are created per account in the
  dashboard (Account Settings → API Keys), shown in full only once.
- A key reaches **every workspace its account belongs to**. A workspace ("owner") is
  type `user` or `team`. `GET /owners` → `[{id, name, email, type}]`; the `id` is the
  `ownerId` required when creating a service.
- **Gotcha:** there is no per-team token — a leaked key exposes all the account's
  workspaces. Different *accounts* (different owner emails) need different keys.

## Services
- `GET /services` — list (filter `?name=`, `?type=`); cursor-paginated.
- `GET /services/{id}` — full service incl. `serviceDetails` (runtime, plan, region,
  dockerfilePath, healthCheckPath, disk, url).
- `POST /services` — create. Body: `type` (`web_service|private_service|
  background_worker|cron_job|static_site`), `name`, `ownerId`, `repo`, `branch`,
  `autoDeploy` (`yes|no`), `serviceDetails{...}`, and optional `envVars[{key,value}]`
  + `secretFiles[{name,content}]`. For Docker: `serviceDetails.runtime="docker"`,
  `dockerfilePath`, `plan`, `region`, `healthCheckPath`, and `disk{name,mountPath,
  sizeGB}` (the disk can be created inline here).
- **No Blueprint API.** `render.yaml` is only created/synced via the dashboard + Git;
  for scripted provisioning use `POST /services` directly.
- **Gotcha:** a private GitHub repo must be connected to the workspace (GitHub app)
  before the API can build from it; the API call alone doesn't grant access. Public
  repos build from the public Git URL.

## Env vars & secret files
- `GET /services/{id}/env-vars` — list `{key,value}` (paginated).
- `PUT /services/{id}/env-vars` — **bulk replace** (array of `{key,value}`); omitted
  keys are removed. Single-key upsert: `PUT /services/{id}/env-vars/{key}`.
- `GET /services/{id}/secret-files` — list. `PUT /services/{id}/secret-files/{name}`
  with `{content}` — lands at `/etc/secrets/{name}` in the container.
- **Gotcha:** env/secret writes are **not auto-deployed** — trigger a deploy to roll
  them out.

## Custom domains
- `POST /services/{id}/custom-domains` `{name}` · `GET .../custom-domains` ·
  `DELETE .../custom-domains/{idOrName}` · `POST .../custom-domains/{idOrName}/verify`.
- A **wildcard** `*.api.example.com` requires its **parent** `api.example.com` to also
  point at Render — register both. DNS for a wildcard: CNAME `*.api` →
  `<service>.onrender.com`, plus `_acme-challenge` → `<service-id>.verify.renderdns.com`
  and `_cf-custom-hostname` → `<service-id>.hostname.renderdns.com`.
- **Gotcha:** a domain can live on only **one service at a time** — adding it where it
  still exists elsewhere errors ("already exists on another site"). The new service has
  a different `onrender.com` target and a new `_acme-challenge` value, so DNS changes on
  every cross-service move.

## Deploys & lifecycle
- `POST /services/{id}/deploys` `{clearCache: "clear"|"do_not_clear"}` → deploy object.
- `GET /services/{id}/deploys/{deployId}` — poll `status`. Success = `live`; failure =
  `build_failed|update_failed|pre_deploy_failed|canceled|deactivated`.
- `POST /services/{id}/suspend` · `POST /services/{id}/resume`.

## Disks
- `POST /disks` `{serviceId,name,mountPath,sizeGB}` (or inline at service create).
  `PATCH /disks/{id}` is grow-only. `GET /disks?serviceId=`.
- **Gotcha — snapshots can't migrate data.** Render auto-snapshots every 24h, but
  `POST /disks/{id}/snapshots/restore` restores **only to the same disk on the same
  service** — no export, no cross-service/cross-account restore. A disk is reachable
  **only from its own running service**, and one-off jobs can't see it. So moving disk
  data between accounts must go through the running service (this skill uses
  `render ssh`).

## Pagination & rate limits
- **Pagination:** `?limit=` (≤100) + `?cursor=`; each response item carries a `cursor`;
  pass the last item's cursor for the next page. `render_api.py._paged` handles this.
- **Rate limits:** GET 400/min; POST/PATCH/DELETE 30/min; **service creation 20/hr**;
  **custom domains 50/hr**; deploys 10/min/service. On `429`, back off (no
  `Retry-After`); watch `Ratelimit-Remaining`/`Ratelimit-Reset` headers. Space out bulk
  domain adds.
