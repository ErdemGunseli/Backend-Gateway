from fastapi import FastAPI

from seo_rise.fastapi_backend.app import app as seo_app
from in_sight.fastapi_backend.app import app as insight_app
from heard.fastapi_backend.app    import app as heard_app

app = FastAPI(title="Backend Gateway")

app.mount("/seo-rise",  seo_app)
app.mount("/in-sight",  insight_app)
app.mount("/heard",     heard_app)
