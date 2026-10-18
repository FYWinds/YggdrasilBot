# Deployment

This guide shows how to run YggdrasilBot on Kubernetes with Argo CD. Names, domains and accounts below are
placeholders: replace `example.com`, `<qq-account>`, `<admin-email>` and friends with your own.

## What you need

| Component | Purpose | Notes |
|---|---|---|
| PostgreSQL with ParadeDB (`pg_search`, `vector`) | messages, memory, settings, audit | e.g. CloudNativePG with the `paradedb/paradedb` image |
| Redis 7+ | rate limits, queues, sandbox mappings, config pub/sub | |
| A OneBot v11 endpoint | the QQ connection | SnowLuma or NapCat, configured as a **reverse WebSocket client** to the bot |
| An OpenAI- or Anthropic-compatible LLM endpoint | chat, tldr, memory, ops | configured in the dashboard |
| Embedding endpoint (OpenAI-compatible) | memory search | e.g. llama.cpp serving a Qwen3 embedding GGUF |
| Optional: guard model endpoint | input/output moderation | e.g. llama.cpp serving Qwen3Guard |
| Optional: Playwright server | rendering, browser fallback for `read_url` | `YGG_BROWSER_WS_URL` |
| Optional: SearXNG | web search | configured in the dashboard |
| Optional: agent-sandbox + gVisor | the sandbox workspace tools | see [Sandbox](#sandbox) |

## Images and roles

CI (`.github/workflows/ci.yaml`) builds two images:

- `ghcr.io/<owner>/yggdrasil-bot`: one image for every role.
- `ghcr.io/<owner>/yggdrasil-sandbox`: the sandbox workspace (only needed with the sandbox).

| Role | Command |
|---|---|
| bot (default) | `python -m yggdrasil` (port 8080: `/onebot/v11/ws`, dashboard and `/api`, `/healthz`, `/readyz`, `/drainz`) |
| worker | `taskiq worker yggdrasil.worker.broker:broker yggdrasil.worker.tasks` |
| scheduler | `taskiq scheduler yggdrasil.worker.broker:scheduler yggdrasil.worker.tasks` |
| migration | `alembic upgrade head` |

Run one bot replica: it owns the OneBot connection. The bot and worker wait up to 5 minutes for Postgres and
Redis at startup.

## Configuration

Environment variables (see `yggdrasil/config.py` and `.env.example` for the full list):

| Variable | Required | Meaning |
|---|---|---|
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | yes | database |
| `REDIS_URL` | yes | e.g. `redis://redis:6379/0` |
| `YGG_SECRET_KEY` | yes | encrypts provider API keys in the database; keep it safe, it cannot be rotated without losing them |
| `ONEBOT_ACCESS_TOKEN` | yes | shared with the OneBot endpoint |
| `YGG_SUPERUSERS` | yes | QQ numbers allowed to use ops commands |
| `YGG_ADMIN_EMAILS` | yes | dashboard admins; the dashboard trusts `X-Auth-Request-Email` from your auth proxy |
| `YGG_ENV` | no | `prod` / `dev`, used in telemetry |
| `YGG_BROWSER_WS_URL` | no | Playwright server, e.g. `ws://browser:3000/` |
| `YGG_GRAFANA_URL` | no | links invocations to traces in the dashboard |
| `OTEL_EXPORTER_OTLP_ENDPOINT` etc. | no | OpenTelemetry traces, metrics and logs |
| `LOG_JSON` | no | `true` for JSON logs |
| `YGG_SANDBOX_TOKEN` | sandbox | shared sandboxd bearer token; the sandbox is off when unset |
| `YGG_SANDBOX_NAMESPACE`, `YGG_SANDBOX_WARMPOOL` | sandbox | defaults `yggdrasil`, `yggdrasil-sandbox` |
| `YGG_SANDBOX_LINK_DOMAIN` | sandbox | wildcard domain for public links (`https://<id>.<domain>`); `serve` is off when unset |

Everything else (providers, model routes, groups, triggers, prompts, moderation, tool settings) lives in the
database and is edited in the dashboard.

## Example: Kubernetes + Argo CD

Lay the manifests out in a GitOps repository, one directory per Argo CD Application:

```
apps/yggdrasil/
  namespace.yaml
  postgres.yaml          # CNPG Cluster (ParadeDB image) + backups
  redis.yaml
  migration.yaml         # Job running `alembic upgrade head`, Argo Sync hook
  bot.yaml               # Deployment + Service (8080)
  worker.yaml
  scheduler.yaml
  onebot.yaml            # SnowLuma or NapCat StatefulSet
  embed.yaml, guard.yaml # optional llama.cpp Deployments
  browser.yaml           # optional Playwright server
  networkpolicy.yaml
  ingressroute.yaml      # dashboard behind your auth proxy; never expose /onebot
  secret.yaml            # SealedSecret (or any secret manager)
  rbac.yaml, sandbox.yaml  # sandbox only
  kustomization.yaml
infra/agent-sandbox/     # sandbox only: controller
infra/gvisor/            # sandbox only: RuntimeClass
```

Order the sync with waves: data (Postgres, Redis) `0`, migration hook `1`, workloads `2`.

Pod hardening that the images support: run as non-root, `readOnlyRootFilesystem: true` with an `emptyDir` on
`/tmp`, drop all capabilities, `seccompProfile: RuntimeDefault`. Give the bot a `preStop` hook calling
`/drainz?max_wait=170` and `terminationGracePeriodSeconds: 200` so in-flight replies finish.

### First-time setup

1. **Secrets.** Create the app secret with the variables above. Postgres credentials can come straight from the
   operator's generated secret (CNPG creates `<cluster>-app`).
2. **Image.** Push a release tag (see [Releases](#releases)) so the image exists before the first sync. If the
   package is private, add an image pull secret.
3. **Sync.** Let Argo CD create the namespace, databases, run the migration and start the workloads.
4. **OneBot login.** Log the QQ account in on the OneBot endpoint (SnowLuma/NapCat show a QR code in their web
   UI or VNC). Point its reverse WebSocket client at `ws://<bot-service>:8080/onebot/v11/ws` with
   `ONEBOT_ACCESS_TOKEN`, array message format, and self messages off. Keep its web UI off the public internet.
5. **Dashboard.** Open the dashboard as an admin and:
   1. add providers (LLM, embedding, optional guard);
   2. map every task route to a provider/model and activate an embedding space;
   3. enable groups and adjust triggers and settings.

### Releases

- Pushes to `main` publish `sha-<short>` and `main` image tags; tags `vX.Y.Z` publish `vX.Y.Z`.
- `pyproject.toml` stays at `0.0.0`; the git tag reaches the image as `YGG_VERSION`.
- With argocd-image-updater, track `^v\d+\.\d+\.\d+$` for both images; a sync runs the migration first.

### Rollback

- **Image:** pin the previous tag on the Application (or delete the bad tag from the registry so the updater does
  not move forward again).
- **Schema:** migrations only run forward. If the older image cannot run against the new schema, run
  `alembic downgrade <revision>` from the new image first, then roll the image back.
- **Data:** restore from your Postgres backups (e.g. CNPG volume snapshots into a new Cluster).

## Sandbox

The sandbox gives the chat model a persistent, per-group Linux workspace (shell, files, optional public links).
It needs:

1. **gVisor** on the nodes: install `runsc` + `containerd-shim-runsc-v1`, register the `runsc` runtime with
   containerd (on k3s via a drop-in under `/var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.d/`), and
   create `RuntimeClass gvisor` with `handler: runsc`.
2. **kubernetes-sigs/agent-sandbox** v1.0.7 with extensions (`sandbox-with-extensions.yaml` release asset).
3. A `SandboxTemplate` + `SandboxWarmPool` named `yggdrasil-sandbox` in the bot's namespace using the sandbox
   image, `runtimeClassName: gvisor`, uid 1000, read-only root fs, `emptyDir` on `/workspace` and `/tmp`, args
   `--root-dir=/workspace --auth-token-sha256=<sha256 of YGG_SANDBOX_TOKEN>`. Set `spec.networkPolicy`
   explicitly (without it the controller's default policy applies): allow ingress from the bot pods on 8080/9090
   (and from your ingress controller on the app ports if you use public links), allow egress only to the public
   internet (exclude cluster, private, link-local and your own public ranges) and to public DNS resolvers with
   `dnsPolicy: None`.
4. **RBAC** for the bot's ServiceAccount in its namespace: `sandboxclaims` (create, get, list, watch, patch,
   delete), `sandboxes` (get, list, watch), and for public links `services` and Traefik `ingressroutes` (create,
   get, list, delete).
5. **Public links (optional):** a wildcard DNS record `*.<domain>` to your ingress, a matching certificate, and
   `YGG_SANDBOX_LINK_DOMAIN=<domain>`. Use a domain that hosts nothing else: pages served from sandboxes are
   untrusted.
6. Enable the `sandbox` tool for chat in the dashboard (tool settings).

The dashboard's Sandbox page lists live workspaces and can destroy them; `tools.sandbox_enabled` in the global
settings switches the whole feature off.
