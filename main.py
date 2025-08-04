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


def load_app(filepath: str):
    spec = importlib.util.spec_from_file_location("subapp", filepath)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)    
    return mod.app                 


# ────────── SEO Rise ───────────────────────────────────
keys = inject_env_vars("seo_rise")
seo_app = load_app("seo_rise/fastapi-backend/app.py")
app.mount("/seo-rise", seo_app)
clear_env_vars(keys)


# ────────── In-Sight ───────────────────────────────────
keys = inject_env_vars("in_sight")
insight_app = load_app("in_sight/fastapi-backend/app.py")
app.mount("/in-sight", insight_app)
clear_env_vars(keys)


# ────────── Heard ──────────────────────────────────────
keys = inject_env_vars("heard")
heard_app = load_app("heard/fastapi-backend/app.py")
app.mount("/heard", heard_app)
clear_env_vars(keys)
