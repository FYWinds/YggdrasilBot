# YggdrasilBot sandbox image: per-QQ-group workspace pod (gVisor) running sandboxd under tini.
# Build context: sandbox/. Targets:
#   sandboxd-build  static sandboxd binary built from kubernetes-sigs/agent-sandbox (no release binary exists)
#   sandboxd-test   CI test image: sandboxd, Python with ipykernel and the /opt/ygg helpers; no Chromium, omp or
#                   LibreOffice
#   (default)       full workspace image
# Runtime: uid 1000, read-only root fs, HOME=/workspace (empty volume), /tmp writable. Everything a program
# writes must go under /workspace (helper scratch: /workspace/.ygg) or /tmp.

ARG SANDBOXD_VERSION=v1.0.7
ARG PYTHON_IMAGE=python:3.13-slim-bookworm
ARG IPYKERNEL_VERSION=7.4.0
ARG JUPYTER_CLIENT_VERSION=8.10.0
ARG MATPLOTLIB_INLINE_VERSION=0.2.2
ARG PLAYWRIGHT_VERSION=1.64.0
ARG OMP_VERSION=v18.12.2
ARG OMP_SHA256=9513ef9f19d7991aac6ae0cb6077bcf16662092ddde861c002a91cedc6f47f9a

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

# CI test image: what the helper and conformance tests need, nothing heavier.
FROM ${PYTHON_IMAGE} AS sandboxd-test
ARG IPYKERNEL_VERSION
ARG JUPYTER_CLIENT_VERSION
ARG MATPLOTLIB_INLINE_VERSION
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini bash coreutils util-linux procps ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 1000 -U -d /workspace -M -s /bin/bash sandbox \
    && mkdir -p /workspace && chown 1000:1000 /workspace
RUN pip install --no-cache-dir --root-user-action=ignore \
        "ipykernel==${IPYKERNEL_VERSION}" "jupyter_client==${JUPYTER_CLIENT_VERSION}" \
        "matplotlib-inline==${MATPLOTLIB_INLINE_VERSION}"
COPY --from=sandboxd-build /out/sandboxd /usr/local/bin/sandboxd
COPY --chmod=0755 reap /opt/ygg/reap
COPY --chmod=0755 bin/ygg /opt/ygg/ygg
COPY lib/ygg /opt/ygg/lib/ygg
RUN echo /opt/ygg/lib > "$(python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))')/ygg.pth" \
    && python3 -m compileall -q /opt/ygg/lib \
    && chmod -R a+rX /opt/ygg
ENV HOME=/workspace LANG=C.UTF-8
USER 1000:1000
WORKDIR /workspace
EXPOSE 8080 9090
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/sandboxd"]
CMD ["--root-dir=/workspace"]

# Node.js comes from the official image (Debian's nodejs is EOL 18 and npm drags in hundreds of node-* debs).
FROM node:22-bookworm-slim AS node

FROM ${PYTHON_IMAGE}
ARG IPYKERNEL_VERSION
ARG JUPYTER_CLIENT_VERSION
ARG MATPLOTLIB_INLINE_VERSION
ARG PLAYWRIGHT_VERSION
ARG OMP_VERSION
ARG OMP_SHA256

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
        beautifulsoup4 lxml pyyaml tabulate python-docx python-pptx pypdf \
        "ipykernel==${IPYKERNEL_VERSION}" "jupyter_client==${JUPYTER_CLIENT_VERSION}" \
        "matplotlib-inline==${MATPLOTLIB_INLINE_VERSION}" "playwright==${PLAYWRIGHT_VERSION}"

# Playwright's bundled full Chromium (no headless shell: `ygg.browser()` uses channel "chromium", and omp's
# puppeteer wants the full build) plus its system libraries. /opt/ygg/chromium is a stable path to the binary.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ygg/ms-playwright
RUN playwright install --with-deps --no-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s "$(ls -d /opt/ygg/ms-playwright/chromium-*/chrome-linux*/chrome)" /opt/ygg/chromium \
    && /opt/ygg/chromium --version \
    && chmod -R a+rX /opt/ygg/ms-playwright

# omp (oh-my-pi) coding agent for background tasks: pinned standalone binary, checksum verified.
RUN curl -fsSL -o /usr/local/bin/omp "https://github.com/can1357/oh-my-pi/releases/download/${OMP_VERSION}/omp-linux-x64" \
    && echo "${OMP_SHA256}  /usr/local/bin/omp" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/omp \
    && HOME=/tmp/omp-home /usr/local/bin/omp --version \
    && rm -rf /tmp/omp-home
COPY omp.yml /etc/ygg/omp.yml

# matplotlib: system rc (Agg, Noto Sans CJK) plus a prebuilt font cache that sitecustomize copies into the
# writable MPLCONFIGDIR on first use (root fs is read-only, HOME is an empty volume).
COPY matplotlibrc /etc/matplotlibrc
COPY sitecustomize.py /usr/local/lib/python3.13/site-packages/sitecustomize.py
RUN MATPLOTLIBRC=/etc/matplotlibrc MPLCONFIGDIR=/opt/ygg/mpl-cache python -c "import matplotlib.font_manager" \
    && find /opt/ygg/mpl-cache -mindepth 1 ! -name 'fontlist-*.json' -delete \
    && ls /opt/ygg/mpl-cache/fontlist-*.json \
    && chmod -R a+rX /opt/ygg/mpl-cache

COPY --from=sandboxd-build /out/sandboxd /usr/local/bin/sandboxd
# A passwd entry for uid 1000 is required: LibreOffice refuses to create its user profile without one.
RUN useradd -u 1000 -U -d /workspace -M -s /bin/bash sandbox \
    && mkdir -p /workspace && chown 1000:1000 /workspace

COPY --chmod=0755 reap /opt/ygg/reap
COPY --chmod=0755 bin/ygg /opt/ygg/ygg
COPY lib/ygg /opt/ygg/lib/ygg
RUN echo /opt/ygg/lib > /usr/local/lib/python3.13/site-packages/ygg.pth \
    && python3 -m compileall -q /opt/ygg/lib \
    && chmod -R a+rX /opt/ygg/lib

# omp state lives in /workspace/.ygg/omp: PI_CONFIG_DIR is relative to HOME (agent dir, sessions, models.yml);
# the native addon would still be unpacked into ~/.omp/natives without the absolute PI_NATIVES_DIR.
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
    MPLCONFIGDIR=/workspace/.cache/matplotlib \
    PUPPETEER_EXECUTABLE_PATH=/opt/ygg/chromium \
    PI_CONFIG_DIR=.ygg/omp \
    PI_NATIVES_DIR=/workspace/.ygg/omp/natives \
    PI_AUTO_QA=0 \
    PI_BROWSER_RELAY=0

USER 1000:1000
WORKDIR /workspace
EXPOSE 8080 9090
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/sandboxd"]
CMD ["--root-dir=/workspace"]
