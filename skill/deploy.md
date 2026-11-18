---
name: gateway-deploy
description: Push the gateway to Render and manage the running service — create the service from the manifest, push per-project Secret Files, register project domains, deploy, and check status. Use to stand up the gateway for the first time or to redeploy/manage it.
---

# Gateway deploy & manage skill

Drives the deterministic primitives in `tools/gateway_cli.py` (which call
`tools/render_api.py`) to provision and operate the gateway's Render service.
Render has **no Blueprint API**, so provisioning is done by direct API calls, not
by `render.yaml` (that file is documentation only).

## Existing production binding (check before changing anything)

Read `CLAUDE.md` §7, **Current deployment** for the authoritative account email,
workspace, service ID and production image. The migrated service and retained
rollback service have the same display name, so matching "Gateway Backend" alone
does not establish the right destination. Verify the account/workspace and exact
service ID with read-only API calls before any mutation.

The three required matches are: credential account email **`s2671343@ed.ac.uk`**,
workspace/`ownerId` **`tea-d8lvllmq1p3s739u7ug0`**, and service ID
**`srv-dbuk4shsrm7s73f4mg20`**. Resolve the credential by that email from its existing
store; verify `GET /owners` includes that workspace, then `GET /services/<id>`
returns that exact ID and `ownerId`. Stop on any mismatch. Neither the shared
display name nor the `GATEWAY` environment label substitutes for these checks.

The live gateway uses `Dockerfile.migration`, a reviewed runtime branch and a pinned
image, with auto-deploy off. A documentation commit is not an instruction to rebuild.
Keep these settings and the approved live commit unless a runtime/code deployment
is intended. **Do not run `up` with its defaults against this live service:** it
reconciles the branch to `main`; its create path defaults to Oregon and the original
floating `Dockerfile`. Current deployment settings must be preserved explicitly.

## Prerequisites

- **Render API keys** in a git-ignored env file, one per account, keyed by label:
  ```
  # ~/.config/gateway/render.env   (chmod 600; override path with GATEWAY_RENDER_ENV)
  RENDER_API_KEY_<LABEL>=rnd_xxx
  ```
  The label `gateway` is the default account for `up`/`status`/`deploy`; for this
  gateway that is the account recorded in `CLAUDE.md` §7 (currently
  `s2671343@ed.ac.uk`). The label is only a local key selector, not an account
  identity. Resolve the current account's existing credential from its authoritative
  store; if adapting it to this legacy CLI, inject `RENDER_API_KEY_GATEWAY` only into
  the operation's process environment. Do not reuse the old account's key or persist
  a second credential copy just to create the label. Real env vars
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

This section is for a new gateway. It is not the maintenance path for the migrated
production service above. Adding a project to the current locked image also needs
its reviewed runtime lock; the existing three locks deliberately preserve the
running projects' dependency sets.

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

Perform the account/service-ID preflight above first. The commands below select by
display name within the chosen account; keep the live branch/Dockerfile settings.

- **Intended code/runtime rebuild** (after reviewing the branch and runtime locks):
  `python tools/gateway_cli.py deploy --wait`. This CLI builds the selected branch's
  latest commit; it does not reuse or pin the approved production image. Use it
  only when that code/runtime rebuild is intended.
- **Environment/Secret File-only restart**: use
  `POST /services/<current-service-id>/deploys` with `{"deployMode":"deploy_only"}`
  only after fresh read-only deployment/build inspection proves the latest image
  this operation will reuse matches the approved live commit in `CLAUDE.md` §7.
  The current live deploy alone is insufficient evidence if a newer image was
  built. If the latest-image identity cannot be proven or differs, stop and choose
  an explicitly reviewed image/deployment path. Verify the resulting commit,
  normal command and all three app routes after restart. The CLI `deploy` command
  does not implement this image-reuse operation.
- **Status** (state, domains + verification, secret files):
  `python tools/gateway_cli.py status`
- **List domains**: `python tools/gateway_cli.py domains "Gateway Backend" --account gateway`.
  Add/remove/verify deliberately with an explicit real hostname, such as
  `--verify api.heard.erdemgunseli.com`; the live configuration uses named hosts,
  not the former wildcard example.
- **Push one secret file**: `python tools/gateway_cli.py secrets-push <name> --file <path>`
- **Retire a service-level env var** (every process inherits those; project config
  belongs in Secret Files): `python tools/gateway_cli.py env-unset KEY [KEY ...]`,
  then apply the guarded image-reuse restart above.
- **Suspend / resume**: `python tools/gateway_cli.py suspend "Gateway Backend" --account gateway`
- **Provision a new sqlite project's schema** before first traffic:
  `python tools/gateway_cli.py provision-sqlite <name>`

## Notes

- Every gateway deploy restarts all project processes (a few seconds). A project
  that needs to deploy often should graduate to its own service.
- Gateway health is Caddy-up (`/__gateway/health`), never gated on a single project.
- Rate limits to respect: service creation 20/hr, custom domains 50/hr, deploys
  10/min/service.
