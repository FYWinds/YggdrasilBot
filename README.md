# YggdrasilBot

QQ group-chat LLM bot (NoneBot2 + SnowLuma): TL;DR, replies, background memory.

## Dashboard

The admin API lives in `yggdrasil/admin` (mounted at `/api`, SPA from `YGG_WEB_DIST` at `/`); the React app in `web/`.

```sh
uv run uvicorn yggdrasil.admin.standalone:app --port 8192   # API without the bot (dev auth: YGG_DEV_ADMIN_EMAIL)
cd web && npm install && npm run dev                          # Vite proxies /api to :8192
uv run python -m yggdrasil.admin.dump_openapi > web/openapi.json && (cd web && npm run gen)  # regenerate the client
```
