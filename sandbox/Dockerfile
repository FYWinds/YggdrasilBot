# YggdrasilBot sandbox image: per-QQ-group workspace pod (gVisor) running sandboxd under tini.
# Build context: sandbox/. Targets:
#   sandboxd-build  static sandboxd binary built from kubernetes-sigs/agent-sandbox (no release binary exists)
#   sandboxd        minimal sandboxd image for the CI conformance tests
#   (default)       full workspace image

ARG SANDBOXD_VERSION=v1.0.7

FROM golang:1.27-bookworm AS sandboxd-build
ARG SANDBOXD_VERSION
WORKDIR /src
RUN git clone --depth 1 --branch "${SANDBOXD_VERSION}" https://github.com/kubernetes-sigs/agent-sandbox . \
    && CGO_ENABLED=0 GOOS=linux go build -trimpath \
        -ldflags="-s -w \
            -X sigs.k8s.io/agent-sandbox/internal/version.gitVersion=${SANDBOXD_VERSION} \
            -X sigs.k8s.io/agent-sandbox/internal/version.gitSHA=$(git rev-parse HEAD)" \
        -o /out/sandboxd ./packages/sandboxd/cmd/sandboxd \
    && /out/sandboxd --version

# Minimal sandboxd for the conformance suite: shell, coreutils and process tools only.
FROM debian:bookworm-slim AS sandboxd
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini bash coreutils util-linux procps ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 1000 -U -d /workspace -M -s /bin/bash sandbox \
    && mkdir -p /workspace && chown 1000:1000 /workspace
COPY --from=sandboxd-build /out/sandboxd /usr/local/bin/sandboxd
COPY --chmod=0755 reap /opt/ygg/reap
ENV HOME=/workspace LANG=C.UTF-8
USER 1000:1000
WORKDIR /workspace
EXPOSE 8080 9090
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/sandboxd"]
CMD ["--root-dir=/workspace"]

# Node.js comes from the official image (Debian's nodejs is EOL 18 and npm drags in hundreds of node-* debs).
FROM node:22-bookworm-slim AS node

FROM python:3.13-slim-bookworm

# LibreOffice: the -nogui writer/calc/impress trio is the smallest set that converts docx/xlsx/pptx to pdf.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tini bash git ca-certificates curl wget jq ripgrep procps util-linux coreutils file less \
        zip unzip xz-utils sqlite3 poppler-utils pandoc ffmpeg imagemagick \
        libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui \
        fontconfig fonts-noto-cjk fonts-noto-color-emoji fonts-dejavu-core \
        build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version

RUN pip install --no-cache-dir --root-user-action=ignore \
        numpy pandas scipy sympy matplotlib seaborn openpyxl xlrd pillow requests httpx \
        beautifulsoup4 lxml pyyaml tabulate python-docx python-pptx pypdf

# matplotlib: system rc (Agg, Noto Sans CJK) plus a prebuilt font cache that sitecustomize copies into the
# writable MPLCONFIGDIR on first use (root fs is read-only, HOME is an empty volume).
COPY matplotlibrc /etc/matplotlibrc
COPY sitecustomize.py /usr/local/lib/python3.13/site-packages/sitecustomize.py
RUN MATPLOTLIBRC=/etc/matplotlibrc MPLCONFIGDIR=/opt/ygg/mpl-cache python -c "import matplotlib.font_manager" \
    && find /opt/ygg/mpl-cache -mindepth 1 ! -name 'fontlist-*.json' -delete \
    && ls /opt/ygg/mpl-cache/fontlist-*.json \
    && chmod -R a+rX /opt/ygg

COPY --from=sandboxd-build /out/sandboxd /usr/local/bin/sandboxd
COPY --chmod=0755 reap /opt/ygg/reap
# A passwd entry for uid 1000 is required: LibreOffice refuses to create its user profile without one.
RUN useradd -u 1000 -U -d /workspace -M -s /bin/bash sandbox \
    && mkdir -p /workspace && chown 1000:1000 /workspace

ENV HOME=/workspace \
    LANG=C.UTF-8 \
    PIP_USER=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_INDEX_URL=https://pypi.org/simple \
    PYTHONUSERBASE=/workspace/.local \
    NPM_CONFIG_PREFIX=/workspace/.npm-global \
    NPM_CONFIG_REGISTRY=https://registry.npmjs.org/ \
    PATH=/workspace/.local/bin:/workspace/.npm-global/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin \
    MPLBACKEND=Agg \
    MATPLOTLIBRC=/etc/matplotlibrc \
    MPLCONFIGDIR=/workspace/.cache/matplotlib

USER 1000:1000
WORKDIR /workspace
EXPOSE 8080 9090
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/sandboxd"]
CMD ["--root-dir=/workspace"]
