# Deployment runbook

YggdrasilBot runs on the homelab k3s cluster, declared in
`FYWinds/k3s-gitops` (Argo CD, root ApplicationSet):

| Path (k3s-gitops) | What |
|---|---|
| `apps/yggdrasil/` | Namespace `yggdrasil`: ParadeDB (CNPG `yggdrasil-pg`), Redis, migration hook, bot / worker / scheduler, SnowLuma (NapCat kept at 0 replicas as rollback), TEI, llama.cpp rerank + guard, Playwright, SearXNG, NetworkPolicies, IngressRoutes, alerts, ImageUpdater, SealedSecrets |
| `observability/yggdrasil-dashboard.yaml` | Grafana dashboard (folder YggdrasilBot) |

Sync waves: data (Postgres, Redis) 0 → `alembic upgrade head` Sync hook 1 →
workloads 2. GitHub Actions (`.github/workflows/ci.yaml`) builds the image
`ghcr.io/fywinds/yggdrasil-bot`; one image serves every role:

| Role | Command |
|---|---|
| bot (default) | `python -m yggdrasil` (port 8080: `/onebot/v11/ws`, dashboard, `/healthz`, `/readyz`) |
| worker | `taskiq worker yggdrasil.worker.broker:broker yggdrasil.worker.tasks` |
| scheduler | `taskiq scheduler yggdrasil.worker.broker:scheduler yggdrasil.worker.tasks` |
| migration | `alembic upgrade head` |

## First-time setup

All `kubeseal` commands use
`--controller-name sealed-secrets-controller --controller-namespace kube-system`.

### 1. GHCR pull secret

The package is private (the repo is private). Create a classic GitHub PAT
with only `read:packages`, store it in 1Password as `GHCR-Pull` (field
`credential`), then seal it as `ghcr-pull` for both the pods (`yggdrasil`) and
argocd-image-updater (`argocd`) into `apps/yggdrasil/ghcr-pull-secret.yaml`:
a `kubernetes.io/dockerconfigjson` Secret with
`{"auths": {"ghcr.io": {"username": "FYWinds", "password": "<PAT>"}}}`,
piped through `kubeseal -o yaml` once per namespace.

### 2. App secrets

`apps/yggdrasil/secret.yaml` is the SealedSecret `yggdrasil-secrets`; keys are
documented in `apps/yggdrasil/secret.example.yaml`. The plaintext values live
in 1Password `YggdrasilBot-Prod-Secrets`. To rotate, edit that item and reseal:

```sh
kubectl create secret generic yggdrasil-secrets -n yggdrasil \
  --from-literal=ONEBOT_ACCESS_TOKEN=... --from-literal=YGG_SECRET_KEY=... \
  --from-literal=NAPCAT_WEBUI_TOKEN=... --from-literal=SEARXNG_SECRET=... \
  --dry-run=client -o yaml \
  | kubeseal --controller-name sealed-secrets-controller --controller-namespace kube-system -o yaml \
  > apps/yggdrasil/secret.yaml
```

Never rotate `YGG_SECRET_KEY` casually: provider API keys in the database are
encrypted with it and are unrecoverable without it.

Postgres credentials are not sealed: CNPG creates `yggdrasil-pg-app`
(`host`, `port`, `user`, `password`, `dbname`), which the pods read directly.

### 3. Build the first image

