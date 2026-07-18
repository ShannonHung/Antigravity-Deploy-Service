"""Inventory API HTTP client.

InventoryTokenManager  — 靜態 API key，未來可換成 JWT 只需改這個 class
InventoryClient        — 實作 InventoryRepository
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import httpx

from app.core.exceptions import (
    NotFoundException,
    UpstreamTimeoutException,
    UpstreamUnavailableException,
)
from app.repositories.inventory_repository import (
    BastionMapping,
    ClusterNodeInfo,
    InventoryRepository,
)

_logger = logging.getLogger(__name__)


# ── TokenManager（與 cluster-service 同結構，deploy-service 自己的副本）────────

class TokenManager(ABC):
    def __init__(self, initial_token: str = "") -> None:
        self._token: str = initial_token
        self._expire_at: float = 0
        self._lock = asyncio.Lock()

    @abstractmethod
    async def _fetch_new_token(self) -> tuple[str, float]: ...

    async def get_token(self) -> str:
        async with self._lock:
            if not self._token or self._is_expired():
                await self.refresh()
            return self._token

    async def refresh(self) -> None:
        token, expires_in = await self._fetch_new_token()
        self._token = token
        self._expire_at = time.time() + expires_in - 30

    def _is_expired(self) -> bool:
        return time.time() >= self._expire_at


class InventoryTokenManager(TokenManager):
    """靜態 API key — 不過期，不需要遠端取得。"""

    def __init__(self, api_key: str) -> None:
        super().__init__(initial_token=api_key)
        self._api_key = api_key

    def _is_expired(self) -> bool:
        return False

    async def _fetch_new_token(self) -> tuple[str, float]:
        return self._api_key, float("inf")


# ── InventoryClient ───────────────────────────────────────────────────────────

class InventoryClient(InventoryRepository):
    """Async HTTP client for the Inventory API."""

    def __init__(
        self,
        base_url: str,
        token_manager: InventoryTokenManager,
        timeout: float,
        verify_ssl: str | bool = True,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_manager = token_manager
        self._timeout = timeout
        self._verify_ssl = verify_ssl
        self._transport = transport

    async def _headers(self) -> Dict[str, str]:
        token = await self._token_manager.get_token()
        return {"Authorization": f"Token {token}"}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            verify=self._verify_ssl,
            transport=self._transport,
        )

    def _raise_for_error(self, response: httpx.Response, context: str) -> None:
        _logger.error(
            "inventory API error | context=%s | status=%s",
            context,
            response.status_code,
        )
        if response.status_code == 404:
            raise NotFoundException(
                f"Inventory resource not found ({context}).",
                detail={"context": context, "status_code": response.status_code},
            )
        raise UpstreamUnavailableException(
            f"Inventory API returned {response.status_code} ({context}).",
            detail={"context": context, "status_code": response.status_code},
        )

    async def _request_with_retry(
        self, method: str, path: str, context: str, **kwargs: Any
    ) -> httpx.Response:
        headers = await self._headers()
        kwargs["headers"] = {**kwargs.get("headers", {}), **headers}
        try:
            async with self._client() as client:
                response = await client.request(method, path, **kwargs)
                if response.status_code == 401:
                    _logger.warning("Received 401 from inventory API (%s); refreshing token.", context)
                    await self._token_manager.refresh()
                    headers = await self._headers()
                    kwargs["headers"] = {**kwargs.get("headers", {}), **headers}
                    response = await client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutException(
                f"Inventory API timed out after {self._timeout}s ({context}).",
                detail={"context": context},
            ) from exc
        except httpx.RequestError as exc:
            raise UpstreamUnavailableException(
                f"Inventory API request failed ({context}): {exc}",
                detail={"context": context},
            ) from exc
        return response

    # ── InventoryRepository methods ──────────────────────────────────────────

    async def lookup_by_name(self, node_name: str) -> ClusterNodeInfo:
        response = await self._request_with_retry(
            "GET",
            "/api/v1/k8s-clusters/node-cluster-lookup",
            context=f"lookup_by_name({node_name})",
            params={"node_name": node_name},
        )
        if response.is_error:
            self._raise_for_error(response, f"lookup_by_name({node_name})")

        try:
            payload = response.json()
        except Exception:
            raise UpstreamUnavailableException(
                f"Inventory API returned non-JSON for lookup_by_name('{node_name}').",
                detail={"node_name": node_name},
            )

        try:
            return ClusterNodeInfo.model_validate(payload)
        except Exception:
            raise UpstreamUnavailableException(
                f"Inventory API returned unexpected payload shape for lookup_by_name('{node_name}').",
                detail={"node_name": node_name},
            )

    async def list_mappings(self, type_name: str) -> List[BastionMapping]:
        response = await self._request_with_retry(
            "GET",
            "/api/v1/bastion-cluster-mappings",
            context=f"list_mappings({type_name})",
            params={"name": type_name},
        )
        if response.is_error:
            self._raise_for_error(response, f"list_mappings({type_name})")

        try:
            payload = response.json()
        except Exception:
            raise UpstreamUnavailableException(
                f"Inventory API returned non-JSON for list_mappings('{type_name}').",
                detail={"type": type_name},
            )

        if not isinstance(payload, dict) or "results" not in payload:
            raise UpstreamUnavailableException(
                f"Inventory API returned unexpected payload shape for list_mappings('{type_name}').",
                detail={"type": type_name},
            )

        results = payload["results"]
        if not results:
            raise NotFoundException(
                f"No bastion mappings found for type '{type_name}'.",
                detail={"type": type_name},
            )
        if len(results) != 1:
            raise UpstreamUnavailableException(
                f"Inventory API returned {len(results)} results for list_mappings('{type_name}'); expected 1.",
                detail={"type": type_name, "count": len(results)},
            )

        data = results[0].get("data")
        if not isinstance(data, list):
            raise UpstreamUnavailableException(
                f"Inventory API result missing 'data' list for list_mappings('{type_name}').",
                detail={"type": type_name},
            )
        return [BastionMapping.model_validate(item) for item in data]
