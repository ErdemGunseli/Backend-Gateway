import os
import json
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


# ────────── SEO Rise ───────────────────────────────────
keys = inject_env_vars("seo_rise")
from seo_rise.fastapi_backend import app as seo_app
app.mount("/seo-rise", seo_app)
clear_env_vars(keys)


# ────────── In-Sight ───────────────────────────────────
keys = inject_env_vars("in_sight")
from in_sight.fastapi_backend import app as insight_app
app.mount("/in-sight", insight_app)
clear_env_vars(keys)


# ────────── Heard ──────────────────────────────────────
keys = inject_env_vars("heard")
from heard.fastapi_backend import app as heard_app
app.mount("/heard", heard_app)
clear_env_vars(keys)
