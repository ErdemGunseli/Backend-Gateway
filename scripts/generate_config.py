"""Render the Caddyfile, supervisord program file, and per-project runspecs.

Run at image build and again at container boot (idempotent) so the live config
always matches the manifest. Everything downstream - routing, process
supervision, env/secret injection - is derived from `gateway.toml` here.

Routing has two layers, both generated from the manifest:

1. **Host routing** (the design): each project owns `<subdomain>.<base_domain>`
   plus any `extra_hosts`. One Caddy site block per project.
2. **Path-prefix routing** on every other Host - the service's own
   `*.onrender.com` address before the custom domains resolve, and legacy
   aliases (`/in-sight`, `/seo-rise`) that shipped clients still call. The prefix
   is stripped before proxying, so the app sees the same paths either way.
   FastAPI's `/docs` fetches `/openapi.json` from the host root, so under a prefix
   that request is routed by its Referer - a browser-only convenience for the
   interactive docs; API clients never depend on it.
"""

from __future__ import annotations

import os
import posixpath
import re

import manifest as m


def _proxy(p: m.Project) -> str:
    return f"reverse_proxy 127.0.0.1:{p.port}"


def _health(indent: str) -> list[str]:
    # Gateway liveness. Emitted in EVERY site block, not just the catch-all:
    # Render's health check carries a Host header of its own choosing, and when
    # that is one of the project hostnames the request lands in that project's
    # site block - where the app has no such route and answers 404, failing the
    # deploy while the gateway is in fact healthy (measured 2026-09-08).
    return [
        f"{indent}# Gateway liveness - Caddy answers this on every Host.",
        f"{indent}@health path /__gateway/health",
        f"{indent}handle @health {{",
        f'{indent}\trespond "ok" 200',
        f"{indent}}}",
    ]


def _caddyfile(cfg: m.GatewayConfig) -> str:
    # Render terminates TLS at its edge and forwards plain HTTP to $PORT, so Caddy
    # serves HTTP only and routes purely by Host header. $PORT is read at runtime.
    # Only Render's edge can reach $PORT (the container port is not public), so its
    # X-Forwarded-* headers are trusted from any source and passed through - apps
    # then see the real client IP and `https` (OAuth redirect URIs, secure cookies).
    port = "{$PORT:10000}"
    lines = [
        "{",
        "\tadmin off",
        "\tauto_https off",
        "\tservers {",
        "\t\ttrusted_proxies static 0.0.0.0/0 ::/0",
        "\t}",
        "}",
        "",
    ]

    # 1. Host routing: one site block per project, matched before the catch-all.
    for p in cfg.projects:
        addresses = ", ".join(f"http://{h}:{port}" for h in p.hosts)
        lines += [
            f"# {p.name}: Host routing",
            f"{addresses} {{",
            *_health("\t"),
            "\thandle {",
            f"\t\t{_proxy(p)}",
            "\t}",
            "}",
            "",
        ]

    # 2. Catch-all: gateway health, path-prefix routing, explicit 404.
    lines += [
        "# Any other Host: gateway health + path-prefix routing",
        f"http://:{port} {{",
        *_health("\t"),
        "",
    ]
    for p in cfg.projects:
        if not p.path_prefixes:
            continue
        lines.append(f"\t# {p.name}: path-prefix routing (prefix stripped)")
        for i, prefix in enumerate(p.path_prefixes):
            tag = f"{p.name}_{i}"
            # Bare prefix -> trailing slash, so relative links in the app resolve.
            lines += [
                f"\t@{tag}_bare path {prefix}",
                f"\tredir @{tag}_bare {prefix}/ 308",
                f"\thandle_path {prefix}/* {{",
                f"\t\t{_proxy(p)}",
                "\t}",
            ]
            # FastAPI's docs page requests /openapi.json at the host root; route it
            # back to the project whose docs page the browser is on.
            lines += [
                f"\t@{tag}_spec {{",
                "\t\tpath /openapi.json",
                f"\t\theader_regexp Referer ^https?://[^/]+{re.escape(prefix)}(/|$)",
                "\t}",
                f"\thandle @{tag}_spec {{",
                f"\t\t{_proxy(p)}",
                "\t}",
            ]
        lines.append("")
    lines += [
        "\t# Unknown host / path -> explicit 404 rather than a misrouted response.",
        "\thandle {",
        "\t\trespond \"gateway: unknown host\" 404",
        "\t}",
        "}",
        "",
    ]
    return "\n".join(lines)


def _supervisord(cfg: m.GatewayConfig) -> str:
    blocks = [
        "[supervisord]",
        "nodaemon=true",
        "user=root",
        "logfile=/dev/null",
        "logfile_maxbytes=0",
        "pidfile=/tmp/supervisord.pid",
        "",
        "[program:caddy]",
        f"command=caddy run --config {posixpath.join(m.GEN_DIR, 'Caddyfile')} --adapter caddyfile",
        "autorestart=true",
        "startretries=10",
        "priority=10",
        "stdout_logfile=/dev/fd/1",
        "stdout_logfile_maxbytes=0",
        "redirect_stderr=true",
        "",
    ]
    for p in cfg.projects:
        # One isolated process per project. autorestart gives startup isolation:
        # a project that crashes (e.g. a bad migration) restarts on its own and
        # never takes down Caddy or its neighbours.
        blocks += [
            f"[program:{p.name}]",
            f"command={posixpath.join(m.APP_ROOT, 'scripts', 'launch.sh')} {p.name}",
            "autorestart=true",
            "startretries=3",
            "stopwaitsecs=15",
            "priority=20",
            "stdout_logfile=/dev/fd/1",
            "stdout_logfile_maxbytes=0",
            "redirect_stderr=true",
            "",
        ]
    return "\n".join(blocks)


def _runspec(p: m.Project) -> str:
    # Non-secret runtime parameters consumed by launch.sh. Secrets are NOT written
    # here - they come only from the per-project Render Secret File at runtime.
    return "\n".join(
        [
            f"PROJECT_NAME={p.name}",
            f"PROJECT_DIR={p.backend_dir}",
            f"PROJECT_VENV={p.venv}",
            f"PROJECT_APP={p.app}",
            f"PROJECT_PORT={p.port}",
            f"PROJECT_WORKERS={p.workers}",
            f"PROJECT_DB={p.db}",
            f"PROJECT_DB_PATH={p.db_path}",
            f"PROJECT_START_CMD={p.start_cmd}",
            "",
        ]
    )


def render(cfg: m.GatewayConfig) -> dict[str, str]:
    """Return every generated file as {relative path: content} (used by tests)."""
    files = {
        "Caddyfile": _caddyfile(cfg),
        "supervisord.conf": _supervisord(cfg),
    }
    for p in cfg.projects:
        files[f"run.d/{p.name}.env"] = _runspec(p)
    return files


def main() -> None:
    cfg = m.load()
    files = render(cfg)
    os.makedirs(posixpath.join(m.GEN_DIR, "run.d"), exist_ok=True)
    for rel, content in files.items():
        with open(posixpath.join(m.GEN_DIR, rel), "w") as fh:
            fh.write(content)

    names = ", ".join(p.name for p in cfg.projects) or "(none)"
    print(f"Generated config for {len(cfg.projects)} project(s): {names}")
    for p in cfg.projects:
        print(f"  {p.name}: hosts={list(p.hosts)} prefixes={list(p.path_prefixes)} -> 127.0.0.1:{p.port}")


if __name__ == "__main__":
    main()
