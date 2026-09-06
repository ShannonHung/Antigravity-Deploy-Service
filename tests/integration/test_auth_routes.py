"""
tests/integration/test_auth_routes.py

Integration tests for auth API endpoints.
Uses FastAPI TestClient with .env.test settings and tests/fixtures/users.json.

Response shapes:
  Success → {"data": {...}, "request_id": "..."}
  Error   → {"error": {"code": "...", "message": "..."}, "request_id": "..."}
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# ── POST /token ───────────────────────────────────────────────────────────────


def test_login_success(client: TestClient):
    resp = client.post(
        "/token",
        data={"username": "test_admin", "password": "secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # /token returns flat OAuth2 format so Swagger UI auto-fills Bearer
    assert body["access_token"]
    assert body["token_type"] == "bearer"


def test_login_wrong_password(client: TestClient):
    resp = client.post(
        "/token",
        data={"username": "test_admin", "password": "wrong"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "AUTH_ERROR"
    assert "request_id" in body


def test_login_unknown_account(client: TestClient):
    resp = client.post(
        "/token",
        data={"username": "ghost", "password": "secret"},
    )
    assert resp.status_code == 401


# ── Helper to get a valid token ───────────────────────────────────────────────


def _get_token(client: TestClient, account: str = "test_admin") -> str:
    resp = client.post("/token", data={"username": account, "password": "secret"})
    return resp.json()["access_token"]


# ── GET /api/v1/auth/verify ───────────────────────────────────────────────────


def test_verify_valid_token(client: TestClient):
    token = _get_token(client)
    resp = client.get(
        "/api/v1/auth/verify",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["valid"] is True
    assert body["data"]["account"] == "test_admin"
    assert "request_id" in body


def test_verify_no_token(client: TestClient):
    resp = client.get("/api/v1/auth/verify")
    assert resp.status_code == 401


def test_verify_bad_token(client: TestClient):
    resp = client.get(
        "/api/v1/auth/verify",
        headers={"Authorization": "Bearer invalid.token.here"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "AUTH_ERROR"


# ── POST /api/v1/auth/hash-password ──────────────────────────────────────────


def test_hash_password_no_auth_required(client: TestClient):
    resp = client.post(
        "/api/v1/auth/hash-password",
        json={"password": "newpassword123"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["hashed_password"].startswith("$2b$")
    assert "request_id" in body


def test_hash_password_too_short(client: TestClient):
    resp = client.post(
        "/api/v1/auth/hash-password",
        json={"password": "short"},
    )
    assert resp.status_code == 422  # Pydantic validation


# ── GET /api/v1/auth/my-scopes ────────────────────────────────────────────────


def test_my_scopes_admin(client: TestClient):
    token = _get_token(client, "test_admin")
    resp = client.get(
        "/api/v1/auth/my-scopes",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "deploy_api" in body["data"]["scopes"]
    assert "vm_api" in body["data"]["scopes"]


def test_my_scopes_deployer(client: TestClient):
    token = _get_token(client, "test_deployer")
    resp = client.get(
        "/api/v1/auth/my-scopes",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    scopes = resp.json()["data"]["scopes"]
    assert "deploy_api" in scopes
    assert "vm_api" not in scopes


# ── X-Coordination-ID header behaviour ───────────────────────────────────────


def test_coordination_id_echoed_in_response(client: TestClient):
    """If client sends X-Coordination-ID, it must be echoed in response header."""
    token = _get_token(client)
    my_id = "trace-abc-123"
    resp = client.get(
        "/api/v1/auth/verify",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Coordination-ID": my_id,
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("X-Coordination-ID") == my_id
    assert resp.json()["request_id"] == my_id


def test_no_coordination_id_no_response_header(client: TestClient):
    """If client sends no X-Coordination-ID, the response header must be absent."""
    token = _get_token(client)
    resp = client.get(
        "/api/v1/auth/verify",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert "X-Coordination-ID" not in resp.headers
