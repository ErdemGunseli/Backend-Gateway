FROM python:3.11-slim

WORKDIR /app

# Submodules need to be loaded on hosting via pre-deploy command

# Copying gateway entrypoint:
COPY main.py .

# Copying backend source code:
COPY seo_rise/fastapi_backend/ seo_rise/fastapi_backend/
COPY in_sight/fastapi_backend/ in_sight/fastapi_backend/
COPY heard/fastapi_backend/    heard/fastapi_backend/

# Creating project root __init__.py files, enabling python imports without cluttering source code:
RUN echo "# Generated for Python imports - SEO Rise package" > seo_rise/__init__.py
RUN echo "# Generated for Python imports - In-Sight package" > in_sight/__init__.py  
RUN echo "# Generated for Python imports - Heard package" > heard/__init__.py

# Creating fastapi_backend __init__.py files to expose apps (no source code requirements):
RUN echo "from .main import app" > seo_rise/fastapi_backend/__init__.py
RUN echo "from .main import app" > in_sight/fastapi_backend/__init__.py
RUN echo "from .main import app" > heard/fastapi_backend/__init__.py

# Copying requirement files:
COPY seo_rise/fastapi_backend/requirements.txt   requirements-seo.txt
COPY in_sight/fastapi_backend/requirements.txt   requirements-insight.txt
COPY heard/fastapi_backend/requirements.txt      requirements-heard.txt

# Installing dependencies:
RUN pip install --no-cache-dir -r requirements-seo.txt \
 && pip install --no-cache-dir -r requirements-insight.txt \
 && pip install --no-cache-dir -r requirements-heard.txt

# Exposing port:
EXPOSE 10000

# Starting the gateway app:
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]
