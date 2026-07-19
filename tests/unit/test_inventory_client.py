"""Unit tests for InventoryClient using httpx.MockTransport."""
from __future__ import annotations

import httpx
import pytest

from app.clients.inventory_client import InventoryClient, InventoryTokenManager
from app.core.exceptions import (
    NotFoundException,
    UpstreamTimeoutException,
    UpstreamUnavailableException,
)


def _client(handler) -> InventoryClient:
    tm = InventoryTokenManager(api_key="test-key")
    transport = httpx.MockTransport(handler)
    return InventoryClient(
        base_url="http://fake",
        token_manager=tm,
        timeout=5.0,
        verify_ssl=True,
        transport=transport,
    )


# ── InventoryTokenManager ─────────────────────────────────────────────────────

async def test_token_manager_returns_api_key():
    tm = InventoryTokenManager(api_key="my-key")
    assert await tm.get_token() == "my-key"


async def test_token_manager_never_expires():
    tm = InventoryTokenManager(api_key="k")
    assert tm._is_expired() is False


# ── lookup_by_name ────────────────────────────────────────────────────────────

async def test_lookup_by_name_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/api/v1/k8s-clusters/node-cluster-lookup" in str(request.url)
        assert request.url.params["node_name"] == "node1"
        assert request.headers["authorization"] == "Token test-key"
        return httpx.Response(200, json={
            "node_type": "baremetal",
            "node": {"id": "123", "name": "node1", "labels": {"mgmt_ip": "10.1.2.3/8"}},
            "cluster": {"id": "1", "name": "type1-cluster-c1"},
        })

    info = await _client(handler).lookup_by_name("node1")
    assert info.node.name == "node1"
    assert info.node.labels["mgmt_ip"] == "10.1.2.3/8"
    assert info.cluster.name == "type1-cluster-c1"


async def test_lookup_by_name_404_raises_not_found():
    repo = _client(lambda r: httpx.Response(404))
    with pytest.raises(NotFoundException):
        await repo.lookup_by_name("missing")


async def test_lookup_by_name_500_raises_upstream_unavailable():
    repo = _client(lambda r: httpx.Response(500))
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup_by_name("x")


async def test_lookup_by_name_timeout_raises_upstream_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)
    with pytest.raises(UpstreamTimeoutException):
        await _client(handler).lookup_by_name("x")


async def test_lookup_by_name_connect_error_raises_upstream_unavailable():
    def handler(request):
        raise httpx.ConnectError("nope", request=request)
    with pytest.raises(UpstreamUnavailableException):
        await _client(handler).lookup_by_name("x")


async def test_lookup_by_name_non_json_raises_upstream_unavailable():
    repo = _client(lambda r: httpx.Response(200, content=b"not-json"))
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup_by_name("x")


# ── list_mappings ─────────────────────────────────────────────────────────────

async def test_list_mappings_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/api/v1/bastion-cluster-mappings" in str(request.url)
        assert request.url.params["name"] == "type1"
        return httpx.Response(200, json={
            "results": [{
                "data": [{
                    "patterns": ["type1-cluster.*"],
                    "runner": "r1",
                    "bastion": "b1",
                    "bastion_ip": "10.0.0.1",
                }]
            }]
        })

    mappings = await _client(handler).list_mappings("type1")
    assert len(mappings) == 1
    assert mappings[0].bastion_ip == "10.0.0.1"


async def test_list_mappings_empty_results_raises_not_found():
    repo = _client(lambda r: httpx.Response(200, json={"results": []}))
    with pytest.raises(NotFoundException):
        await repo.list_mappings("type1")


async def test_list_mappings_multiple_results_raises_upstream_unavailable():
    repo = _client(lambda r: httpx.Response(200, json={"results": [{"data": []}, {"data": []}]}))
    with pytest.raises(UpstreamUnavailableException):
        await repo.list_mappings("type1")


async def test_list_mappings_500_raises_upstream_unavailable():
    repo = _client(lambda r: httpx.Response(500))
    with pytest.raises(UpstreamUnavailableException):
        await repo.list_mappings("type1")


async def test_list_mappings_timeout_raises_upstream_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)
    with pytest.raises(UpstreamTimeoutException):
        await _client(handler).list_mappings("type1")


# ── _build_inventory_client verify selection ─────────────────────────────────

from app.core.config import get_settings
from app.core.dependencies import _build_inventory_client


def test_build_inventory_client_uses_ca_path_when_set(monkeypatch):
    monkeypatch.setenv("INVENTORY_CA", "data/ca.crt")
    monkeypatch.setenv("INVENTORY_API_VERIFY_SSL", "false")
    get_settings.cache_clear()
    try:
        client = _build_inventory_client()
        assert client._verify_ssl == "data/ca.crt"
    finally:
        get_settings.cache_clear()


def test_build_inventory_client_falls_back_to_verify_true(monkeypatch):
    monkeypatch.setenv("INVENTORY_CA", "")
    monkeypatch.setenv("INVENTORY_API_VERIFY_SSL", "true")
    get_settings.cache_clear()
    try:
        client = _build_inventory_client()
        assert client._verify_ssl is True
    finally:
        get_settings.cache_clear()


def test_build_inventory_client_falls_back_to_verify_false(monkeypatch):
    monkeypatch.setenv("INVENTORY_CA", "")
    monkeypatch.setenv("INVENTORY_API_VERIFY_SSL", "false")
    get_settings.cache_clear()
    try:
        client = _build_inventory_client()
        assert client._verify_ssl is False
    finally:
        get_settings.cache_clear()
