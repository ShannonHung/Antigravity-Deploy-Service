"""Integration tests for GET /api/v1/inventory/nodes/{node_name}/bastion-resolution."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_inventory_repository, get_inventory_service
from app.main import create_app
from app.repositories.inventory_repository import (
    BastionMapping,
    ClusterNodeInfo,
    ClusterRef,
    NodeInfo,
)
from app.services.inventory_service import InventoryService
from tests.fixtures.cluster import InMemoryInventoryRepository

_NODE_TYPE_MAP = {"baremetal": "type1"}

_INV_REPO = InMemoryInventoryRepository(
    nodes={
        "node1": ClusterNodeInfo(
            node_type="baremetal",
            node=NodeInfo(id="1", name="node1", labels={"mgmt_ip": "10.0.1.5/24"}),
            cluster=ClusterRef(id="1", name="type1-cluster-c1"),
        ),
    },
    mappings={
        "type1": [
            BastionMapping(
                patterns=["type1-cluster-c.*"],
                runner="type1-runner",
                bastion="type1-bastion",
                bastion_ip="10.223.192.40",
            )
        ]
    },
)


def _get_token(client: TestClient, account: str = "test_admin") -> str:
    resp = client.post("/token", data={"username": account, "password": "secret"})
    return resp.json()["access_token"]


@pytest.fixture
def resolution_client():
    app = create_app()
    app.dependency_overrides[get_inventory_repository] = lambda: _INV_REPO
    app.dependency_overrides[get_inventory_service] = lambda: InventoryService(
        repo=_INV_REPO, node_type_map=_NODE_TYPE_MAP
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── Happy path ────────────────────────────────────────────────────────────────


def test_resolution_success(resolution_client):
    token = _get_token(resolution_client)
    resp = resolution_client.get(
        "/api/v1/inventory/nodes/node1/bastion-resolution",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["node_type"] == "baremetal"
    assert data["node"]["name"] == "node1"
    assert data["cluster"]["name"] == "type1-cluster-c1"
    assert data["bastion_type"] == "type1"
    assert data["bastion_type_source"] == "config"
    assert data["matched_mapping"]["runner"] == "type1-runner"
    assert data["matched_mapping"]["bastion_ip"] == "10.223.192.40"
    assert data["matched_pattern"] == "type1-cluster-c.*"


def test_resolution_with_bastion_type_override(resolution_client):
    # Add override-type mapping to the in-memory repo inline
    override_repo = InMemoryInventoryRepository(
        nodes=_INV_REPO._nodes,
        mappings={
            **_INV_REPO._mappings,
            "override-type": [
                BastionMapping(
                    patterns=[".*"],
                    runner="override-runner",
                    bastion="override-bastion",
                    bastion_ip="10.9.9.9",
                )
            ],
        },
    )
    app = create_app()
    app.dependency_overrides[get_inventory_repository] = lambda: override_repo
    app.dependency_overrides[get_inventory_service] = lambda: InventoryService(
        repo=override_repo, node_type_map=_NODE_TYPE_MAP
    )
    with TestClient(app) as c:
        token = _get_token(c)
        resp = c.get(
            "/api/v1/inventory/nodes/node1/bastion-resolution",
            params={"bastion_type": "override-type"},
            headers={"Authorization": f"Bearer {token}"},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["bastion_type"] == "override-type"
    assert data["bastion_type_source"] == "query_param"
    assert data["matched_mapping"]["runner"] == "override-runner"


# ── Error cases ───────────────────────────────────────────────────────────────


def test_resolution_node_not_found_returns_404(resolution_client):
    token = _get_token(resolution_client)
    resp = resolution_client.get(
        "/api/v1/inventory/nodes/missing-node/bastion-resolution",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_resolution_no_pattern_match_returns_404(resolution_client):
    no_match_repo = InMemoryInventoryRepository(
        nodes={
            "node1": ClusterNodeInfo(
                node_type="baremetal",
                node=NodeInfo(id="1", name="node1", labels={}),
                cluster=ClusterRef(id="1", name="unmatched-cluster"),
            )
        },
        mappings={
            "type1": [
                BastionMapping(
                    patterns=["type1-cluster-c.*"],
                    runner="r",
                    bastion="b",
                    bastion_ip="10.0.0.1",
                )
            ]
        },
    )
    app = create_app()
    app.dependency_overrides[get_inventory_repository] = lambda: no_match_repo
    app.dependency_overrides[get_inventory_service] = lambda: InventoryService(
        repo=no_match_repo, node_type_map=_NODE_TYPE_MAP
    )
    with TestClient(app) as c:
        token = _get_token(c)
        resp = c.get(
            "/api/v1/inventory/nodes/node1/bastion-resolution",
            headers={"Authorization": f"Bearer {token}"},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 404, resp.text


def test_resolution_unknown_node_type_returns_400(resolution_client):
    unknown_type_repo = InMemoryInventoryRepository(
        nodes={
            "node1": ClusterNodeInfo(
                node_type="unknown-type",
                node=NodeInfo(id="1", name="node1", labels={}),
                cluster=ClusterRef(id="1", name="any-cluster"),
            )
        },
        mappings={},
    )
    app = create_app()
    app.dependency_overrides[get_inventory_repository] = lambda: unknown_type_repo
    app.dependency_overrides[get_inventory_service] = lambda: InventoryService(
        repo=unknown_type_repo, node_type_map=_NODE_TYPE_MAP
    )
    with TestClient(app) as c:
        token = _get_token(c)
        resp = c.get(
            "/api/v1/inventory/nodes/node1/bastion-resolution",
            headers={"Authorization": f"Bearer {token}"},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "COMMAND_EXECUTION_ERROR"


def test_resolution_no_token_returns_401(resolution_client):
    resp = resolution_client.get("/api/v1/inventory/nodes/node1/bastion-resolution")
    assert resp.status_code == 401


def test_resolution_wrong_scope_returns_403(resolution_client):
    # test_deployer only has deploy_api scope, not command_api
    resp = resolution_client.post(
        "/token", data={"username": "test_deployer", "password": "secret"}
    )
    token = resp.json()["access_token"]
    resp = resolution_client.get(
        "/api/v1/inventory/nodes/node1/bastion-resolution",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
