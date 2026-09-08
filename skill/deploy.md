---
name: gateway-deploy
description: Push the gateway to Render and manage the running service — create the service from the manifest, push per-project Secret Files, register the wildcard domain, deploy, and check status. Use to stand up the gateway for the first time or to redeploy/manage it.
---

# Gateway deploy & manage skill

Drives the deterministic primitives in `tools/gateway_cli.py` (which call
`tools/render_api.py`) to provision and operate the gateway's Render service.
Render has **no Blueprint API**, so provisioning is done by direct API calls, not
by `render.yaml` (that file is documentation only).

## Prerequisites

- **Render API keys** in a git-ignored env file, one per account, keyed by label:
  ```
  # ~/.config/gateway/render.env   (chmod 600; override path with GATEWAY_RENDER_ENV)
  RENDER_API_KEY_<LABEL>=rnd_xxx
  ```
  The label `gateway` is the default account for `up`/`status`/`deploy`; for this
  gateway that is the `egunseli4@gmail.com` account (the projects' Postgres
  instances live there and are reachable only by internal hostname). Real env vars
  override the file. If a key is missing the CLI warns and stops (no partial
  changes). The service name defaults to the manifest's `[gateway] service_name`
  ("Gateway Backend"); pass `--service` to override.
- **The gateway repo must be reachable by Render.** Render's GitHub App checks out
  the submodules it has access to before the build (the private Heard and SEO Rise
  repos included), so no build-time token is needed.
- **Per-project Secret Files** staged locally as `<secrets-dir>/<name>.env` (default
  `secrets/`, git-ignored). Each holds only that project's own secrets — the gateway
  owns `DATABASE_URL` for sqlite projects.

## Stand up the gateway (first deploy)

```
python tools/gateway_cli.py up --account gateway
```

`up` is idempotent and does, in order:
1. Create the Docker web service with the `/data` disk inline (if absent; `--no-disk`
   when no project uses sqlite). Resolves the workspace via `GET /owners` — pass
   `--owner <name|email|id>` if the key reaches several workspaces. On an existing
   service it instead **reconciles** `branch`, `autoDeploy=no` and the health path
   to the manifest's expectations and prints what it changed.
2. Push every local `secrets/<name>.env` as a Render Secret File.
3. Register each project's hostname (`host_template`, i.e. `api.<name>.<base>`) as a
   custom domain. `extra_hosts` are never added automatically - a product's own API
   domain may still be registered on another service; move it with
   `gateway domains --remove` / `--add` deliberately.
4. Trigger a deploy and wait for it to go `live`, then print the per-project URLs.

Then add one **DNS-only CNAME per project** at Cloudflare (the factory's domain-ops
tooling: `cf.py dns-upsert <zone> CNAME api.<name>.<base> <service>.onrender.com`) and
let Render verify (auto, or force with `gateway domains --verify api.<name>.<base>`).
No wildcard: Cloudflare's free certificate does not cover second-level names behind
its proxy, so the records stay grey-cloud and Render issues the certificates.

Flags: `--no-deploy` (provision only), `--region`, `--plan`, `--disk-gb`,
`--secrets-dir`, `--clear-cache`.

## Day-to-day management

- **Redeploy** (after a submodule bump or secret change — neither auto-deploys):
  `python tools/gateway_cli.py deploy --wait`
- **Status** (state, domains + verification, secret files):
  `python tools/gateway_cli.py status`
- **Domains**: `python tools/gateway_cli.py domains gateway --add '*.api.<base>'`
  (also `--remove`, `--verify`, or no flag to list).
- **Push one secret file**: `python tools/gateway_cli.py secrets-push <name> --file <path>`
- **Retire a service-level env var** (every process inherits those; project config
  belongs in Secret Files): `python tools/gateway_cli.py env-unset KEY [KEY ...]`, then redeploy.
- **Suspend / resume**: `python tools/gateway_cli.py suspend "Gateway Backend" --account gateway`
- **Provision a new sqlite project's schema** before first traffic:
  `python tools/gateway_cli.py provision-sqlite <name>`

## Notes

- Every gateway deploy restarts all project processes (a few seconds). A project
  that needs to deploy often should graduate to its own service.
- Gateway health is Caddy-up (`/__gateway/health`), never gated on a single project.
- Rate limits to respect: service creation 20/hr, custom domains 50/hr, deploys
  10/min/service.
