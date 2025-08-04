import os
import json
import importlib.util
from fastapi import FastAPI
from typing import List

app = FastAPI(title="Backend Gateway")

# Load tenant config from env
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


def load_app(path: str):
    spec = importlib.util.spec_from_file_location("sub_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


"""
Multi-Tenant Env Var Injection Pattern:
1. INJECT: Set tenant-specific env vars
2. IMPORT: Load tenant app file - executes module-level code immediately
3. MOUNT: Mount the fully-configured app to a route
4. CLEAR: Remove env vars - safe because config is already captured
"""

# ───── SEO Rise ─────
seo_env_keys = inject_env_vars("seo_rise")
seo_app = load_app("seo_rise/fastapi_backend/app.py")
app.mount("/seo-rise", seo_app)
clear_env_vars(seo_env_keys)


# ───── In-Sight ─────
insight_env_keys = inject_env_vars("in_sight")
insight_app = load_app("in_sight/fastapi_backend/app.py")
app.mount("/in-sight", insight_app)
clear_env_vars(insight_env_keys)


# ───── Heard ─────
heard_env_keys = inject_env_vars("heard")
heard_app = load_app("heard/fastapi_backend/app.py")
app.mount("/heard", heard_app)
clear_env_vars(heard_env_keys)
