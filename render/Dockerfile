# Render service: Playwright browser server (:3000, for read_url) + Markdown/Mermaid/HTML render API (:8080).
# The Playwright version here must match playwright in package.json and the bot's Python playwright.
FROM node:22-bookworm-slim AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY build.mjs tsconfig.json ./
COPY src ./src
RUN npm run typecheck && npm run build && npm prune --omit=dev

FROM mcr.microsoft.com/playwright:v1.63.0-noble
# System fonts used by the render page: Noto Sans CJK (all weights), color emoji, JetBrains Mono.
# WenQuanYi (single weight, no bold) is removed so nothing falls back to it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-noto-cjk fonts-noto-cjk-extra fonts-noto-color-emoji fonts-jetbrains-mono \
    && (apt-get purge -y fonts-wqy-zenhei || true) \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f
WORKDIR /app
COPY --from=build /src/package.json ./
COPY --from=build /src/node_modules ./node_modules
COPY --from=build /src/dist ./dist
# Read-only root filesystem: Chromium profiles, fontconfig cache and other scratch go to /tmp.
ENV HOME=/tmp NODE_ENV=production
USER pwuser
EXPOSE 3000 8080
STOPSIGNAL SIGTERM
CMD ["node", "dist/server.js"]
