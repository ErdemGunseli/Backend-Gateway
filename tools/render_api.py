"""Minimal Render REST API client used by the swap tooling.

One instance is bound to one account's API key, so cross-account swaps construct
two clients (source + gateway). Only the operations the swap playbooks need are
implemented. See https://api-docs.render.com/ for the full API.
"""

from __future__ import annotations

import time

import requests

BASE = "https://api.render.com/v1"

# Deploy status values (https://api-docs.render.com/reference/get-deploy):
DEPLOY_SUCCESS = {"live"}
DEPLOY_FAILURE = {"build_failed", "update_failed", "pre_deploy_failed", "canceled", "deactivated"}


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

    # ── Discovery ────────────────────────────────────────────────────────────
    def list_owners(self) -> list[dict]:
        """Workspaces this key can reach: each {id, name, email, type:user|team}.

        The `id` is the `ownerId` required when creating a service.
        """
        r = self._req("GET", "/owners", params={"limit": 100})
        return [item.get("owner", item) for item in r.json()]

    def resolve_owner_id(self, hint: str | None) -> str:
        """Pick a workspace id. With no hint, require exactly one workspace."""
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
            f"[{self.account_label}] key reaches {len(owners)} workspaces; pass --owner "
            f"to choose one of: {[(o.get('name'), o.get('email')) for o in owners]}"
        )

    def find_service(self, name: str) -> dict | None:
        """Return the service dict whose name matches exactly, else None."""
        r = self._req("GET", "/services", params={"name": name, "limit": 100})
        for item in r.json():
            svc = item.get("service", item)
            if svc.get("name") == name:
                return svc
        return None

    def get_service(self, service_id: str) -> dict:
        return self._req("GET", f"/services/{service_id}").json()

    # ── Provisioning ───────────────────────────────────────────────────────────
    def create_service(
        self,
        *,
        name: str,
        owner_id: str,
        repo: str,
        branch: str = "main",
        plan: str = "starter",
        region: str = "oregon",
        dockerfile_path: str = "./Dockerfile",
        health_check_path: str | None = None,
        disk: dict | None = None,          # {"name", "mountPath", "sizeGB"}
        env_vars: list[dict] | None = None,  # [{"key","value"}]
        secret_files: list[dict] | None = None,  # [{"name","content"}]
        auto_deploy: bool = False,
    ) -> dict:
        """Create a Docker web service, optionally with its disk/secrets inline.

        Render has no Blueprint API, so this is the scripted-provisioning path.
        The disk can be created in this same call (one fewer round-trip).
        """
        details: dict = {
            "runtime": "docker",
            "plan": plan,
            "region": region,
            "dockerfilePath": dockerfile_path,
        }
        if health_check_path:
            details["healthCheckPath"] = health_check_path
        if disk:
            details["disk"] = disk
        body: dict = {
            "type": "web_service",
            "name": name,
            "ownerId": owner_id,
            "repo": repo,
            "branch": branch,
            "autoDeploy": "yes" if auto_deploy else "no",
            "serviceDetails": details,
        }
        if env_vars:
            body["envVars"] = env_vars
        if secret_files:
            body["secretFiles"] = secret_files
        r = self._req("POST", "/services", json=body).json()
        # The create response nests the service under "service" alongside a deploy id:
        return r.get("service", r)

    def update_service(self, service_id: str, **fields) -> dict:
        """PATCH mutable service fields (branch, autoDeploy, serviceDetails.*, ...).

        Pass exactly the Render API body shape, e.g.
        ``update_service(sid, branch="main", autoDeploy="no",
        serviceDetails={"healthCheckPath": "/__gateway/health"})``.
        """
        return self._req("PATCH", f"/services/{service_id}", json=fields).json()

    # ── Environment variables ──────────────────────────────────────────────────
    def list_env_vars(self, service_id: str) -> list[dict]:
        r = self._req("GET", f"/services/{service_id}/env-vars", params={"limit": 100})
        return [item.get("envVar", item) for item in r.json()]

    def delete_env_var(self, service_id: str, key: str) -> bool:
        """Remove one env var. Returns False when it was already absent."""
        r = self._s.delete(f"{BASE}/services/{service_id}/env-vars/{key}", timeout=30)
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True

    # ── Custom domains ──────────────────────────────────────────────────────────
    def list_custom_domains(self, service_id: str) -> list[dict]:
        r = self._req("GET", f"/services/{service_id}/custom-domains", params={"limit": 100})
        return [item.get("customDomain", item) for item in r.json()]

    def add_custom_domain(self, service_id: str, name: str) -> dict:
        return self._req(
            "POST", f"/services/{service_id}/custom-domains", json={"name": name}
        ).json()

    def delete_custom_domain(self, service_id: str, id_or_name: str) -> None:
        self._req("DELETE", f"/services/{service_id}/custom-domains/{id_or_name}")

    def verify_custom_domain(self, service_id: str, id_or_name: str) -> None:
        self._req("POST", f"/services/{service_id}/custom-domains/{id_or_name}/verify")

    # ── Deploys ─────────────────────────────────────────────────────────────────
    def trigger_deploy(self, service_id: str, *, clear_cache: bool = False) -> dict:
        body = {"clearCache": "clear" if clear_cache else "do_not_clear"}
        return self._req("POST", f"/services/{service_id}/deploys", json=body).json()

    def get_deploy(self, service_id: str, deploy_id: str) -> dict:
        return self._req("GET", f"/services/{service_id}/deploys/{deploy_id}").json()

    def wait_deploy(self, service_id: str, deploy_id: str, *, timeout: int = 900) -> str:
        """Poll a deploy until it reaches a terminal state; return that status.

        Raises TimeoutError if it doesn't settle in `timeout` seconds.
        """
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

    # ── Lifecycle (suspend/resume) ──────────────────────────────────────────────
    def suspend(self, service_id: str) -> None:
        self._req("POST", f"/services/{service_id}/suspend")

    def resume(self, service_id: str) -> None:
        self._req("POST", f"/services/{service_id}/resume")

    # ── Secrets ───────────────────────────────────────────────────────────────
    def list_secret_files(self, service_id: str) -> list[dict]:
        r = self._req("GET", f"/services/{service_id}/secret-files", params={"limit": 100})
        return [item.get("secretFile", item) for item in r.json()]

    def upsert_secret_file(self, service_id: str, name: str, content: str) -> None:
        """Create or replace a single secret file on a service."""
        self._req(
            "PUT",
            f"/services/{service_id}/secret-files/{name}",
            json={"content": content},
        )

    def get_secret_file(self, service_id: str, name: str) -> str | None:
        r = self._s.get(f"{BASE}/services/{service_id}/secret-files/{name}", timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("content")
