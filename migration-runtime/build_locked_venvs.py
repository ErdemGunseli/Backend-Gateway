"""Build the migration image with the source service's installed versions.

Build-only replacement for scripts/build_venvs.py. No application modules are
imported. The original launcher/manifest/application source stays unchanged.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import manifest

LOCK_ROOT = Path(__file__).resolve().parent


def read_lock(path):
    pairs = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9_.+!-]*", line):
            raise ValueError("Invalid locked distribution")
        name, version = line.split("==", 1)
        pairs.append((name, version))
    if not pairs or len({name.lower().replace("_", "-") for name, _ in pairs}) != len(pairs):
        raise ValueError("Empty/duplicate locked distribution set")
    return sorted(pairs)


def main():
    if sys.version_info[:3] != (3, 11, 16):
        raise SystemExit("Migration image requires source Python 3.11.16")
    cfg = manifest.load()
    for project in cfg.projects:
        lock = LOCK_ROOT / (project.name + ".requirements.lock")
        expected = read_lock(lock)
        pip_version = next((v for n, v in expected if n.lower() == "pip"), None)
        if not pip_version or not Path(project.requirements).is_file():
            raise SystemExit("Missing source lock/requirements for " + project.name)
        subprocess.run([sys.executable, "-m", "venv", project.venv], check=True)
        python = str(Path(project.venv) / "bin" / "python")
        env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_CACHE_DIR="1")
        subprocess.run([python, "-m", "pip", "install", "--no-deps", "pip==" + pip_version], env=env, check=True)
        # A complete installed-source lock includes every transitive dependency.
        # --no-deps prevents a contemporary resolver from upgrading the runtime.
        subprocess.run([python, "-m", "pip", "install", "--no-deps", "--requirement", str(lock)], env=env, check=True)
        subprocess.run([python, "-m", "pip", "check"], env=env, check=True)
        sites = list(Path(project.venv).glob("lib/python*/site-packages"))
        actual = sorted((d.metadata.get("Name", ""), d.version)
                        for d in importlib.metadata.distributions(path=[str(p) for p in sites]))
        if actual != expected:
            raise SystemExit("Installed distribution set differs from source lock: " + project.name)
        print(json.dumps({"project": project.name, "locked_distributions": len(actual), "source_versions_match": True}))


if __name__ == "__main__":
    main()
