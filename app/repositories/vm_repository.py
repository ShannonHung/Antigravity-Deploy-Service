"""VM API repository.

GET {base_url}/api/v1/vms?name={node_name}

Contract:
  - 200 + exactly 1 result → VmInfo
  - 200 + empty results    → NotFoundException
  - 200 + >1 results       → UpstreamUnavailableException
                             (invariant violation: name lookup must be unique)
  - timeout                → UpstreamTimeoutException
  - other 4xx / 5xx / net  → UpstreamUnavailableException
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import (
    NotFoundException,
    UpstreamTimeoutException,
    UpstreamUnavailableException,
)


class VmK8sCluster(BaseModel):
    id: int
    name: str


class VmInfo(BaseModel):
    # Allow both alias ("k8s-cluster" from JSON) and attr name ("k8s_cluster")
    # construction. The alias matches the upstream JSON key which contains a
    # hyphen and is therefore not a valid Python identifier.
    model_config = ConfigDict(populate_by_name=True)

    id: int
    name: str
    k8s_cluster: VmK8sCluster = Field(alias="k8s-cluster")


class VmRepository(ABC):
    """Look up a VM by its node name."""

    @abstractmethod
    async def lookup_by_name(self, node_name: str) -> VmInfo: ...


class HttpVmRepository(VmRepository):
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
        )

    async def lookup_by_name(self, node_name: str) -> VmInfo:
        try:
            async with self._client() as client:
                resp = await client.get(
                    "/api/v1/vms",
                    params={"name": node_name},
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutException(
                f"VM lookup for '{node_name}' timed out after {self._timeout}s.",
                detail={"node_name": node_name},
            ) from exc
        except httpx.RequestError as exc:
            raise UpstreamUnavailableException(
                f"VM lookup for '{node_name}' failed: {exc}",
                detail={"node_name": node_name},
            ) from exc

        if resp.status_code >= 400:
            raise UpstreamUnavailableException(
                f"VM API returned {resp.status_code} for '{node_name}'.",
                detail={"node_name": node_name, "status_code": resp.status_code},
            )

        try:
            payload = resp.json()
        except Exception as exc:
            raise UpstreamUnavailableException(
                f"VM API returned non-JSON response for '{node_name}'.",
                detail={"node_name": node_name},
            ) from exc
        if not isinstance(payload, dict) or "results" not in payload:
            raise UpstreamUnavailableException(
                f"VM API returned unexpected payload shape for '{node_name}'.",
                detail={"node_name": node_name},
            )
        results = payload["results"]
        if len(results) == 0:
            raise NotFoundException(
                f"VM '{node_name}' not found.",
                detail={"node_name": node_name},
            )
        if len(results) > 1:
            raise UpstreamUnavailableException(
                f"VM lookup for '{node_name}' returned {len(results)} results; "
                "expected exactly 1 (unique-name invariant).",
                detail={"node_name": node_name, "result_count": len(results)},
            )

        return VmInfo.model_validate(results[0])
