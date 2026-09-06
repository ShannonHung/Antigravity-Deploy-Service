import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.core.dependencies import get_command_state_repository
from app.core.exceptions import CommandExecutionException


class _EmptyCommandStateRepo:
    """In-memory repo that mirrors the real one's 'not found' behaviour
    (raises CommandExecutionException) so we don't need a live Redis."""

    async def get(self, command_id: str):
        raise CommandExecutionException(f"Execution record {command_id} not found.")


@pytest.fixture
def trace_client():
    app = create_app()
    app.dependency_overrides[get_command_state_repository] = (
        lambda: _EmptyCommandStateRepo()
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _token(client):
    r = client.post("/token", data={"username": "test_admin", "password": "secret"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_trace_ui_requires_scope(trace_client):
    r = trace_client.get("/api/v1/command/execution/whatever/trace/ui")
    assert r.status_code in (401, 403)


def test_trace_ui_unknown_command_404(trace_client):
    tok = _token(trace_client)
    r = trace_client.get(
        "/api/v1/command/execution/does-not-exist/trace/ui",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 404
    body = r.json()
    assert "request_id" in body or "detail" in body  # structured error envelope


def test_view_returns_html_with_trace_url(trace_client):
    r = trace_client.get("/api/v1/command/execution/c1/view")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "/api/v1/command/execution/c1/trace/ui" in r.text
