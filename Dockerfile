ARG PYTHON_VERSION=3.12-slim

FROM python:${PYTHON_VERSION} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY api ./api

RUN python -m pip install --upgrade pip build \
    && python -m build --wheel --outdir /dist


FROM python:${PYTHON_VERSION} AS production

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app

WORKDIR ${APP_HOME}

RUN groupadd --gid 1000 memoryos \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin memoryos

COPY --from=builder /dist/*.whl /tmp/
COPY alembic.ini ${APP_HOME}/alembic.ini
COPY api/db/migrations ${APP_HOME}/api/db/migrations

RUN python -m pip install --no-cache-dir /tmp/*.whl \
    && rm -rf /tmp/*.whl

USER 1000:1000

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
