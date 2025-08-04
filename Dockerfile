FROM python:3.11-slim

WORKDIR /app

# Copying gateway entrypoint:
COPY main.py .

# Copying backend source code, renaming dashes to underscores:
COPY seo_rise/fastapi-backend/ seo_rise/fastapi_backend/
COPY in_sight/fastapi-backend/ in_sight/fastapi_backend/
COPY heard/fastapi-backend/ heard/fastapi_backend/

# Copying requirement files:
COPY seo_rise/fastapi-backend/requirements.txt requirements-seo.txt
COPY in_sight/fastapi-backend/requirements.txt requirements-insight.txt
COPY heard/fastapi-backend/requirements.txt requirements-heard.txt

# Installing dependencies:
RUN pip install --no-cache-dir -r requirements-seo.txt \
 && pip install --no-cache-dir -r requirements-insight.txt \
 && pip install --no-cache-dir -r requirements-heard.txt

# Ensuring Python recognizes copied directories as packages:
RUN touch seo_rise/fastapi_backend/__init__.py \
 && touch in_sight/fastapi_backend/__init__.py \
 && touch heard/fastapi_backend/__init__.py

# Exposing port:
EXPOSE 10000

# Starting the gateway app:
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]
