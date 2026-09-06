from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_command_service
from app.domain.command import CommandState, CommandStatus
from app.main import create_app


def _token(client, account):
    r = client.post("/token", data={"username": account, "password": "secret"})
    return r.json()["access_token"]


def _state(cid, status):
    return CommandState(
        command_id=cid,
        status=status,
        host="h",
        resolved_ip="1.1.1.1",
        port=22,
        username="root",
        ssh_config="default",
        request_id="r",
        exec_command="true",
        killable=False,
    )


class _FakeService:
    async def list_running_commands(self, statuses=None):
        all_states = [
            _state("a", CommandStatus.RUNNING),
            _state("b", CommandStatus.KILLING),
            _state("c", CommandStatus.SUCCESS),
        ]
        if statuses is None:
            statuses = {CommandStatus.RUNNING, CommandStatus.KILLING}
        return [s for s in all_states if s.status in statuses]


@pytest.fixture
def client():
    app = create_app()
    app.dependency_overrides[get_command_service] = lambda: _FakeService()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_admin_lists_non_terminal(client):
    t = _token(client, "test_admin")
    r = client.get("/api/v1/command/running", headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["count"] == 2
    assert sorted(c["command_id"] for c in data["commands"]) == ["a", "b"]


def test_status_filter(client):
    t = _token(client, "test_admin")
    r = client.get(
        "/api/v1/command/running",
        params={"status": "success"},
        headers={"Authorization": f"Bearer {t}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["count"] == 1
    assert data["commands"][0]["command_id"] == "c"


def test_command_api_only_user_forbidden(client):
    t = _token(client, "test_command")
    r = client.get("/api/v1/command/running", headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 403, r.text


def test_invalid_status_422(client):
    t = _token(client, "test_admin")
    r = client.get(
        "/api/v1/command/running",
        params={"status": "bogus"},
        headers={"Authorization": f"Bearer {t}"},
    )
    assert r.status_code == 422
