"""Integration tests for /api/v1/inventory/ endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_inventory_repository
from app.main import create_app
from app.repositories.inventory_repository import (
    BastionMapping,
    ClusterNodeInfo,
    ClusterRef,
    NodeInfo,
)
from tests.fixtures.cluster import InMemoryInventoryRepository


def _get_token(client: TestClient, account: str = "test_admin") -> str:
    resp = client.post("/token", data={"username": account, "password": "secret"})
    return resp.json()["access_token"]


@pytest.fixture
def inventory_client():
    inv_repo = InMemoryInventoryRepository(
        nodes={
            "node1": ClusterNodeInfo(
                node_type="baremetal",
                node=NodeInfo(id="1", name="node1", labels={"mgmt_ip": "10.0.1.5/24"}),
                cluster=ClusterRef(id="1", name="cluster-c1"),
            ),
        },
        mappings={
            "type1": [
                BastionMapping(
                    patterns=["cluster-c.*"],
                    runner="r1",
                    bastion="bastion1",
                    bastion_ip="10.99.0.1",
                )
            ]
        },
    )
    app = create_app()
    app.dependency_overrides[get_inventory_repository] = lambda: inv_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── GET /api/v1/inventory/nodes/{node_name} ───────────────────────────────────


def test_lookup_by_name_success(inventory_client):
    token = _get_token(inventory_client)
    resp = inventory_client.get(
        "/api/v1/inventory/nodes/node1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["node"]["name"] == "node1"
    assert data["cluster"]["name"] == "cluster-c1"


def test_lookup_by_name_not_found(inventory_client):
    token = _get_token(inventory_client)
    resp = inventory_client.get(
        "/api/v1/inventory/nodes/missing",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_lookup_by_name_no_token_returns_401(inventory_client):
    resp = inventory_client.get("/api/v1/inventory/nodes/node1")
    assert resp.status_code == 401


def test_lookup_by_name_wrong_scope_returns_403(inventory_client):
    # test_deployer 只有 deploy_api scope，沒有 command_api
    resp = inventory_client.post(
        "/token",
        data={"username": "test_deployer", "password": "secret"},
    )
    assert (
        resp.status_code == 200
    ), "test_deployer must be configured in tests/fixtures/users.json"
    token = resp.json()["access_token"]
    resp = inventory_client.get(
        "/api/v1/inventory/nodes/node1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# ── GET /api/v1/inventory/mappings ────────────────────────────────────────────


def test_list_mappings_success(inventory_client):
    token = _get_token(inventory_client)
    resp = inventory_client.get(
        "/api/v1/inventory/mappings",
        params={"type": "type1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["bastion_ip"] == "10.99.0.1"


def test_list_mappings_not_found(inventory_client):
    token = _get_token(inventory_client)
    resp = inventory_client.get(
        "/api/v1/inventory/mappings",
        params={"type": "unknown"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, resp.text


def test_list_mappings_missing_type_param_returns_422(inventory_client):
    token = _get_token(inventory_client)
    resp = inventory_client.get(
        "/api/v1/inventory/mappings",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


def test_list_mappings_no_token_returns_401(inventory_client):
    resp = inventory_client.get("/api/v1/inventory/mappings", params={"type": "type1"})
    assert resp.status_code == 401
