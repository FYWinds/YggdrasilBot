"""The workspaces' Kubernetes objects: a SandboxClaim from the warm pool per workspace pod, and the Service plus
IngressRoute behind each public link, all labelled as the bot's."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from yggdrasil.core.log import LOGGER
from yggdrasil.sandbox.errors import SandboxError
from yggdrasil.sandbox.k8s import KubeClient
from yggdrasil.sandbox.state import link_name

_LABELS = {"app.kubernetes.io/part-of": "yggdrasil", "app.kubernetes.io/component": "sandbox"}
_LABEL_SELECTOR = ",".join(f"{k}={v}" for k, v in _LABELS.items())
_GROUP_LABEL = "yggdrasil/group"
_NAME_LABEL = "yggdrasil/workspace"
_WORKSPACE_LABEL = "sandbox.users.io/workspace"
"""Pod label carrying the claim name; link Services select on it."""


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def claim_ready(claim: dict[str, Any]) -> tuple[bool, str | None, str | None, str | None]:
    """(ready, pod ip, sandbox name, terminal failure reason) from a SandboxClaim object."""
    status = claim.get("status") or {}
    sandbox = status.get("sandbox") or {}
    ips = [ip for ip in sandbox.get("podIPs") or [] if ip]
    name = sandbox.get("name") or None
    ready = False
    failure = None
    for cond in status.get("conditions") or []:
        if cond.get("type") == "Ready":
            ready = cond.get("status") == "True"
            if not ready and cond.get("reason") in {"PodFailed", "PodSucceeded", "TemplateNotFound", "SandboxExpired"}:
                failure = str(cond.get("reason"))
    return ready and bool(ips), (ips[0] if ips else None), name, failure


def _owner(sandbox: dict[str, Any]) -> list[dict[str, Any]]:
    meta = sandbox.get("metadata") or {}
    return [
        {
            "apiVersion": "agents.x-k8s.io/v1beta1",
            "kind": "Sandbox",
            "name": meta.get("name"),
            "uid": meta.get("uid"),
            "blockOwnerDeletion": False,
        }
    ]


class Cluster:
    """The bot's objects in one namespace; claims take their pods from warm pool ``warmpool``. Without ``kube`` the
    in-cluster client is built on first use."""

    def __init__(self, kube: KubeClient | None, *, namespace: str, warmpool: str) -> None:
        self._kube_client = kube
        self._ns = namespace
        self._warmpool = warmpool

    def _kube(self) -> KubeClient:
        if self._kube_client is None:
            self._kube_client = KubeClient()
        return self._kube_client

    # --- paths ------------------------------------------------------------------------------------------

    def _claim_path(self, name: str = "") -> str:
        base = f"/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/{self._ns}/sandboxclaims"
        return f"{base}/{name}" if name else base

    def _sandbox_path(self, name: str) -> str:
        return f"/apis/agents.x-k8s.io/v1beta1/namespaces/{self._ns}/sandboxes/{name}"

    def _service_path(self, name: str = "") -> str:
        base = f"/api/v1/namespaces/{self._ns}/services"
        return f"{base}/{name}" if name else base

    def _route_path(self, name: str = "") -> str:
        base = f"/apis/traefik.io/v1alpha1/namespaces/{self._ns}/ingressroutes"
        return f"{base}/{name}" if name else base

    # --- claims -----------------------------------------------------------------------------------------

    async def claim(self, name: str) -> dict[str, Any] | None:
        """The SandboxClaim ``name``; ``None`` when it is gone."""
        return await self._kube().get(self._claim_path(name))

    async def live_claims(self, group_id: int | None = None) -> set[str]:
        """The names of the bot's claims (of one group)."""
        selector = _LABEL_SELECTOR if group_id is None else f"{_LABEL_SELECTOR},{_GROUP_LABEL}={group_id}"
        items = await self._kube().list(self._claim_path(), label_selector=selector)
        return {str(i.get("metadata", {}).get("name")) for i in items}

    async def create_claim(self, name: str, group_id: int, workspace: str, *, shutdown: float) -> None:
        """Claim a pod for workspace ``workspace`` of ``group_id``; the controller deletes it at ``shutdown``
        unless renewed (``set_shutdown``)."""
        labels = {**_LABELS, _GROUP_LABEL: str(group_id), _NAME_LABEL: workspace}
        body = {
            "apiVersion": "extensions.agents.x-k8s.io/v1beta1",
            "kind": "SandboxClaim",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                "warmPoolRef": {"name": self._warmpool},
                "lifecycle": {"shutdownTime": _rfc3339(shutdown), "shutdownPolicy": "Delete"},
                "additionalPodMetadata": {"labels": {_WORKSPACE_LABEL: name}},
            },
        }
        await self._kube().create(self._claim_path(), body)

    async def delete_claim(self, name: str) -> bool:
        """Delete the claim and with it its pod; False when it was gone already."""
        return await self._kube().delete(self._claim_path(name))

    async def set_shutdown(self, name: str, at: float) -> bool:
        """Move the claim's shutdownTime to ``at``; False when the claim is gone."""
        patched = await self._kube().merge_patch(
            self._claim_path(name), {"spec": {"lifecycle": {"shutdownTime": _rfc3339(at)}}}
        )
        return patched is not None

    # --- links ------------------------------------------------------------------------------------------

    async def create_link(
        self, *, group_id: int, workspace: str, claim_id: str, sandbox: str | None, rid: str, port: int, domain: str
    ) -> None:
        """Serve ``port`` of claim ``claim_id``'s pod at ``https://<link_name(rid)>.<domain>``: a Service and an
        IngressRoute owned by the pod's Sandbox (``sandbox``: its name when known), so they go with it."""
        sandbox_name = sandbox
        if not sandbox_name:
            claim = await self.claim(claim_id)
            sandbox_name = claim_ready(claim)[2] if claim else None
        found = await self._kube().get(self._sandbox_path(sandbox_name)) if sandbox_name else None
        if found is None:
            raise SandboxError("找不到工作区对应的沙箱对象，没法建立公开链接。")
        owners = _owner(found)
        name = link_name(rid)
        labels = {**_LABELS, _GROUP_LABEL: str(group_id), _NAME_LABEL: workspace}
        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "labels": labels, "ownerReferences": owners},
            "spec": {
                "selector": {_WORKSPACE_LABEL: claim_id},
                "ports": [{"name": "http", "port": 80, "targetPort": port, "protocol": "TCP"}],
            },
        }
        route = {
            "apiVersion": "traefik.io/v1alpha1",
            "kind": "IngressRoute",
            "metadata": {"name": name, "labels": labels, "ownerReferences": owners},
            "spec": {
                "entryPoints": ["websecure"],
                "routes": [
                    {"kind": "Rule", "match": f"Host(`{name}.{domain}`)", "services": [{"name": name, "port": 80}]}
                ],
            },
        }
        await self._kube().create(self._service_path(), service)
        try:
            await self._kube().create(self._route_path(), route)
        except BaseException:
            await asyncio.shield(self.delete_link(rid))
            raise

    async def delete_link(self, rid: str) -> None:
        """Remove a link's IngressRoute and Service; failures are logged, not raised."""
        name = link_name(rid)
        for path in (self._route_path(name), self._service_path(name)):
            try:
                await self._kube().delete(path)
            except Exception:
                LOGGER.exception("deleting {} failed", path)
