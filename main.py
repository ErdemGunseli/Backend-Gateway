import os
import json
import importlib.util
from fastapi import FastAPI
from typing import List

app = FastAPI(title="Backend Gateway")

TENANT_CONFIG = json.loads(os.getenv("TENANT_CONFIG", "{}"))


def inject_env_vars(schema: str) -> List[str]:
    cfg = TENANT_CONFIG.get(schema, {})
    injected = []
    for k, v in cfg.items():
        os.environ[k] = str(v)
        injected.append(k)
    os.environ["SCHEMA"] = schema
    injected.append("SCHEMA")
    return injected


def clear_env_vars(keys: List[str]):
    for k in keys:
        os.environ.pop(k, None)


def load_app_direct(project_name: str):
    # Using unique module name to prevent sys.modules caching conflicts:
    unique_module_name = f"{project_name}_fastapi_backend"
    module_path = f"{project_name}/fastapi_backend/__init__.py"
    
    spec = importlib.util.spec_from_file_location(unique_module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


# ────────── SEO Rise ───────────────────────────────────
keys = inject_env_vars("seo_rise")
seo_app = load_app_direct("seo_rise")
app.mount("/seo-rise", seo_app)
clear_env_vars(keys)


# ────────── In-Sight ───────────────────────────────────
keys = inject_env_vars("in_sight")
insight_app = load_app_direct("in_sight")
app.mount("/in-sight", insight_app)
clear_env_vars(keys)


# ────────── Heard ──────────────────────────────────────
keys = inject_env_vars("heard")
heard_app = load_app_direct("heard")
app.mount("/heard", heard_app)
clear_env_vars(keys)
