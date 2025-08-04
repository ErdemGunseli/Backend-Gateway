import os
import json
import sys
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


def import_isolated_app(project_path: str):
    """Import FastAPI app with proper module isolation to prevent relative import conflicts"""
    # Save current state
    old_modules = set(sys.modules.keys())
    old_path = sys.path.copy()
    
    try:
        # Temporarily modify sys.path to prioritize the project directory
        sys.path.insert(0, project_path)
        
        # Load the main module in isolation
        spec = importlib.util.spec_from_file_location("main", f"{project_path}/main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.app
        
    finally:
        # Clean up to prevent cross-project contamination
        sys.path[:] = old_path
        
        # Remove any newly imported modules to prevent namespace pollution
        new_modules = set(sys.modules.keys()) - old_modules
        for mod_name in new_modules:
            # Only remove modules that could cause conflicts (not built-ins)
            if not mod_name.startswith('_') and '.' not in mod_name:
                sys.modules.pop(mod_name, None)


# ────────── SEO Rise ───────────────────────────────────
keys = inject_env_vars("seo_rise")
seo_app = import_isolated_app("seo_rise/fastapi-backend")
app.mount("/seo-rise", seo_app)
clear_env_vars(keys)


# ────────── In-Sight ───────────────────────────────────
keys = inject_env_vars("in_sight")
insight_app = import_isolated_app("in_sight/fastapi-backend")
app.mount("/in-sight", insight_app)
clear_env_vars(keys)


# ────────── Heard ──────────────────────────────────────
keys = inject_env_vars("heard")
heard_app = import_isolated_app("heard/fastapi-backend")
app.mount("/heard", heard_app)
clear_env_vars(keys)
