# syntax=docker/dockerfile:1.7

# --- Dashboard SPA ---------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY web/ ./
RUN npm run build

# --- Python app ------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first so source edits keep this layer cached.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-default-groups --no-install-project

COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY migration ./migration
COPY yggdrasil ./yggdrasil
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups

COPY --from=web /web/dist /app/web/dist

# Root FS is read-only in the cluster: anything written at runtime (jieba's
# dict cache, tempfiles) goes to TMPDIR, an emptyDir mounted at /tmp.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TMPDIR=/tmp \
    HOME=/tmp \
    YGG_WEB_DIST=/app/web/dist

# Last so a new tag only changes this metadata, not any cached layer above.
ARG YGG_VERSION=dev
ENV YGG_VERSION=$YGG_VERSION

USER 65532:65532
EXPOSE 8080

# Bot (NoneBot + dashboard API) on YGG_PORT. Other roles reuse the image:
#   worker:    taskiq worker yggdrasil.worker.broker:broker yggdrasil.worker.tasks
#   scheduler: taskiq scheduler yggdrasil.worker.broker:scheduler yggdrasil.worker.tasks
#   migration: alembic upgrade head
CMD ["python", "-m", "yggdrasil"]
