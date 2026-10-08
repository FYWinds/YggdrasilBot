# Deployment runbook

YggdrasilBot runs on the homelab k3s cluster, declared in
`FYWinds/k3s-gitops` (Argo CD, root ApplicationSet):

| Path (k3s-gitops) | What |
|---|---|
| `apps/yggdrasil/` | Namespace `yggdrasil`: ParadeDB (CNPG `yggdrasil-pg`), Redis, migration hook, bot / worker / scheduler, NapCat, TEI, llama.cpp rerank + guard, Playwright, SearXNG, NetworkPolicies, IngressRoutes, alerts, ImageUpdater |
| `infra/buildkit/` | Rootless BuildKit used by `scripts/build.ps1` |
| `apps/gitea/service-registry-node.yaml` | NodePort 30500 to Gitea for node-local image pulls |
| `infra/argocd-image-updater/application.yaml` | Gitea registry entry (`registry.fyw.fyi`) |
| `observability/yggdrasil-dashboard.yaml` | Grafana dashboard (folder YggdrasilBot) |

Sync waves: data (Postgres, Redis) 0 → `alembic upgrade head` Sync hook 1 →
workloads 2. The image is `registry.fyw.fyi/fywinds/yggdrasil-bot`; one image
serves every role:

| Role | Command |
|---|---|
| bot (default) | `python -m yggdrasil` (port 8080: `/onebot/v11/ws`, dashboard, `/healthz`, `/readyz`) |
| worker | `taskiq worker yggdrasil.worker.broker:broker yggdrasil.worker.tasks` |
| scheduler | `taskiq scheduler yggdrasil.worker.broker:scheduler` |
| migration | `alembic upgrade head` |

## First-time setup

All `kubeseal` commands use
`--controller-name sealed-secrets-controller --controller-namespace kube-system`.

### 1. Gitea pull mirror

In Gitea (git.fyw.fyi) create a **pull mirror** `FYWinds/YggdrasilBot` of
`https://github.com/FYWinds/YggdrasilBot` (New Migration → GitHub, tick
"This repository will be a mirror"; for a private GitHub repo give a
read-only GitHub token). BuildKit clones the build context from
`http://gitea-http.gitea.svc:3000/FYWinds/YggdrasilBot.git`, so the mirror
must be readable anonymously in-cluster or be public.

Create a Gitea access token with `write:repository` (mirror-sync) and
`write:package` + `read:package` (registry) scopes.

### 2. BuildKit certificates

Follow `infra/buildkit/README.md` in k3s-gitops: generate CA/daemon/client
certs, seal the daemon certs to `infra/buildkit/certs-sealed.yaml`, uncomment
it in `infra/buildkit/kustomization.yaml`, push. Keep the client certs in
`~/.config/buildkit/client/` (`ca.pem`, `cert.pem`, `key.pem`).

### 3. App secrets

**TODO (integrator): generate `apps/yggdrasil/secret.yaml` and enable it in
`apps/yggdrasil/kustomization.yaml`** (it is commented out until it exists so
the kustomize build stays green). Keys are documented in
`apps/yggdrasil/secret.example.yaml`:

```sh
kubectl create secret generic yggdrasil-secrets -n yggdrasil \
  --from-literal=ONEBOT_ACCESS_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=YGG_SECRET_KEY="$(uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
  --from-literal=NAPCAT_WEBUI_TOKEN="$(openssl rand -hex 16)" \
  --from-literal=SEARXNG_SECRET="$(openssl rand -hex 32)" \
  --dry-run=client -o yaml \
  | kubeseal --controller-name sealed-secrets-controller --controller-namespace kube-system -o yaml \
  > apps/yggdrasil/secret.yaml
```

Keep a copy of `YGG_SECRET_KEY` in the password manager: provider API keys in
the database are encrypted with it and are unrecoverable without it.

Postgres credentials are not sealed: CNPG creates `yggdrasil-pg-app`
(`host`, `port`, `user`, `password`, `dbname`), which the pods read directly.

### 4. Registry pulls on the k3s node

Images are pushed in-cluster to `gitea-http.gitea.svc:3000/fywinds/...` and
referenced as `registry.fyw.fyi/fywinds/...` (same Gitea registry). Point the
node's containerd at the NodePort so pulls never leave the node. On each k3s
node, `/etc/rancher/k3s/registries.yaml`:

```yaml
mirrors:
  registry.fyw.fyi:
    endpoint:
      - "http://127.0.0.1:30500"
configs:
  "127.0.0.1:30500":
    # Only needed while the package is private.
    auth:
      username: FYWinds
      password: <gitea token with read:package>
```

then `sudo systemctl restart k3s` (agents: `k3s-agent`). If the package stays
private, argocd-image-updater also needs credentials: add
`credentials: secret:argocd/gitea-registry#creds` (sealed `user:token`) to the
Gitea registry entry. Making the `yggdrasil-bot` package public in Gitea avoids
both.

