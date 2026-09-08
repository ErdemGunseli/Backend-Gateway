"""render-ops — manage Render services across any number of accounts, and migrate
any service (with its disk data, secret files, env and custom domains) from one
account to another.

Accounts are selected by label. API keys come from the environment as
RENDER_API_KEY_<LABEL>; the bundled scripts/render.sh sources them from an external
git-ignored file (default ~/.config/render/accounts.env) so the keys never enter the
agent's context. This module also loads that file directly, so it works when run as
`python render_ops.py ...` too.

Render cannot transfer a service between accounts, so `migrate` recreates it in the
destination from the source's own spec, copies its data over `render ssh`, and cuts
its custom domains over. See ../references/migration.md for the full runbook.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_ENV = os.environ.get(
    "RENDER_ACCOUNTS_ENV", os.path.expanduser("~/.config/render/accounts.env")
)


# ── account credentials ──────────────────────────────────────────────────────
def _load_accounts() -> None:
    """Merge RENDER_API_KEY_* from the external file into the env (env wins)."""
    if not os.path.isfile(ACCOUNTS_ENV):
        return
    with open(ACCOUNTS_ENV) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip("'\""))


def _labels() -> list[str]:
    _load_accounts()
    return sorted(
        k[len("RENDER_API_KEY_"):].lower()
        for k in os.environ
        if k.startswith("RENDER_API_KEY_") and os.environ[k]
    )


def _key(account: str) -> str:
    _load_accounts()
    key = os.environ.get(f"RENDER_API_KEY_{account.upper()}", "")
    if not key:
        sys.exit(
            f"ERROR: RENDER_API_KEY_{account.upper()} is not set (looked in env and "
            f"{ACCOUNTS_ENV}). Known accounts: {_labels() or '(none)'}. Aborting."
        )
    return key


def _client(account: str):
    sys.path.insert(0, HERE)
    from render_api import RenderClient
    return RenderClient(_key(account), account_label=account)


def _service(c, name: str, account: str) -> dict:
    svc = c.find_service(name)
    if not svc:
        sys.exit(f"ERROR: service '{name}' not found in account '{account}'")
    return svc


# ── read-only commands ───────────────────────────────────────────────────────
def cmd_accounts(args) -> None:
    labels = _labels()
    if not labels:
        sys.exit(f"No accounts found. Create {ACCOUNTS_ENV} with RENDER_API_KEY_<LABEL>=... lines.")
    for label in labels:
        line = label
        if args.check:
            try:
                owners = _client(label).list_owners()
                line += "  ✓  " + ", ".join(f"{o.get('name')}({o.get('type')})" for o in owners)
            except Exception as e:  # noqa: BLE001 — surface auth/connectivity issues plainly
                line += f"  ✗  {e}"
        print(line)


def cmd_services(args) -> None:
    c = _client(args.account)
    for s in c.list_services(type=args.type):
        sd = s.get("serviceDetails", {})
        print(f"{s.get('name'):30s} {s.get('type',''):16s} {sd.get('plan',''):12s} {s.get('id','')}")


def cmd_status(args) -> None:
    c = _client(args.account)
    s = _service(c, args.service, args.account)
    sid = s["id"]
    sd = s.get("serviceDetails", {})
    print(f"service : {s.get('name')}  ({sid})  type={s.get('type')}")
    print(f"plan    : {sd.get('plan')}  region={sd.get('region')}  url={sd.get('url')}")
    print(f"suspend : {s.get('suspended')}")
    disk = c.service_disk(s)
    print(f"disk    : {disk if disk else '(none)'}")
    print("domains :")
    for d in c.list_custom_domains(sid):
        print(f"  {d.get('name'):40s} {d.get('verificationStatus', '?')}")
    print(f"secrets : {sorted(f.get('name') for f in c.list_secret_files(sid))}")


def cmd_domains(args) -> None:
    c = _client(args.account)
    sid = _service(c, args.service, args.account)["id"]
    if args.add:
        c.add_custom_domain(sid, args.add); print(f"added {args.add}")
    if args.remove:
        c.delete_custom_domain(sid, args.remove); print(f"removed {args.remove}")
    if args.verify:
        c.verify_custom_domain(sid, args.verify); print(f"verification triggered for {args.verify}")
    for d in c.list_custom_domains(sid):
        print(f"  {d.get('name'):40s} {d.get('domainType','?'):10s} {d.get('verificationStatus','?')}")


def cmd_deploy(args) -> None:
    c = _client(args.account)
    sid = _service(c, args.service, args.account)["id"]
    dep = c.trigger_deploy(sid, clear_cache=args.clear_cache)
    print(f"triggered deploy {dep.get('id','?')}")
    if args.wait:
        status = c.wait_deploy(sid, dep["id"])
        print(f"deploy {status.upper()}")
        sys.exit(0 if status in {"live"} else 1)


def cmd_suspend(args) -> None:
    c = _client(args.account)
    c.suspend(_service(c, args.service, args.account)["id"]); print(f"suspended {args.service}")


def cmd_resume(args) -> None:
    c = _client(args.account)
    c.resume(_service(c, args.service, args.account)["id"]); print(f"resumed {args.service}")


# ── migration ────────────────────────────────────────────────────────────────
def _render_ssh(account_key: str, service_id: str, remote_cmd: str, *,
                capture: bool = False, stdin_path: str | None = None):
    """Run a shell command on a service over `render ssh`, scoped to one account.

    Requires the Render CLI and SSH access (paid services). RENDER_API_KEY selects
    the account for the invocation; render ssh must hit the RUNNING service (not
    --ephemeral, which has no disk).
    """
    env = {**os.environ, "RENDER_API_KEY": account_key}
    cmd = ["render", "ssh", service_id, "--", "sh", "-lc", remote_cmd]
    stdin = open(stdin_path, "rb") if stdin_path else None
    try:
        return subprocess.run(cmd, env=env, check=True, stdin=stdin,
                              stdout=subprocess.PIPE if capture else None)
    finally:
        if stdin:
            stdin.close()


def _wait_healthy(url: str, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001 — polling, any error means "not yet"
            pass
        time.sleep(3)
    return False


def cmd_migrate(args) -> None:
    src = _client(args.src)
    dst = _client(args.dst)
    src_key, dst_key = _key(args.src), _key(args.dst)
    src_svc = _service(src, args.service, args.src)
    stages = args.stage or ["provision", "env", "secrets", "data", "domains", "finalize"]
    print(f"migrate '{args.service}': {args.src} -> {args.dst}  (stages: {', '.join(stages)})")

    dst_svc = dst.find_service(args.service)
    if "provision" in stages:
        if dst_svc:
            print(f"[provision] dest service already exists ({dst_svc['id']})")
        else:
            owner_id = dst.resolve_owner_id(args.owner)
            kwargs = src.clone_kwargs(src_svc, owner_id=owner_id)
            if args.repo:
                kwargs["repo"] = args.repo
            if args.branch:
                kwargs["branch"] = args.branch
            if args.plan:
                kwargs["service_details"]["plan"] = args.plan
            if args.region:
                kwargs["service_details"]["region"] = args.region
            print(f"[provision] cloning spec -> dest ({kwargs['type']}, plan="
                  f"{kwargs['service_details'].get('plan')}, disk="
                  f"{kwargs['service_details'].get('disk')})")
            dst_svc = dst.create_service(**kwargs)
            dep = dst.trigger_deploy(dst_svc["id"])
            print(f"[provision] first deploy {dep.get('id','?')} ...")
            if dst.wait_deploy(dst_svc["id"], dep["id"]) not in {"live"}:
                sys.exit("[provision] dest first deploy failed; aborting before any data move")
    if not dst_svc:
        sys.exit("ERROR: dest service does not exist; run the 'provision' stage first")

    if "env" in stages:
        pairs = [{"key": e["key"], "value": e["value"]} for e in src.list_env_vars(src_svc["id"])]
        if pairs:
            dst.replace_env_vars(dst_svc["id"], pairs)
        print(f"[env] copied {len(pairs)} env var(s)")

    if "secrets" in stages:
        n = 0
        for f in src.list_secret_files(src_svc["id"]):
            content = src.get_secret_file(src_svc["id"], f["name"])
            if content is not None:
                dst.upsert_secret_file(dst_svc["id"], f["name"], content); n += 1
        print(f"[secrets] copied {n} secret file(s)")

    if "data" in stages:
        disk = src.service_disk(src_svc)
        if not disk:
            print("[data] source has no disk; nothing to copy")
        else:
            mount = disk["mountPath"]
            os.makedirs(args.workdir, exist_ok=True)
            tar = os.path.join(args.workdir, f"{args.service}-data.tgz")
            if args.no_vacuum:
                snap = f"tar czf - -C {mount} ."
            else:
                # SQLite-safe: checkpoint + VACUUM INTO each *.db, tar the snapshots
                # plus any non-db files. Plain tar for everything else.
                snap = (
                    f"set -e; rm -rf {mount}/_mig && mkdir -p {mount}/_mig; "
                    f"for f in {mount}/*.db; do [ -e \"$f\" ] || continue; "
                    f"sqlite3 \"$f\" 'PRAGMA wal_checkpoint(TRUNCATE);' >/dev/null 2>&1 || true; "
                    f"sqlite3 \"$f\" \"VACUUM INTO '{mount}/_mig/$(basename \"$f\")'\"; done; "
                    f"tar czf - --exclude=_mig -C {mount} . -C {mount}/_mig . ; rm -rf {mount}/_mig"
                )
            print(f"[data] snapshot+download {mount} from source -> {tar}")
            res = _render_ssh(src_key, src_svc["id"], snap, capture=True)
            with open(tar, "wb") as fh:
                fh.write(res.stdout)
            print(f"[data] upload -> dest {mount} ({os.path.getsize(tar)} bytes)")
            _render_ssh(dst_key, dst_svc["id"], f"mkdir -p {mount} && tar xzf - -C {mount}",
                        stdin_path=tar)
            print("[data] done")

    if "domains" in stages:
        domains = args.domain or [d.get("name") for d in src.list_custom_domains(src_svc["id"])]
        for name in domains:
            try:
                src.delete_custom_domain(src_svc["id"], name)
                print(f"[domains] removed {name} from source")
            except Exception as e:  # noqa: BLE001
                print(f"[domains] source remove {name}: {e} (continuing)")
            dst.add_custom_domain(dst_svc["id"], name)
            print(f"[domains] added {name} to dest")
        if domains:
            print("[domains] ACTION REQUIRED: update DNS to the DEST service (CNAME + "
                  "_acme-challenge), then verify with: render-ops domains "
                  f"{args.dst} {args.service} --verify <domain>")

    if "finalize" in stages:
        url = args.health_url or (dst.get_service(dst_svc["id"]).get("serviceDetails", {}) or {}).get("url")
        if url and not _wait_healthy(url, args.timeout):
            sys.exit(f"[finalize] dest not healthy at {url} within {args.timeout}s; "
                     "NOT suspending source (investigate / roll back DNS)")
        print(f"[finalize] dest healthy ({url or 'no url checked'}); suspending source (rollback-safe)")
        src.suspend(src_svc["id"])
        print("[finalize] migration complete. Delete the source service manually once confident.")


# ── parser ───────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="render-ops")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("accounts", help="list configured account labels")
    a.add_argument("--check", action="store_true", help="validate each key against the API")
    a.set_defaults(func=cmd_accounts)

    sv = sub.add_parser("services", help="list services in an account")
    sv.add_argument("account"); sv.add_argument("--type", help="filter: web_service|background_worker|cron_job|...")
    sv.set_defaults(func=cmd_services)

    st = sub.add_parser("status", help="show one service's state, disk, domains, secrets")
    st.add_argument("account"); st.add_argument("service"); st.set_defaults(func=cmd_status)

    dm = sub.add_parser("domains", help="list/add/remove/verify a service's custom domains")
    dm.add_argument("account"); dm.add_argument("service")
    dm.add_argument("--add"); dm.add_argument("--remove"); dm.add_argument("--verify")
    dm.set_defaults(func=cmd_domains)

    dp = sub.add_parser("deploy", help="trigger a deploy")
    dp.add_argument("account"); dp.add_argument("service")
    dp.add_argument("--wait", action="store_true"); dp.add_argument("--clear-cache", action="store_true")
    dp.set_defaults(func=cmd_deploy)

    for verb, fn in (("suspend", cmd_suspend), ("resume", cmd_resume)):
        p = sub.add_parser(verb, help=f"{verb} a service")
        p.add_argument("account"); p.add_argument("service"); p.set_defaults(func=fn)

    mg = sub.add_parser("migrate", help="move a service (data + secrets + env + domains) between accounts")
    mg.add_argument("--service", required=True)
    mg.add_argument("--src", required=True, help="source account label")
    mg.add_argument("--dst", required=True, help="destination account label")
    mg.add_argument("--owner", default="", help="dest workspace name/email/id (if ambiguous)")
    mg.add_argument("--repo", default=""); mg.add_argument("--branch", default="")
    mg.add_argument("--plan", default=""); mg.add_argument("--region", default="")
    mg.add_argument("--domain", action="append", help="domain(s) to move (default: all on source)")
    mg.add_argument("--workdir", default="/tmp/render-migrate")
    mg.add_argument("--no-vacuum", action="store_true",
                    help="plain tar of the disk instead of sqlite VACUUM INTO snapshots")
    mg.add_argument("--health-url", default="", help="URL to poll in finalize (default: dest onrender url)")
    mg.add_argument("--timeout", type=int, default=300)
    mg.add_argument("--stage", action="append",
                    choices=["provision", "env", "secrets", "data", "domains", "finalize"],
                    help="run only these stages (repeatable; default: all in order)")
    mg.set_defaults(func=cmd_migrate)
    return ap


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
