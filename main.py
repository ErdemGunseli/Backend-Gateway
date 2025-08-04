import os
import json
from fastapi import FastAPI
from typing import List

app = FastAPI(title="Backend Gateway")

# Loading full per-tenant config from central env var:
TENANT_CONFIG = json.loads(os.getenv("TENANT_CONFIG", "{}"))


def inject_env_vars(schema: str) -> List[str]:
    config = TENANT_CONFIG.get(schema, {})
    injected_keys = []
    for key, value in config.items():
        os.environ[key] = str(value)
        injected_keys.append(key)
    os.environ["SCHEMA"] = schema
    injected_keys.append("SCHEMA")
    return injected_keys


def clear_env_vars(keys: List[str]):
    for key in keys:
        os.environ.pop(key, None)



"""
Multi-Tenant Env Var Injection Pattern:
1. INJECT: Set tenant-specific env vars
2. IMPORT: Import tenant app - ALL module-level code executes immediately, 
   capturing config values into Python objects (database engines, connections, etc.)
3. MOUNT: Mount the fully-configured app to a route
4. CLEAR: Remove env vars - safe because config is already captured in imported modules

Global env vars (e.g. DATABASE_URL) can be shared between tenants, 
but tenants CANNOT have vars with the same identifier.
"""


# ───── SEO Rise ─────
seo_env_keys = inject_env_vars("seo_rise")
from seo_rise.fastapi_backend.app import app as seo_app
app.mount("/seo-rise", seo_app)
clear_env_vars(seo_env_keys)


# ───── In-Sight ─────
insight_env_keys = inject_env_vars("in_sight")
from in_sight.fastapi_backend.app import app as insight_app
app.mount("/in-sight", insight_app)
clear_env_vars(insight_env_keys)


# ───── Heard ─────
heard_env_keys = inject_env_vars("heard")
from heard.fastapi_backend.app import app as heard_app
app.mount("/heard", heard_app)
clear_env_vars(heard_env_keys)
