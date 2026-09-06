"""gateway - orchestration CLI for onboarding projects and swapping them between
the shared gateway instance and their own standalone Render service.

The risky, stateful steps live here as deterministic, idempotent commands; the
agent skill (skill/SKILL.md) sequences them and makes judgement calls. Render
credentials come from the environment:

    RENDER_API_KEY_GATEWAY     API key for the account hosting the gateway
    RENDER_API_KEY_STANDALONE  API key for the account hosting the standalone svc
                               (may equal the gateway key for single-account setups)

The service name defaults to the manifest's `[gateway] service_name`.

Per the agreed design (#6) both keys are expected to be present in the agent's
context; commands that need a missing key warn and stop rather than half-completing.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
GATEWAY_ROOT = os.path.dirname(HERE)


# ── credential helpers ───────────────────────────────────────────────────────
# Render API keys live in a git-ignored .env outside the repo, one key per
# account, keyed by an arbitrary label:
#     RENDER_API_KEY_PERSONAL=rnd_xxx
#     RENDER_API_KEY_CLIENTB=rnd_yyy
# Override the path with GATEWAY_RENDER_ENV. Real environment variables take
# precedence over the file, so CI can inject keys without it.
RENDER_ENV_PATH = os.environ.get(
    "GATEWAY_RENDER_ENV", os.path.expanduser("~/.config/gateway/render.env")
)
_render_env_loaded = False


def _load_render_env() -> None:
    global _render_env_loaded
    if _render_env_loaded:
        return
    _render_env_loaded = True
    if not os.path.isfile(RENDER_ENV_PATH):
        return
    with open(RENDER_ENV_PATH) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip("'\"")
            # Real env vars win, so a shell export overrides the file:
            os.environ.setdefault(key, val)


def _client(account: str):
    """Build a RenderClient for an account label. Warn-and-stop if its key is absent."""
    from render_api import RenderClient

    _load_render_env()
    env = f"RENDER_API_KEY_{account.upper()}"
    key = os.environ.get(env, "")
    if not key:
        sys.exit(
            f"ERROR: {env} is not set (looked in env and {RENDER_ENV_PATH}). The "
            f"'{account}' Render API key must be available to run this command. "
            f"Aborting (no partial changes)."
        )
    return RenderClient(key, account_label=account)


def _git_remote_url() -> str:
    """HTTPS URL of the gateway repo's origin (used as the Render service repo)."""
    out = subprocess.run(
        ["git", "-C", GATEWAY_ROOT, "remote", "get-url", "origin"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # Normalise git@github.com:owner/repo.git -> https://github.com/owner/repo
    if out.startswith("git@"):
        host, _, path = out.partition(":")
        out = f"https://{host[4:]}/{path}"
    return out[:-4] if out.endswith(".git") else out


# ── manifest editing (tomlkit round-trips comments/formatting) ───────────────
def cmd_add(args) -> None:
    import tomlkit

    path = os.path.join(GATEWAY_ROOT, "gateway.toml")
    doc = tomlkit.parse(open(path).read())
    block = tomlkit.table()
    block["name"] = args.name
    block["subdomain"] = args.subdomain or args.name
    block["path"] = f"projects/{args.name}"
    block["backend_dir"] = args.backend_dir
    block["app"] = args.app
    block["requirements"] = args.requirements
    block["workers"] = args.workers
    block["health_path"] = args.health_path
    block["db"] = args.db
    block["enabled"] = True
    doc.setdefault("project", tomlkit.aot()).append(block)
    open(path, "w").write(tomlkit.dumps(doc))

    if args.repo:
        subprocess.run(
            ["git", "submodule", "add", args.repo, f"projects/{args.name}"],
            cwd=GATEWAY_ROOT, check=True,
        )
    print(f"Added '{args.name}' to manifest. Create its secret file and deploy.")


def cmd_remove(args) -> None:
    import tomlkit

    path = os.path.join(GATEWAY_ROOT, "gateway.toml")
    doc = tomlkit.parse(open(path).read())
    doc["project"] = [p for p in doc.get("project", []) if p.get("name") != args.name]
    open(path, "w").write(tomlkit.dumps(doc))
    subprocess.run(["git", "submodule", "deinit", "-f", f"projects/{args.name}"],
                   cwd=GATEWAY_ROOT, check=False)
    print(f"Removed '{args.name}' from manifest.")


# ── secrets ──────────────────────────────────────────────────────────────────
def cmd_secrets_push(args) -> None:
    """Upload a local <name>.env to the gateway service as a Render Secret File."""
    content = open(args.file).read()
    c = _client(args.account)
    svc = c.find_service(args.service)
    if not svc:
        sys.exit(f"ERROR: gateway service '{args.service}' not found")
    c.upsert_secret_file(svc["id"], f"{args.name}.env", content)
    print(f"Pushed {args.file} -> {args.service}:/etc/secrets/{args.name}.env")


# ── sqlite provisioning ──────────────────────────────────────────────────────
def cmd_provision_sqlite(args) -> None:
    """Build a new SQLite project's schema without replaying Postgres-only history.

    Runs (in the project's own venv) ``metadata.create_all`` to materialise the
    current schema directly, then ``alembic stamp head`` when the project uses
    Alembic — so the first boot doesn't try to replay migrations authored against
    PostgreSQL, while future revisions still apply via ``alembic upgrade head``.
    Idempotent: create_all only adds missing tables.
    """
    os.environ.setdefault("GATEWAY_APP_ROOT", GATEWAY_ROOT)
    sys.path.insert(0, os.path.join(GATEWAY_ROOT, "scripts"))
    import manifest as mf

    cfg = mf.load()
    proj = next((p for p in cfg.projects if p.name == args.name), None)
    if proj is None:
        sys.exit(f"ERROR: project '{args.name}' not found in the manifest (or not enabled)")
    if proj.db != "sqlite":
        sys.exit(f"ERROR: project '{args.name}' is db={proj.db}, not sqlite; nothing to provision")

    venv_py = os.path.join(proj.venv, "bin", "python")
    if not os.path.isfile(venv_py):
        sys.exit(f"ERROR: project venv not found at {venv_py}. Build it first (build_venvs.py / image build).")

    # Derive the import prefix from the ASGI target so this works for both flat
    # ("main:app") and package ("fastapi_backend.main:app") layouts:
    module = proj.app.split(":", 1)[0]
    pkg = module.rpartition(".")[0]
    prefix = f"{pkg}." if pkg else ""
    metadata_ref = args.metadata or f"{prefix}database:Base"
    imports = args.imports or [f"{prefix}models"]

    os.makedirs(os.path.dirname(proj.db_path), exist_ok=True)
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{proj.db_path}"}

    bootstrap = (
        "import importlib, os\n"
        "from sqlalchemy import create_engine\n"
        f"for mod in {imports!r}:\n"
        "    importlib.import_module(mod)\n"
        f"module_name, _, attr = {metadata_ref!r}.partition(':')\n"
        "base = getattr(importlib.import_module(module_name), attr or 'Base')\n"
        "create_engine(os.environ['DATABASE_URL'])  # noqa: validates the URL early\n"
        "base.metadata.create_all(create_engine(os.environ['DATABASE_URL']))\n"
        "print('  create_all complete')\n"
    )
    print(f"Provisioning sqlite schema for '{proj.name}' at {proj.db_path} "
          f"(metadata={metadata_ref}, import={imports}) ...")
    subprocess.run([venv_py, "-c", bootstrap], cwd=proj.backend_dir, env=env, check=True)

    alembic_ini = os.path.join(proj.backend_dir, "alembic.ini")
    if not args.no_stamp and os.path.isfile(alembic_ini):
        alembic = os.path.join(proj.venv, "bin", "alembic")
        subprocess.run([alembic, "-c", alembic_ini, "stamp", "head"], cwd=proj.backend_dir, env=env, check=True)
        print(f"Stamped Alembic head for '{proj.name}'.")
    print(f"Provisioned '{proj.name}'.")


# ── env vars ─────────────────────────────────────────────────────────────────
def cmd_env_unset(args) -> None:
    """Remove service-level env vars. Every process inherits the container env, so
    per-project secrets must live in Secret Files, never here; this is how the old
    shared-blob variables are retired once the projects run from their own files."""
    c = _client(args.account)
    svc = c.find_service(args.service)
    if not svc:
        sys.exit(f"ERROR: service '{args.service}' not found in account '{args.account}'")
    for key in args.keys:
        removed = c.delete_env_var(svc["id"], key)
        print(f"  {key}: {'removed' if removed else 'already absent'}")
    print("Env change takes effect on the next deploy (`gateway deploy --wait`).")


# ── Render lifecycle ─────────────────────────────────────────────────────────
def cmd_suspend(args) -> None:
    c = _client(args.account)
    svc = c.find_service(args.service)
    if not svc:
        sys.exit(f"ERROR: service '{args.service}' not found")
    c.suspend(svc["id"]); print(f"Suspended {args.service}")


def cmd_resume(args) -> None:
    c = _client(args.account)
    svc = c.find_service(args.service)
    if not svc:
        sys.exit(f"ERROR: service '{args.service}' not found")
    c.resume(svc["id"]); print(f"Resumed {args.service}")


def cmd_deploy(args) -> None:
    c = _client(args.account)
    svc = c.find_service(args.service)
    if not svc:
        sys.exit(f"ERROR: service '{args.service}' not found in account '{args.account}'")
    dep = c.trigger_deploy(svc["id"], clear_cache=args.clear_cache)
    print(f"Triggered deploy of {args.service} ({dep.get('id', '?')})")
    if args.wait:
        status = c.wait_deploy(svc["id"], dep["id"])
        print(f"Deploy {status.upper()}")
        sys.exit(0 if status in {"live"} else 1)


# ── health polling ───────────────────────────────────────────────────────────
def _wait_healthy(url: str, timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


def cmd_wait(args) -> None:
    ok = _wait_healthy(args.url, args.timeout)
    print(f"{'HEALTHY' if ok else 'TIMEOUT'}: {args.url}")
    sys.exit(0 if ok else 1)


# ── manifest access ──────────────────────────────────────────────────────────
def _manifest():
    os.environ.setdefault("GATEWAY_APP_ROOT", GATEWAY_ROOT)
    sys.path.insert(0, os.path.join(GATEWAY_ROOT, "scripts"))
    import manifest as mf
    return mf.load()


def _gateway_health_url(cfg) -> str:
    return f"https://{cfg.base_domain}/__gateway/health"


def _default_service() -> str:
    """The Render service name: manifest `[gateway] service_name`, else 'gateway'."""
    try:
        return _manifest().service_name
    except Exception:
        return "gateway"


# ── provision / deploy the gateway service ───────────────────────────────────
def cmd_up(args) -> None:
    """Idempotently provision and deploy the gateway service on Render.

    Creates the Docker web service (with the /data disk inline) if it doesn't
    exist, pushes any local Secret Files, registers the wildcard custom domain
    and its parent, then triggers a deploy and waits for it to go live. Safe to
    re-run: existing service / domains are detected and left in place.
    """
    cfg = _manifest()
    c = _client(args.account)

    svc = c.find_service(args.service)
    if svc:
        print(f"Service '{args.service}' already exists ({svc['id']}).")
        # Reconcile the fields the gateway depends on, so re-running `up` after a
        # branch change or on a service created by hand converges on the manifest.
        details = svc.get("serviceDetails", {})
        patch: dict = {}
        if svc.get("branch") != args.branch:
            patch["branch"] = args.branch
        if svc.get("autoDeploy") != "no":
            patch["autoDeploy"] = "no"      # deploys are explicit (render.yaml: autoDeploy false)
        if details.get("healthCheckPath") != "/__gateway/health":
            patch["serviceDetails"] = {"healthCheckPath": "/__gateway/health"}
        if patch:
            print(f"  reconciling {sorted(patch)} -> {patch}")
            svc = c.update_service(svc["id"], **patch)
        else:
            print("  branch / autoDeploy / healthCheckPath already match")
    else:
        owner_id = c.resolve_owner_id(args.owner)
        repo = args.repo or _git_remote_url()
        print(f"Creating service '{args.service}' from {repo}@{args.branch} in owner {owner_id} ...")
        svc = c.create_service(
            name=args.service,
            owner_id=owner_id,
            repo=repo,
            branch=args.branch,
            plan=args.plan,
            region=args.region,
            health_check_path="/__gateway/health",
            disk=None if args.no_disk else {"name": "gateway-data", "mountPath": cfg.data_dir, "sizeGB": args.disk_gb},
        )
        print(f"Created service {svc['id']}.")
    sid = svc["id"]

    # Secret Files: push any local <secrets_dir>/<name>.env for enabled projects.
    pushed = 0
    for p in cfg.projects:
        local = os.path.join(args.secrets_dir, f"{p.name}.env")
        if os.path.isfile(local):
            c.upsert_secret_file(sid, f"{p.name}.env", open(local).read())
            print(f"  pushed secret file {p.name}.env")
            pushed += 1
        else:
            print(f"  (no local secret file for {p.name} at {local}; skipping)")
    print(f"Secret files pushed: {pushed}/{len(cfg.projects)}")

    # Custom domains: the wildcard + its parent cover every <name>.<base_domain>.
    existing = {d.get("name") for d in c.list_custom_domains(sid)}
    for domain in (cfg.base_domain, f"*.{cfg.base_domain}"):
        if domain in existing:
            print(f"  domain {domain} already registered")
        else:
            c.add_custom_domain(sid, domain)
            print(f"  added custom domain {domain} (configure DNS, then it auto-verifies)")

    if args.no_deploy:
        print("Skipping deploy (--no-deploy). Service is provisioned.")
        return
    dep = c.trigger_deploy(sid, clear_cache=args.clear_cache)
    print(f"Triggered deploy {dep.get('id', '?')}; waiting for it to go live ...")
    status = c.wait_deploy(sid, dep["id"])
    print(f"Deploy {status.upper()}")
    if status not in {"live"}:
        sys.exit(1)
    print(f"\nGateway is live. Health: {_gateway_health_url(cfg)}")
    print("Per-project URLs:")
    for p in cfg.projects:
        print(f"  https://{p.hostname}{p.health_path}")


def cmd_status(args) -> None:
    cfg = _manifest()
    c = _client(args.account)
    svc = c.find_service(args.service)
    if not svc:
        sys.exit(f"ERROR: service '{args.service}' not found in account '{args.account}'")
    sid = svc["id"]
    details = svc.get("serviceDetails", {})
    print(f"service : {args.service}  ({sid})")
    print(f"state   : {svc.get('suspended', 'n/a')}  plan={details.get('plan', '?')}  region={details.get('region', '?')}")
    print(f"source  : {svc.get('repo', '?')}@{svc.get('branch', '?')}  autoDeploy={svc.get('autoDeploy', '?')}  health={details.get('healthCheckPath') or '(none)'}")
    print(f"url     : {details.get('url', '?')}")
    print(f"env vars: {sorted(e.get('key') for e in c.list_env_vars(sid))}")
    print("domains :")
    for d in c.list_custom_domains(sid):
        print(f"  {d.get('name'):40s} {d.get('verificationStatus', '?')}")
    print(f"secrets : {sorted(f.get('name') for f in c.list_secret_files(sid))}")
    print(f"health  : {_gateway_health_url(cfg)}")


def cmd_domains(args) -> None:
    c = _client(args.account)
    svc = c.find_service(args.service)
    if not svc:
        sys.exit(f"ERROR: service '{args.service}' not found in account '{args.account}'")
    sid = svc["id"]
    if args.add:
        c.add_custom_domain(sid, args.add); print(f"Added {args.add}")
    if args.remove:
        c.delete_custom_domain(sid, args.remove); print(f"Removed {args.remove}")
    if args.verify:
        c.verify_custom_domain(sid, args.verify); print(f"Verification triggered for {args.verify}")
    for d in c.list_custom_domains(sid):
        print(f"  {d.get('name'):40s} {d.get('domainType', '?'):10s} {d.get('verificationStatus', '?')}")


# ── cross-account migration (render ssh + scp data move) ──────────────────────
def _render_ssh(account_key: str, service_id: str, remote_cmd: str, *, capture: bool = False,
                stdin_path: str | None = None):
    """Run a command on a service over `render ssh`, scoped to one account's key.

    Requires the Render CLI (`render`) and SSH access (paid web services). The
    RENDER_API_KEY env var selects the account/workspace for the invocation.
    """
    env = {**os.environ, "RENDER_API_KEY": account_key}
    cmd = ["render", "ssh", service_id, "--", "sh", "-lc", remote_cmd]
    stdin = open(stdin_path, "rb") if stdin_path else None
    try:
        return subprocess.run(
            cmd, env=env, check=True,
            stdin=stdin,
            stdout=subprocess.PIPE if capture else None,
        )
    finally:
        if stdin:
            stdin.close()


def cmd_migrate(args) -> None:
    """Move the deployed gateway (service + /data + domains) between Render accounts.

    Render can't transfer a service, so this recreates it in the destination,
    copies the SQLite data over `render ssh`, and cuts the wildcard domain over.
    Designed to be run in stages (--stage) so a migration is resumable and has
    explicit rollback points; the data legs need the Render CLI + SSH configured.
    """
    cfg = _manifest()
    src = _client(args.src)
    dst = _client(args.dst)
    src_svc = src.find_service(args.service)
    if not src_svc:
        sys.exit(f"ERROR: source service '{args.service}' not found in account '{args.src}'")
    src_key = os.environ[f"RENDER_API_KEY_{args.src.upper()}"]
    dst_key = os.environ[f"RENDER_API_KEY_{args.dst.upper()}"]
    stages = args.stage or ["provision", "secrets", "data", "domains", "finalize"]

    print(f"Migrating '{args.service}': {args.src} -> {args.dst}  (stages: {', '.join(stages)})")

    dst_svc = dst.find_service(args.service)
    if "provision" in stages:
        if dst_svc:
            print(f"[provision] dest service already exists ({dst_svc['id']}).")
        else:
            owner_id = dst.resolve_owner_id(args.owner)
            repo = args.repo or _git_remote_url()
            print(f"[provision] creating dest service from {repo}@{args.branch} ...")
            dst_svc = dst.create_service(
                name=args.service, owner_id=owner_id, repo=repo, branch=args.branch,
                plan=args.plan, region=args.region, health_check_path="/__gateway/health",
                disk={"name": "gateway-data", "mountPath": cfg.data_dir, "sizeGB": args.disk_gb},
            )
            dep = dst.trigger_deploy(dst_svc["id"])
            print(f"[provision] waiting for first deploy {dep.get('id','?')} ...")
            if dst.wait_deploy(dst_svc["id"], dep["id"]) not in {"live"}:
                sys.exit("[provision] dest first deploy failed; aborting before any data move")
    if not dst_svc:
        sys.exit("ERROR: dest service does not exist; run the 'provision' stage first")

    if "secrets" in stages:
        names = [f.get("name") for f in src.list_secret_files(src_svc["id"])]
        for name in names:
            content = src.get_secret_file(src_svc["id"], name)
            if content is not None:
                dst.upsert_secret_file(dst_svc["id"], name, content)
                print(f"[secrets] copied {name}")
        print(f"[secrets] copied {len(names)} secret file(s)")

    if "data" in stages:
        if not args.skip_quiesce:
            print(f"[data] suspending SOURCE to quiesce writers for a clean copy ...")
            src.suspend(src_svc["id"])
            # NOTE: suspend stops the instance; ssh needs it running. For a clean
            # SQLite copy we instead checkpoint+VACUUM while live, then resume is
            # unnecessary. If you prefer a stop-window, copy before suspend.
            src.resume(src_svc["id"])
        os.makedirs(args.workdir, exist_ok=True)
        tar = os.path.join(args.workdir, f"{args.service}-data.tgz")
        data_dir = cfg.data_dir
        # Make a consistent snapshot of every sqlite db, then stream it down.
        snap = (
            f"set -e; rm -rf {data_dir}/_mig && mkdir -p {data_dir}/_mig; "
            f"for f in {data_dir}/*.db; do [ -e \"$f\" ] || continue; "
            f"sqlite3 \"$f\" \"PRAGMA wal_checkpoint(TRUNCATE);\" >/dev/null 2>&1 || true; "
            f"sqlite3 \"$f\" \"VACUUM INTO '{data_dir}/_mig/$(basename \"$f\")'\"; done; "
            f"tar czf - -C {data_dir}/_mig . ; rm -rf {data_dir}/_mig"
        )
        print(f"[data] snapshotting + downloading source /data -> {tar}")
        res = _render_ssh(src_key, src_svc["id"], snap, capture=True)
        with open(tar, "wb") as fh:
            fh.write(res.stdout)
        print(f"[data] uploading -> dest /data ({os.path.getsize(tar)} bytes)")
        restore = f"mkdir -p {data_dir} && tar xzf - -C {data_dir}"
        _render_ssh(dst_key, dst_svc["id"], restore, stdin_path=tar)
        print("[data] done")

    if "domains" in stages:
        # A domain lives on one service at a time: remove from source first.
        for domain in (f"*.{cfg.base_domain}", cfg.base_domain):
            try:
                src.delete_custom_domain(src_svc["id"], domain)
                print(f"[domains] removed {domain} from source")
            except Exception as e:
                print(f"[domains] source remove {domain}: {e} (continuing)")
            dst.add_custom_domain(dst_svc["id"], domain)
            print(f"[domains] added {domain} to dest")
        print("[domains] ACTION REQUIRED: point DNS (CNAME *.api + parent, and "
              "_acme-challenge) at the DEST service, then run: "
              f"gateway domains {args.service} --account {args.dst} --verify '*.{cfg.base_domain}'")

    if "finalize" in stages:
        if not _wait_healthy(_gateway_health_url(cfg), args.timeout):
            sys.exit(f"[finalize] dest not healthy at {_gateway_health_url(cfg)} within {args.timeout}s; "
                     "NOT suspending source (investigate / roll back DNS)")
        print("[finalize] dest healthy; suspending source (kept for rollback, not deleted)")
        src.suspend(src_svc["id"])
        print("[finalize] migration complete. Delete the source service manually once confident.")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gateway")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="register a project in the manifest (+submodule)")
    a.add_argument("name"); a.add_argument("--repo"); a.add_argument("--subdomain")
    a.add_argument("--backend-dir", default="fastapi_backend")
    a.add_argument("--app", default="main:app")
    a.add_argument("--requirements", default="requirements.txt")
    a.add_argument("--workers", type=int, default=1)
    a.add_argument("--health-path", default="/healthz")
    a.add_argument("--db", choices=["sqlite", "external"], default="sqlite")
    a.set_defaults(func=cmd_add)

    r = sub.add_parser("remove", help="remove a project from the manifest")
    r.add_argument("name"); r.set_defaults(func=cmd_remove)

    s = sub.add_parser("secrets-push", help="upload a local .env as a Render secret file")
    s.add_argument("name"); s.add_argument("--file", required=True)
    s.add_argument("--service", default=_default_service())
    s.add_argument("--account", default="gateway", help="account label from render.env")
    s.set_defaults(func=cmd_secrets_push)

    u = sub.add_parser("up", help="provision + deploy the gateway service on Render (idempotent)")
    u.add_argument("--account", default="gateway", help="account label from render.env")
    u.add_argument("--service", default=_default_service())
    u.add_argument("--owner", default="", help="workspace name/email/id (if the key reaches several)")
    u.add_argument("--repo", default="", help="git repo URL (default: this repo's origin)")
    u.add_argument("--branch", default="main")
    u.add_argument("--plan", default="starter")
    u.add_argument("--region", default="oregon")
    u.add_argument("--disk-gb", type=int, default=1)
    u.add_argument("--no-disk", action="store_true",
                   help="create the service without a persistent disk (no sqlite projects)")
    u.add_argument("--secrets-dir", default=os.path.join(GATEWAY_ROOT, "secrets"),
                   help="local dir holding <project>.env Secret Files to push")
    u.add_argument("--clear-cache", action="store_true")
    u.add_argument("--no-deploy", action="store_true", help="provision only, don't deploy")
    u.set_defaults(func=cmd_up)

    st = sub.add_parser("status", help="show the gateway service's state, domains and secrets")
    st.add_argument("--account", default="gateway"); st.add_argument("--service", default=_default_service())
    st.set_defaults(func=cmd_status)

    dm = sub.add_parser("domains", help="list/add/remove/verify a service's custom domains")
    dm.add_argument("service", nargs="?", default=_default_service())
    dm.add_argument("--account", default="gateway")
    dm.add_argument("--add"); dm.add_argument("--remove"); dm.add_argument("--verify")
    dm.set_defaults(func=cmd_domains)

    mg = sub.add_parser("migrate", help="move the gateway between Render accounts (service + data + domains)")
    mg.add_argument("--src", required=True, help="source account label from render.env")
    mg.add_argument("--dst", required=True, help="destination account label from render.env")
    mg.add_argument("--service", default=_default_service())
    mg.add_argument("--owner", default="", help="dest workspace name/email/id (if ambiguous)")
    mg.add_argument("--repo", default=""); mg.add_argument("--branch", default="main")
    mg.add_argument("--plan", default="starter"); mg.add_argument("--region", default="oregon")
    mg.add_argument("--disk-gb", type=int, default=1)
    mg.add_argument("--workdir", default="/tmp/gateway-migrate", help="local scratch dir for the data tarball")
    mg.add_argument("--stage", action="append",
                    choices=["provision", "secrets", "data", "domains", "finalize"],
                    help="run only these stages (repeatable; default: all in order)")
    mg.add_argument("--skip-quiesce", action="store_true",
                    help="don't briefly cycle the source before the data copy")
    mg.add_argument("--timeout", type=int, default=300, help="finalize health-wait seconds")
    mg.set_defaults(func=cmd_migrate)

    ps = sub.add_parser("provision-sqlite",
                        help="create a new sqlite project's schema (create_all + alembic stamp head)")
    ps.add_argument("name")
    ps.add_argument("--metadata", default="",
                    help="module:attr exposing the declarative Base (default: derived from the app target)")
    ps.add_argument("--import", dest="imports", action="append", default=[],
                    help="model module to import so its tables register (repeatable; default: '<pkg>models')")
    ps.add_argument("--no-stamp", action="store_true",
                    help="skip 'alembic stamp head' even if alembic.ini exists")
    ps.set_defaults(func=cmd_provision_sqlite)

    eu = sub.add_parser("env-unset", help="remove service-level env vars (per-project config belongs in Secret Files)")
    eu.add_argument("keys", nargs="+")
    eu.add_argument("--service", default=_default_service())
    eu.add_argument("--account", default="gateway", help="account label from render.env")
    eu.set_defaults(func=cmd_env_unset)

    for verb, fn in (("suspend", cmd_suspend), ("resume", cmd_resume)):
        p = sub.add_parser(verb, help=f"{verb} a Render service")
        p.add_argument("service")
        p.add_argument("--account", default="standalone", help="account label from render.env")
        p.set_defaults(func=fn)

    d = sub.add_parser("deploy", help="trigger a gateway redeploy")
    d.add_argument("--service", default=_default_service())
    d.add_argument("--account", default="gateway", help="account label from render.env")
    d.add_argument("--wait", action="store_true", help="wait for the deploy to go live")
    d.add_argument("--clear-cache", action="store_true"); d.set_defaults(func=cmd_deploy)

    w = sub.add_parser("wait", help="poll a health URL until 200")
    w.add_argument("url"); w.add_argument("--timeout", type=int, default=120)
    w.set_defaults(func=cmd_wait)
    return ap


def main() -> None:
    sys.path.insert(0, HERE)  # so render_api / tomlkit resolve
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
