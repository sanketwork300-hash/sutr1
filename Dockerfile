# ─── Stage 1: build the UI ───────────────────────────────────────────────────
FROM node:22-slim AS ui-builder

ARG IS_CLOUD=false
ARG VITE_PUBLIC_POSTHOG_HOST=https://us.i.posthog.com
ENV VITE_IS_CLOUD=$IS_CLOUD
ENV VITE_PUBLIC_POSTHOG_HOST=$VITE_PUBLIC_POSTHOG_HOST

WORKDIR /app/ui

RUN corepack enable

COPY ui/package.json ui/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY ui/ ./
RUN pnpm build

# ─── Stage 2: Python runtime ─────────────────────────────────────────────────
FROM python:3.12-slim

# libpq-dev is required to compile psycopg2 from source.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/sutr.db

WORKDIR /app/server

# Install dependencies before copying source for better layer caching.
COPY server/pyproject.toml server/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra postgres --extra aws

# Copy source and install the project itself.
COPY server/src ./src
COPY server/alembic ./alembic
COPY server/alembic.ini ./
RUN uv sync --frozen --no-dev --extra postgres --extra aws

# Copy built UI so FastAPI can serve it.
COPY --from=ui-builder /app/ui/dist ./ui_dist

# Volume mount point for SQLite data.
RUN mkdir -p /data

COPY server/start.sh ./start.sh
RUN chmod +x ./start.sh

# Run as a non-root user. Found by the Semgrep gate in Phase 13, and it was a
# real gap rather than a lint: the Helm chart already declared
# `runAsNonRoot: true` with uid 1000, so an image that only ran as root would
# either be refused by the kubelet or run with a /data it cannot write.
#
# uid 1000 specifically, because that is the number the chart's securityContext
# names; the two have to agree or the volume is owned by the wrong user.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin sutr \
 && chown -R sutr:sutr /app /data
USER sutr

EXPOSE 4747

CMD ["./start.sh"]
