"""Manifest loading and project resolution.

Shared by the build-time and boot-time tooling (`build_venvs.py`,
`generate_config.py`) and by the CLI. Reads `gateway.toml` and turns each enabled
project into a fully-resolved record with absolute container paths, an assigned
internal port, its routable hostnames and path prefixes, and a derived database URL.

Paths are resolved against GATEWAY_APP_ROOT (default `/app`, the container WORKDIR)
so the generated configs always reference real container paths regardless of where
the generator runs. Generated artifacts go to GATEWAY_GEN_DIR (default
`<APP_ROOT>/generated`).
"""

from __future__ import annotations

import os
import posixpath
import tomllib
from dataclasses import dataclass

APP_ROOT = os.environ.get("GATEWAY_APP_ROOT", "/app")
GEN_DIR = os.environ.get("GATEWAY_GEN_DIR", posixpath.join(APP_ROOT, "generated"))
MANIFEST_PATH = os.environ.get("GATEWAY_MANIFEST", posixpath.join(APP_ROOT, "gateway.toml"))


@dataclass(frozen=True)
class Project:
    name: str
    subdomain: str
    hostname: str          # host_template rendered, e.g. api.<name>.<base_domain>
    hosts: tuple[str, ...] # every Host name routed here: hostname + extra_hosts
    path_prefixes: tuple[str, ...]  # prefixes routed here on any other host ("" = none)
    port: int              # internal localhost port
    submodule_path: str    # absolute path to the submodule root
    backend_dir: str       # absolute path to the dir holding requirements + app
    venv: str              # absolute path to the project's virtualenv
    app: str               # ASGI target, e.g. "main:app"
    requirements: str      # absolute path to the requirements file
    workers: int
    health_path: str
    db: str                # "sqlite" | "external"
    db_path: str           # sqlite file path (only meaningful when db == "sqlite")
    start_cmd: str         # optional explicit start command ("" = default runner)


@dataclass(frozen=True)
class GatewayConfig:
    base_domain: str
    host_template: str     # e.g. "api.{name}.{base_domain}"
    port_base: int
    data_dir: str
    secrets_dir: str
    service_name: str      # the Render web service hosting the gateway
    projects: tuple[Project, ...]


def _abs(*parts: str) -> str:
    return posixpath.normpath(posixpath.join(APP_ROOT, *parts))


def _normalise_prefix(prefix: str) -> str:
    """Return a path prefix as `/name` (leading slash, no trailing slash)."""
    prefix = prefix.strip()
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    prefix = prefix.rstrip("/")
    if not prefix or "*" in prefix or " " in prefix:
        raise ValueError(f"invalid path prefix {prefix!r}")
    return prefix


def load(path: str | None = None) -> GatewayConfig:
    with open(path or MANIFEST_PATH, "rb") as fh:
        raw = tomllib.load(fh)

    g = raw.get("gateway", {})
    base_domain = g["base_domain"]
    # How a project's primary hostname is formed. Placeholders: {name}, {subdomain},
    # {base_domain}. The factory default is api.<project>.<base_domain>, so a project's
    # frontend at <project>.<base_domain> and its API share a project-specific parent.
    host_template = g.get("host_template", "api.{name}.{base_domain}")
    port_base = int(g.get("port_base", 8001))
    data_dir = g.get("data_dir", "/data")
    secrets_dir = g.get("secrets_dir", "/etc/secrets")
    service_name = g.get("service_name", "gateway")

    projects: list[Project] = []
    next_port = port_base
    seen_names: set[str] = set()
    seen_hosts: dict[str, str] = {}
    seen_prefixes: dict[str, str] = {}
    for entry in raw.get("project", []):
        if not entry.get("enabled", True):
            continue

        name = entry["name"]
        if name in seen_names:
            raise ValueError(f"duplicate project name {name!r} in manifest")
        seen_names.add(name)
        subdomain = entry.get("subdomain", name)
        port = int(entry.get("port", next_port))
        next_port = max(next_port, port) + 1

        submodule_path = _abs(entry["path"])
        backend_dir_rel = entry.get("backend_dir", ".")
        backend_dir = submodule_path if backend_dir_rel == "." else _abs(entry["path"], backend_dir_rel)
        requirements = posixpath.join(backend_dir, entry.get("requirements", "requirements.txt"))

        hostname = host_template.format(name=name, subdomain=subdomain, base_domain=base_domain).lower()
        hosts = (hostname, *[h.strip().lower() for h in entry.get("extra_hosts", []) if h.strip()])
        for h in hosts:
            if h in seen_hosts:
                raise ValueError(f"host {h!r} is claimed by both {seen_hosts[h]!r} and {name!r}")
            seen_hosts[h] = name

        # Path-prefix routing on hosts no project claims (the service's own
        # onrender.com host before DNS exists, legacy aliases). Default: /<name>.
        # An explicit empty list disables it for the project.
        raw_prefixes = entry.get("path_prefixes", [f"/{name}"])
        prefixes = tuple(_normalise_prefix(p) for p in raw_prefixes)
        for p in prefixes:
            if p in seen_prefixes:
                raise ValueError(f"path prefix {p!r} is claimed by both {seen_prefixes[p]!r} and {name!r}")
            seen_prefixes[p] = name

        projects.append(
            Project(
                name=name,
                subdomain=subdomain,
                hostname=hostname,
                hosts=hosts,
                path_prefixes=prefixes,
                port=port,
                submodule_path=submodule_path,
                backend_dir=backend_dir,
                venv=posixpath.join(submodule_path, ".venv"),
                app=entry.get("app", "main:app"),
                requirements=requirements,
                workers=int(entry.get("workers", 1)),
                health_path=entry.get("health_path", "/healthz"),
                db=entry.get("db", "sqlite"),
                db_path=posixpath.join(data_dir, f"{name}.db"),
                start_cmd=entry.get("start_cmd", ""),
            )
        )

    return GatewayConfig(
        base_domain=base_domain,
        host_template=host_template,
        port_base=port_base,
        data_dir=data_dir,
        secrets_dir=secrets_dir,
        service_name=service_name,
        projects=tuple(projects),
    )
