"""Unit tests for the fake-api endpoints."""

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def fake_app():
    # fake-api is not a package — load main.py by file path.
    root = Path(__file__).resolve().parents[2]  # deploy-service/
    sys.path.insert(0, str(root / "fake-api"))
    try:
        if "main" in sys.modules:
            del sys.modules["main"]
        module = importlib.import_module("main")
    finally:
        sys.path.pop(0)
    return TestClient(module.app)


def _auth():
    return {"Authorization": "Token test"}


def test_cluster_node_lookup_known_node_returns_fixture(fake_app):
    r = fake_app.get(
        "/api/v1/k8s-clusters/node-cluster-lookup",
        params={"node_name": "node1"},
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["node"]["name"] == "node1"
    assert body["cluster"]["name"] == "type1-cluster-c1"


def test_cluster_node_lookup_unknown_node_returns_fallback(fake_app):
    r = fake_app.get(
        "/api/v1/k8s-clusters/node-cluster-lookup",
        params={"node_name": "does-not-exist"},
        headers=_auth(),
    )
    # Falls back to cluster-node-lookup.json which exists
    assert r.status_code == 200


def test_cluster_node_lookup_missing_auth_returns_401(fake_app):
    r = fake_app.get(
        "/api/v1/k8s-clusters/node-cluster-lookup", params={"node_name": "node1"}
    )
    assert r.status_code == 401


def test_mappings_known_type_returns_fixture(fake_app):
    r = fake_app.get(
        "/api/v1/bastion-cluster-mappings",
        params={"name": "type1"},
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["results"][0]["data"][0]["bastion"] == "type1-bastion"


def test_mappings_unknown_type_returns_empty(fake_app):
    r = fake_app.get(
        "/api/v1/bastion-cluster-mappings",
        params={"name": "does-not-exist"},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.json()["results"] == []


def test_mappings_missing_auth_returns_401(fake_app):
    r = fake_app.get("/api/v1/bastion-cluster-mappings", params={"name": "type1"})
    assert r.status_code == 401
