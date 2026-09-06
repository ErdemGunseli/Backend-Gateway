# Production verification - 2026-09-06

First deploy of the process-per-project gateway to the Render service **Gateway
Backend** (`srv-d28cecuuk2gs73f5b5qg`, `egunseli4@gmail.com`, frankfurt, starter),
commit `68259f7` on `claude/gateway-setup-integration-q2gen4`, then a second deploy
after the old `TENANT_CONFIG` / `DATABASE_URL` service env vars were removed.

Everything below was **measured** against `https://backend-gateway-zyu0.onrender.com`
(the custom `*.api.erdemgunseli.com` hosts are registered on the service but wait on
DNS the owner controls, so Host routing was verified locally only - see `CLAUDE.md` §7).

| Check | Result |
|---|---|
| `/__gateway/health` | 200 `ok` |
| `/heard/healthz`, `/heard/`, `/heard/docs` (+ spec) | 200, 200, 200 with operations rendered |
| `/insight/`, `/in-sight/`, `/insight/docs`, `/in-sight/docs` | 200 each, both docs pages rendered their spec |
| `/seorise/`, `/seo-rise/`, `/seorise/docs`, `/seo-rise/docs` | 200 each, both docs pages rendered their spec |
| `POST /heard/auth/token` unknown user | 404 "couldn't find an account" - the lookup ran against Heard DB |
| `POST /in-sight/auth/token`, `POST /seo-rise/auth/token` unknown user | 404 "Please log in with a valid account" - lookups ran against Gateway DB |
| Unknown path / host | 404 `gateway: unknown host` |
| Runtime log after deploy | all three `Application startup complete`; Heard's Alembic run on Postgres clean; SIGTERM shutdowns of the superseded instances clean; no errors |
| Service state | branch `claude/gateway-setup-integration-q2gen4`, autoDeploy off, health path `/__gateway/health`, env vars `[]`, secret files `heard.env` `insight.env` `seorise.env`, domains `api.erdemgunseli.com` + `*.api.erdemgunseli.com` unverified |

The screenshots (`prod-*.png`) were taken with headless Chromium against the live
service; each carries a banner with the real URL, the HTTP status, whether Swagger
loaded its spec, and the capture time. `results.json` is the raw capture log.

Also learned on this deploy and folded into the Dockerfile + `CLAUDE.md` §10: the
first attempt failed because Render's runtime refuses to exec the official Caddy
binary while it carries its `cap_net_bind_service` file capability
(`supervisor: couldn't exec caddy: EPERM`), while all three project processes were
already up. The zero-downtime deploy kept the previous gateway serving throughout.
