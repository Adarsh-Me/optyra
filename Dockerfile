FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# Bake default config (orgs.yaml etc.) into the image; CONFIG_PATH defaults to /app/config
COPY config ./config
RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 optyra \
    && chown -R optyra:optyra /app

USER optyra
EXPOSE 8080

# Container-level liveness: the worker's /healthz endpoint.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).status==200 else 1)"

CMD ["python", "-m", "optyra"]
