from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_inventory_repository
from app.main import create_app
from app.repositories.inventory_repository import BastionMapping
from tests.fixtures.cluster import InMemoryInventoryRepository


def _token(client, account="test_admin"):
    r = client.post("/token", data={"username": account, "password": "secret"})
    return r.json()["access_token"]


@pytest.fixture
def client():
    inv = InMemoryInventoryRepository(
        mappings={
            "type1": [
                BastionMapping(
                    patterns=["taiwan-.*"],
                    runner="r1",
                    bastion="b1",
                    bastion_ip="10.1.0.1",
                )
            ],
            "type2": [
                BastionMapping(
                    patterns=["taiwan-taipei/.*"],
                    runner="r2",
                    bastion="b2",
                    bastion_ip="10.2.0.2",
                )
            ],
        }
    )
    app = create_app()
    app.dependency_overrides[get_inventory_repository] = lambda: inv
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_no_slash_resolves(client):
    t = _token(client)
    r = client.get(
        "/api/v1/inventory/cluster/bastion-resolution",
        params={"cluster_name": "taiwan-taipei-my-cluster"},
        headers={"Authorization": f"Bearer {t}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["bastion_type"] == "type1"
    assert data["has_slash"] is False
    assert data["matched_mapping"]["bastion_ip"] == "10.1.0.1"


def test_slash_resolves(client):
    t = _token(client)
    r = client.get(
        "/api/v1/inventory/cluster/bastion-resolution",
        params={"cluster_name": "taiwan-taipei/my-cluster"},
        headers={"Authorization": f"Bearer {t}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["bastion_type"] == "type2"


def test_no_match_returns_404(client):
    t = _token(client)
    r = client.get(
        "/api/v1/inventory/cluster/bastion-resolution",
        params={"cluster_name": "nomatch-cluster"},
        headers={"Authorization": f"Bearer {t}"},
    )
    assert r.status_code == 404, r.text


def test_requires_auth(client):
    r = client.get(
        "/api/v1/inventory/cluster/bastion-resolution",
        params={"cluster_name": "taiwan-taipei-my-cluster"},
    )
    assert r.status_code == 401
