"""Minimal async client for the in-cluster Kubernetes API (JSON get/list/create/merge-patch/delete).

Authenticates with the pod's ServiceAccount token, re-read periodically because projected tokens rotate.
"""

from __future__ import annotations

import ssl
import time
from pathlib import Path
from typing import Any

import httpx

_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_TOKEN_TTL = 60.0


class KubeError(Exception):
    """A Kubernetes API call failed (not a 404, which the read/delete helpers report as None/False)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"kubernetes API {status}: {message}")
        self.status = status


class KubeClient:
    """Paths are API paths such as ``/apis/<group>/<version>/namespaces/<ns>/<plural>[/<name>]``."""

    def __init__(
        self,
        *,
        base_url: str = "https://kubernetes.default.svc",
        token_path: Path = _SA_DIR / "token",
        ca_path: Path = _SA_DIR / "ca.crt",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._token_path = token_path
        self._ca_path = ca_path
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._token = ""
        self._token_read = 0.0

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            verify: ssl.SSLContext | bool = True
            if self._transport is None:
                verify = ssl.create_default_context(cafile=str(self._ca_path))
            self._client = httpx.AsyncClient(
                base_url=self._base_url, verify=verify, transport=self._transport, timeout=httpx.Timeout(30.0)
            )
        return self._client

    def _auth(self) -> dict[str, str]:
        now = time.monotonic()
        if not self._token or now - self._token_read > _TOKEN_TTL:
            try:
                self._token = self._token_path.read_text().strip()
            except OSError:
                if not self._token:
                    raise
            self._token_read = now
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        content_type: str = "application/json",
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = {**self._auth(), "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type
        return await self._http().request(method, path, json=body, headers=headers, params=params)

    @staticmethod
    def _check(response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 300:
            try:
                message = str(response.json().get("message", ""))
            except ValueError:
                message = response.text[:300]
            raise KubeError(response.status_code, message)
        return response.json() if response.content else {}

    async def get(self, path: str) -> dict[str, Any] | None:
        response = await self._request("GET", path)
        if response.status_code == 404:
            return None
        return self._check(response)

    async def list(self, path: str, *, label_selector: str | None = None) -> list[dict[str, Any]]:
        params = {"labelSelector": label_selector} if label_selector else None
        data = self._check(await self._request("GET", path, params=params))
        return list(data.get("items") or [])

    async def create(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._check(await self._request("POST", path, body=body))

    async def merge_patch(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        response = await self._request("PATCH", path, body=body, content_type="application/merge-patch+json")
        if response.status_code == 404:
            return None
        return self._check(response)

    async def delete(self, path: str, *, propagation: str | None = "Background") -> bool:
        body = {"kind": "DeleteOptions", "apiVersion": "v1", "propagationPolicy": propagation} if propagation else None
        response = await self._request("DELETE", path, body=body)
        if response.status_code == 404:
            return False
        self._check(response)
        return True

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
