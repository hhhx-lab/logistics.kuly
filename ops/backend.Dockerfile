FROM ghcr.io/astral-sh/uv:0.9.3 AS uv
FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsS -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 libreoffice-writer fonts-noto-cjk fontconfig \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /workspace/src/backend
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/workspace/src/backend/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY src/backend/pyproject.toml src/backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/backend ./
COPY ops /workspace/ops
RUN cp /workspace/ops/fonts.conf /etc/fonts/conf.d/65-logistics-cjk.conf \
    && fc-cache -f \
    && uv sync --frozen --no-dev && useradd --uid 10001 --create-home app
USER 10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