See [Build & release](#build--release). The kustomization floor tag is
`v0.1.0`; that tag must exist before the first sync can roll out.

### 4. Argo CD sync

Push k3s-gitops; the root ApplicationSet creates `apps-yggdrasil`. Order:
`yggdrasil-pg` + `yggdrasil-redis` → migration Job → workloads. TEI and the
llama.cpp pods download models on first start (startup probes allow 15 min).

### 5. SnowLuma QR login

SnowLuma runs Linux QQ in the `snowluma-0` pod; nothing is exposed outside
the pod (NetworkPolicy `yggdrasil-snowluma`), so use port-forwards:

```bash
kubectl -n yggdrasil logs snowluma-0 -c snowluma | grep -E "远程桌面密码|临时密码"
kubectl -n yggdrasil port-forward snowluma-0 6081:6081 5099:5099
```

Open http://127.0.0.1:6081/vnc.html with the remote-desktop password (printed
once, on the first start of an empty data volume) and scan the QR code in the
QQ window with the prod bot account **2140292435**. The WebUI
(http://127.0.0.1:5099, `admin` + the one-time password from the log) asks
to accept SnowLuma's EULA/PRIVACY and to change the password on first login;
keep the new password in 1Password.

The `onebot-config` initContainer rewrites `config/onebot.json` and every
`config/onebot_<uin>.json` on each start: one wsClients entry
(`ws://yggdrasil-bot.yggdrasil.svc:8080/onebot/v11/ws`, token
`ONEBOT_ACCESS_TOKEN`, array format, no self messages), no HTTP/WS listeners,
the built-in `#sl` status reply off. Do not edit the OneBot config in the
WebUI (it is reset on restart). It also pins the machine-id; with the pinned
MAC and hostname that keeps QQ on one device, so restarts reuse the session on
the three PVCs.

SnowLuma only serves group history once it has seen a message in that group,
so the reconnect backfill after a fresh start finds nothing: trigger
「回填」 on the Groups page once each group has new messages.

### 6. Seed providers and routes

Open https://ygg.fyw.fyi (login as `i@fywinds.com`, bootstrap admin). In the
dashboard:

1. Providers: add the LLM endpoint(s) and the in-cluster inference services:
   - kami (new-api) `https://kami.fyw.fyi/v1` with format **anthropic**: its
     OpenAI endpoint rejects tool calls for Claude models.
   - embeddings `http://yggdrasil-tei.yggdrasil.svc:8080/v1` (`qwen3-embedding-0.6b`)
   - rerank `http://yggdrasil-rerank.yggdrasil.svc:8080/v1` (`qwen3-reranker-0.6b`)
   - guard `http://yggdrasil-guard.yggdrasil.svc:8080/v1` (`qwen3guard-gen-0.6b`)
2. Routes: map every task to a provider/model; activate the embed space.
3. Groups: enable the target QQ groups; adjust settings/triggers.

## Build & release

`.github/workflows/ci.yaml` runs ruff, basedpyright and pytest (ParadeDB and
Redis service containers) on every push and PR. Pushes to `main` also push
`sha-<short>` and `main` images; tags `vX.Y.Z` push `vX.Y.Z`.
`ghcr-cleanup.yaml` prunes old `sha-*` images weekly (keeps 5).

Release:

1. Merge to `main`, then `git tag vX.Y.Z && git push origin vX.Y.Z`. Do not bump
   `version` in `pyproject.toml`: it stays `0.0.0` so `uv.lock` (and the cached
   dependency layer) only change with dependencies; the tag reaches the image as
   `YGG_VERSION` (telemetry `service.version`).
2. argocd-image-updater (polls every 30s) picks the highest `^v\d+\.\d+\.\d+$` tag
   and writes it onto the `apps-yggdrasil` Application; the sync runs
   migrations first, then recreates the bot.

Test builds: every `main` push produces `sha-<short>`; pin it manually (see
Rollback) to try it without a release.

## Rollback

- **Image**: `argocd app set apps-yggdrasil --kustomize-image ghcr.io/fywinds/yggdrasil-bot:vX.Y.(Z-1)`
  pins an older tag, but the updater moves forward again on its next run.
  For a lasting rollback, delete the bad release tag in GHCR
  (Packages → yggdrasil-bot), or ship a fixed `vX.Y.(Z+1)`.
- **Schema**: migrations run forward only in the hook. If the older image
  cannot run against the new schema, downgrade while the new image is still
  running, then roll back the image:
  `kubectl -n yggdrasil exec deploy/yggdrasil-bot -- alembic downgrade <revision>`.
- **Data**: CNPG takes a daily volume snapshot (`yggdrasil-pg-daily`, 03:15).
  Restore by creating a new Cluster with `bootstrap.recovery.volumeSnapshots`
  pointing at the chosen `Backup`'s snapshot, then switch the app to it.

## Alerts

`apps/yggdrasil/prometheusrule.yaml`: QQ endpoint disconnected (5m), heartbeat
stale (>120s), invocation failure ratio (>20% over 15m), memory lag (>1000 for
1h), pending reviews (90m), partition runway (<1 month).
