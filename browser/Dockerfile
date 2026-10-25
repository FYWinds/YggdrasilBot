# syntax=docker/dockerfile:1.7
# Browser service (yggdrasil.browser): real Google Chrome, headed under Xvfb, driven by patchright, with nodriver as
# the escalation, for read_url's browser tier. Build from the repository root:
#   docker build -f browser/Dockerfile .
# Only the service module and the SSRF guard it imports are copied, not the bot.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# Google Chrome stable (its .deb pulls the libraries it needs), Xvfb, and desktop-like fonts (CJK, emoji, metric-
# compatible Latin) so font probing looks like a real Linux desktop.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tzdata xvfb \
        fonts-noto-cjk fonts-noto-color-emoji fonts-liberation \
    && curl -fsSLo /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm -rf /tmp/chrome.deb /var/lib/apt/lists/* \
    && google-chrome --version

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Versions pinned by uv.lock; only what the service imports is installed.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv export --frozen --no-hashes --no-emit-project --no-default-groups --group browser -o /tmp/constraints.txt \
    && uv venv /app/.venv \
    && VIRTUAL_ENV=/app/.venv uv pip install --constraint /tmp/constraints.txt \
        httpx certifi starlette uvicorn patchright nodriver \
    && rm /tmp/constraints.txt

COPY yggdrasil/__init__.py yggdrasil/
COPY yggdrasil/tools/__init__.py yggdrasil/tools/errors.py yggdrasil/tools/ssrf.py yggdrasil/tools/
COPY yggdrasil/browser yggdrasil/browser/

# Root FS is read-only in the cluster: the Chrome profiles, Xvfb socket and fontconfig cache live in /tmp (emptyDir).
# TZ matches the node's egress location so the timezone agrees with the IP (override per deployment); LANGUAGE sets
# Chrome's UI language and Accept-Language (Linux Chrome ignores --lang).
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    TMPDIR=/tmp \
    TZ=Europe/Berlin \
    LANGUAGE=zh_CN:zh

USER 65532:65532
EXPOSE 8080
STOPSIGNAL SIGTERM
CMD ["python", "-m", "yggdrasil.browser"]