### 5. Build the first image

See [Build & release](#build--release). The kustomization floor tag is
`v0.1.0`; that tag must exist before the first sync can roll out.

### 6. Argo CD sync

Push k3s-gitops; the root ApplicationSet creates `apps-yggdrasil`. Order:
`yggdrasil-pg` + `yggdrasil-redis` → migration Job → workloads. TEI and the
llama.cpp pods download models on first start (startup probes allow 15 min).

### 7. NapCat QR login

Open https://napcat.fyw.fyi (oauth2-proxy, then the WebUI token
`NAPCAT_WEBUI_TOKEN`) and scan the QR code with the bot's QQ account.

The `onebot-config` initContainer writes `/app/napcat/config/onebot11.json`
with a websocketClients entry (`ws://yggdrasil-bot.yggdrasil.svc:8080/onebot/v11/ws`,
token `ONEBOT_ACCESS_TOKEN`, array post format, no self messages, 30s
heartbeat) and overwrites every existing `onebot11_<uin>.json` on each start.
NapCat seeds `onebot11_<uin>.json` from `onebot11.json` on an account's first
login, so after the first QR login restart the pod once
(`kubectl -n yggdrasil delete pod napcat-0`) if the bot does not connect, and
do not edit the OneBot network config in the WebUI (it is reset on restart).
QQ data and the login session persist on the `qq-data` PVC.

### 8. Seed providers and routes

Open https://ygg.fyw.fyi (login as `i@fywinds.com`, bootstrap admin). In the
dashboard:

1. Providers: add the LLM endpoint(s) and the in-cluster inference services:
   - embeddings `http://yggdrasil-tei.yggdrasil.svc:8080/v1` (`qwen3-embedding-0.6b`)
   - rerank `http://yggdrasil-rerank.yggdrasil.svc:8080/v1` (`qwen3-reranker-0.6b`)
   - guard `http://yggdrasil-guard.yggdrasil.svc:8080/v1` (`qwen3guard-gen-0.6b`)
2. Routes: map every task to a provider/model; activate the embed space.
3. Groups: enable the target QQ groups; adjust settings/triggers.

## Build & release

Local tools: `kubectl` (cluster kubeconfig), `git`, `buildctl` from a
[moby/buildkit release](https://github.com/moby/buildkit/releases)
(`bin/buildctl.exe` on PATH). Environment:

- `GITEA_TOKEN`: Gitea token (`write:repository`) for mirror-sync.
- `DOCKER_CONFIG`: directory with `config.json` that buildctl forwards to
  buildkitd for the push:

  ```json
  {"auths": {"gitea-http.gitea.svc:3000": {"auth": "<base64 of FYWinds:<token with write:package>>"}}}
  ```

Release:

1. Bump `version` in `pyproject.toml`, merge to `main` on GitHub, tag `vX.Y.Z`.
2. `./scripts/build.ps1 -Ref vX.Y.Z` — syncs the Gitea mirror, port-forwards
   `buildkit/svc/buildkitd`, builds from the Gitea clone at that commit and
   pushes `vX.Y.Z` and `sha-<short>`.
3. argocd-image-updater picks the highest `^v\d+\.\d+\.\d+$` tag (within a few
   minutes) and writes it onto the `apps-yggdrasil` Application; the sync runs
   migrations first, then recreates the bot.

Test builds: `./scripts/build.ps1 -Tag dev-foo` pushes without affecting prod
(only release tags are considered by the updater).

## Rollback

- **Image**: `argocd app set apps-yggdrasil --kustomize-image registry.fyw.fyi/fywinds/yggdrasil-bot:vX.Y.(Z-1)`
  pins an older tag, but the updater moves forward again on its next run.
  For a lasting rollback, delete or retag the bad release in Gitea
  (Packages → yggdrasil-bot), or ship a fixed `vX.Y.(Z+1)`.
- **Schema**: migrations run forward only in the hook. If the older image
  cannot run against the new schema, downgrade while the new image is still
  running, then roll back the image:
  `kubectl -n yggdrasil exec deploy/yggdrasil-bot -- alembic downgrade <revision>`.
- **Data**: CNPG takes a daily volume snapshot (`yggdrasil-pg-daily`, 03:15).
  Restore by creating a new Cluster with `bootstrap.recovery.volumeSnapshots`
  pointing at the chosen `Backup`'s snapshot, then switch the app to it.

## Alerts

`apps/yggdrasil/prometheusrule.yaml`: NapCat disconnected (5m), heartbeat
stale (>120s), invocation failure ratio (>20% over 15m), memory lag (>1000 for
1h), pending reviews (90m), partition runway (<1 month).
