"""Create an isolated virtualenv per project and install its requirements.

Run during the Docker build. Each project gets its own venv so projects can pin
incompatible dependency versions - the shared-venv alternative saves no RAM
(module memory is per-process regardless) while sacrificing that isolation.

Missing submodules are skipped with a warning so the image can still build in a
partially-populated checkout (e.g. while staging the gateway outside its own repo).
"""

from __future__ import annotations

import os
import subprocess
import sys

import manifest as m


def main() -> None:
    cfg = m.load()
    failures: list[str] = []

    for p in cfg.projects:
        if not os.path.isfile(p.requirements):
            print(f"WARNING: {p.name}: requirements not found at {p.requirements}; skipping venv")
            continue

        print(f"==> {p.name}: creating venv at {p.venv}")
        subprocess.run([sys.executable, "-m", "venv", p.venv], check=True)
        pip = os.path.join(p.venv, "bin", "pip")
        subprocess.run([pip, "install", "--no-cache-dir", "--upgrade", "pip"], check=True)
        try:
            subprocess.run([pip, "install", "--no-cache-dir", "-r", p.requirements], check=True)
        except subprocess.CalledProcessError:
            failures.append(p.name)
            print(f"ERROR: {p.name}: dependency install failed")

    if failures:
        raise SystemExit(f"venv build failed for: {', '.join(failures)}")
    print("All project venvs built.")


if __name__ == "__main__":
    main()
