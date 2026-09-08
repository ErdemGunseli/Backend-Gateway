"""Render REST API client — account-agnostic, used by render_ops.py.

One client is bound to one account's API key (accounts are selected by label at the
CLI layer). Only the operations the ops/migration workflows need are implemented.
Docs: https://api-docs.render.com/  ·  base https://api.render.com/v1

This file is self-contained so the skill is portable; it does not import anything
from the surrounding repo.
"""

from __future__ import annotations

import time

import requests

BASE = "https://api.render.com/v1"

# Deploy status values (https://api-docs.render.com/reference/get-deploy):
DEPLOY_SUCCESS = {"live"}
DEPLOY_FAILURE = {"build_failed", "update_failed", "pre_deploy_failed", "canceled", "deactivated"}

# serviceDetails fields that are valid to send back on create (a clone copies these
# from the source service; read-only/runtime fields like url are dropped):
_CLONE_DETAIL_FIELDS = (
    "runtime", "plan", "region", "numInstances", "healthCheckPath",
    "dockerfilePath", "dockerContext", "dockerCommand",
    "buildCommand", "startCommand", "preDeployCommand",
)


class RenderClient:
    def __init__(self, api_key: str, *, account_label: str = "default"):
        if not api_key:
            raise ValueError(f"Render API key for '{account_label}' is empty")
        self.account_label = account_label
        self._s = requests.Session()
        self._s.headers.update(
            {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        )

    def _req(self, method: str, path: str, **kw) -> requests.Response:
        r = self._s.request(method, f"{BASE}{path}", timeout=30, **kw)
        r.raise_for_status()
        return r

    def _paged(self, path: str, key: str, params: dict | None = None) -> list[dict]:
        """Walk Render's cursor pagination, returning the unwrapped resource dicts."""
        out: list[dict] = []
        cursor = None
        while True:
            p = {"limit": 100, **(params or {})}
            if cursor:
                p["cursor"] = cursor
            page = self._req("GET", path, params=p).json()
            if not page:
                break
            for item in page:
                out.append(item.get(key, item))
            cursor = page[-1].get("cursor")
            if not cursor or len(page) < p["limit"]:
                break
        return out

    # ── Workspaces ──────────────────────────────────────────────────────────────
    def list_owners(self) -> list[dict]:
        """Workspaces this key can reach: {id, name, email, type:user|team}."""
        return self._paged("/owners", "owner")

    def resolve_owner_id(self, hint: str | None) -> str:
        owners = self.list_owners()
        if hint:
            for o in owners:
                if hint in (o.get("id"), o.get("name"), o.get("email")):
                    return o["id"]
            raise ValueError(
                f"[{self.account_label}] no workspace matches '{hint}'. "
                f"Available: {[(o.get('name'), o.get('email')) for o in owners]}"
            )
        if len(owners) == 1:
            return owners[0]["id"]
        raise ValueError(
            f"[{self.account_label}] key reaches {len(owners)} workspaces; pass "
            f"--owner to choose: {[(o.get('name'), o.get('email')) for o in owners]}"
        )

    # ── Services ────────────────────────────────────────────────────────────────
    def list_services(self, *, type: str | None = None) -> list[dict]:
        params = {"type": type} if type else None
        return self._paged("/services", "service", params)

    def find_service(self, name: str) -> dict | None:
        for svc in self._paged("/services", "service", {"name": name}):
            if svc.get("name") == name:
                return svc
        return None

    def get_service(self, service_id: str) -> dict:
        return self._req("GET", f"/services/{service_id}").json()

    def create_service(
        self, *, type: str, name: str, owner_id: str, repo: str, branch: str = "main",
        auto_deploy: bool = False, service_details: dict | None = None,
        env_vars: list[dict] | None = None, secret_files: list[dict] | None = None,
    ) -> dict:
        body: dict = {
            "type": type, "name": name, "ownerId": owner_id, "repo": repo,
            "branch": branch, "autoDeploy": "yes" if auto_deploy else "no",
            "serviceDetails": service_details or {},
        }
        if env_vars:
            body["envVars"] = env_vars
        if secret_files:
            body["secretFiles"] = secret_files
        r = self._req("POST", "/services", json=body).json()
        return r.get("service", r)

    def clone_kwargs(self, src: dict, *, owner_id: str, overrides: dict | None = None) -> dict:
        """Build create_service(**kwargs) that reproduces `src` in another workspace.

        Copies type/repo/branch and the create-safe serviceDetails fields (incl. the
        disk, with its read-only id stripped). Anything Render doesn't return on the
        create-safe path (autoscaling, build filters) must be reconciled manually —
        the migration runbook calls this out.
        """
        sd = src.get("serviceDetails", {}) or {}
        details = {k: sd[k] for k in _CLONE_DETAIL_FIELDS if sd.get(k) not in (None, "")}
        disk = sd.get("disk") or {}
        if disk.get("mountPath"):
            details["disk"] = {
                "name": disk.get("name", "data"),
                "mountPath": disk["mountPath"],
                "sizeGB": disk.get("sizeGB", 1),
            }
        kwargs = {
            "type": src.get("type", "web_service"),
            "name": src.get("name"),
            "owner_id": owner_id,
            "repo": src.get("repo", ""),
            "branch": src.get("branch", "main"),
            "service_details": details,
        }
        kwargs.update(overrides or {})
        return kwargs

    def service_disk(self, src: dict) -> dict | None:
        """The disk descriptor {name, mountPath, sizeGB} if the service has one."""
        d = (src.get("serviceDetails", {}) or {}).get("disk") or {}
        return d if d.get("mountPath") else None

    # ── Env vars & secret files ──────────────────────────────────────────────────
    def list_env_vars(self, service_id: str) -> list[dict]:
        return self._paged(f"/services/{service_id}/env-vars", "envVar")

    def replace_env_vars(self, service_id: str, pairs: list[dict]) -> None:
        """Bulk PUT — replaces the FULL set (omitted vars are removed)."""
        self._req("PUT", f"/services/{service_id}/env-vars", json=pairs)

    def list_secret_files(self, service_id: str) -> list[dict]:
        return self._paged(f"/services/{service_id}/secret-files", "secretFile")

    def get_secret_file(self, service_id: str, name: str) -> str | None:
        r = self._s.get(f"{BASE}/services/{service_id}/secret-files/{name}", timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("content")

    def upsert_secret_file(self, service_id: str, name: str, content: str) -> None:
        self._req("PUT", f"/services/{service_id}/secret-files/{name}", json={"content": content})

    # ── Custom domains ───────────────────────────────────────────────────────────
    def list_custom_domains(self, service_id: str) -> list[dict]:
        return self._paged(f"/services/{service_id}/custom-domains", "customDomain")

    def add_custom_domain(self, service_id: str, name: str) -> dict:
        return self._req("POST", f"/services/{service_id}/custom-domains", json={"name": name}).json()

    def delete_custom_domain(self, service_id: str, id_or_name: str) -> None:
        self._req("DELETE", f"/services/{service_id}/custom-domains/{id_or_name}")

    def verify_custom_domain(self, service_id: str, id_or_name: str) -> None:
        self._req("POST", f"/services/{service_id}/custom-domains/{id_or_name}/verify")

    # ── Deploys & lifecycle ──────────────────────────────────────────────────────
    def trigger_deploy(self, service_id: str, *, clear_cache: bool = False) -> dict:
        body = {"clearCache": "clear" if clear_cache else "do_not_clear"}
        return self._req("POST", f"/services/{service_id}/deploys", json=body).json()

    def get_deploy(self, service_id: str, deploy_id: str) -> dict:
        return self._req("GET", f"/services/{service_id}/deploys/{deploy_id}").json()

    def wait_deploy(self, service_id: str, deploy_id: str, *, timeout: int = 900) -> str:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            status = self.get_deploy(service_id, deploy_id).get("status", "")
            if status != last:
                print(f"  deploy {deploy_id}: {status}")
                last = status
            if status in DEPLOY_SUCCESS or status in DEPLOY_FAILURE:
                return status
            time.sleep(5)
        raise TimeoutError(f"deploy {deploy_id} did not finish within {timeout}s (last: {last})")

    def suspend(self, service_id: str) -> None:
        self._req("POST", f"/services/{service_id}/suspend")

    def resume(self, service_id: str) -> None:
        self._req("POST", f"/services/{service_id}/resume")
