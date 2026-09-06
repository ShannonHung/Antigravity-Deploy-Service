"""Integration tests for the host_type field on /api/v1/command/execution.

We override:
  - get_inventory_repository → InMemoryInventoryRepository
  - asyncssh.connect → stub that records target host and returns a fake conn
so we can assert the final SSH target without standing up real nodes.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

import app.services.command_service as svc_module
from app.core.dependencies import (
    get_command_state_repository,
    get_inventory_repository,
)
from app.main import create_app
from app.repositories.inventory_repository import BastionMapping
from app.repositories.inventory_repository import ClusterNodeInfo, ClusterRef, NodeInfo
from tests.fixtures.cluster import InMemoryInventoryRepository


class _InMemoryCommandStateRepo:
    """Minimal in-memory stand-in for CommandStateRepository.

    Implements only the methods exercised by the integration tests:
    save / get / update / update_if. State is keyed by command_id and TTL is ignored.
    The updater callable may be sync or async, mirroring the real repository.
    """

    def __init__(self):
        self._store: dict = {}

    async def save(self, state, ttl_seconds: int) -> None:
        self._store[state.command_id] = state

    async def get(self, command_id: str):
        return self._store.get(command_id)

    @staticmethod
    async def _apply(updater, state):
        import inspect

        result = updater(state)
        if inspect.isawaitable(result):
            await result

    async def update(self, command_id: str, updater, ttl_seconds: int):
        state = self._store.get(command_id)
        if state is None:
            return None
        await self._apply(updater, state)
        return state

    async def update_if(
        self, command_id: str, condition, updater, ttl_seconds: int
    ) -> bool:
        state = self._store.get(command_id)
        if state is None or not condition(state):
            return False
        await self._apply(updater, state)
        return True


def _get_token(client: TestClient, account: str = "test_admin") -> str:
    resp = client.post("/token", data={"username": account, "password": "secret"})
    return resp.json()["access_token"]


@pytest.fixture
def inventory_repo():
    return InMemoryInventoryRepository(
        nodes={
            "node-a01": ClusterNodeInfo(
                node_type="baremetal",
                node=NodeInfo(
                    id="1", name="node-a01", labels={"mgmt_ip": "10.0.1.10/24"}
                ),
                cluster=ClusterRef(id="1", name="cluster-c1"),
            ),
        }
    )


@pytest.fixture
def client_with_cluster_repo(inventory_repo):
    app = create_app()
    state_repo = _InMemoryCommandStateRepo()
    app.dependency_overrides[get_inventory_repository] = lambda: inventory_repo
    app.dependency_overrides[get_command_state_repository] = lambda: state_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _patch_asyncssh():
    """Patch asyncssh.connect so command_service._connect records the host
    and returns a stub connection that immediately fails on create_process."""
    fake_conn = MagicMock()
    fake_conn.is_closed.return_value = True
    fake_conn.run = AsyncMock(
        return_value=MagicMock(stdout="", stderr="", exit_status=0)
    )
    fake_conn.close = MagicMock()
    fake_conn.create_process = AsyncMock(side_effect=RuntimeError("stop here"))
    return (
        patch(
            "app.services.command_executor.asyncssh.connect",
            new=AsyncMock(return_value=fake_conn),
        ),
        fake_conn,
    )


def test_host_type_ip_connects_to_raw_ip(client_with_cluster_repo):
    p, _ = _patch_asyncssh()
    with p as mock_connect:
        token = _get_token(client_with_cluster_repo)
        resp = client_with_cluster_repo.post(
            "/api/v1/command/execution",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "command_name": "list_file",
                "host": "10.0.99.99",
                "host_type": "ip",
                "port": 22,
                "username": "root",
                "arguments": {"key_word": "ssh"},
            },
        )
        assert resp.status_code == 200, resp.text
        called_host = mock_connect.call_args.kwargs["host"]
        assert called_host == "10.0.99.99"


def test_host_type_hostname_connects_to_resolved_ip(client_with_cluster_repo):
    p, _ = _patch_asyncssh()
    with p as mock_connect:
        token = _get_token(client_with_cluster_repo)
        resp = client_with_cluster_repo.post(
            "/api/v1/command/execution",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "command_name": "list_file",
                "host": "node-a01",
                "host_type": "hostname",
                "port": 22,
                "username": "root",
                "arguments": {"key_word": "ssh"},
            },
        )
        assert resp.status_code == 200, resp.text
        called_host = mock_connect.call_args.kwargs["host"]
        assert called_host == "10.0.1.10"  # mgmt_ip CIDR strip


@pytest.fixture
def client_with_bastion(monkeypatch):
    """TestClient with a unified inventory repo covering both node lookup and bastion mapping."""
    monkeypatch.setattr(
        svc_module.settings, "BASTION_NODE_TYPE_MAP", {"baremetal": "type1"}
    )
    app = create_app()
    state_repo = _InMemoryCommandStateRepo()
    inv_repo = InMemoryInventoryRepository(
        nodes={
            "node1": ClusterNodeInfo(
                node_type="baremetal",
                node=NodeInfo(
                    id="1",
                    name="node1",
                    labels={"mgmt_ip": "10.0.1.5/8", "router_id": "10.0.1.1"},
                ),
                cluster=ClusterRef(id="1", name="type1-cluster-c1"),
            ),
        },
        mappings={
            "type1": [
                BastionMapping(
                    patterns=["type1-cluster-(c1|c2|c3)", "type1-cluster.*"],
                    runner="r1",
                    bastion="bastion-type1",
                    bastion_ip="10.99.99.1",
                )
            ]
        },
    )
    app.dependency_overrides[get_command_state_repository] = lambda: state_repo
    app.dependency_overrides[get_inventory_repository] = lambda: inv_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_host_type_bastion_connects_to_mapped_bastion_ip(client_with_bastion):
    p, _ = _patch_asyncssh()
    with p as mock_connect:
        token = _get_token(client_with_bastion)
        resp = client_with_bastion.post(
            "/api/v1/command/execution",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "command_name": "list_file",
                "host": "node1",
                "host_type": "bastion",
                "port": 22,
                "username": "root",
                "arguments": {"key_word": "ssh"},
            },
        )
        assert resp.status_code == 200, resp.text
        called_host = mock_connect.call_args.kwargs["host"]
        assert called_host == "10.99.99.1"


def test_hostname_not_in_cluster_lookup_returns_404(client_with_cluster_repo):
    token = _get_token(client_with_cluster_repo)
    resp = client_with_cluster_repo.post(
        "/api/v1/command/execution",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "command_name": "list_file",
            "host": "missing-node",
            "host_type": "hostname",
            "port": 22,
            "username": "root",
            "arguments": {"key_word": "ssh"},
        },
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_unknown_host_type_returns_422(client_with_cluster_repo):
    token = _get_token(client_with_cluster_repo)
    resp = client_with_cluster_repo.post(
        "/api/v1/command/execution",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "command_name": "list_file",
            "host": "node-a01",
            "host_type": "dns",
            "port": 22,
            "username": "root",
            "arguments": {"key_word": "ssh"},
        },
    )
    assert resp.status_code == 422, resp.text


def test_poll_response_surfaces_host_type_resolved_ip_and_pgids(
    client_with_cluster_repo,
):
    """GET /command/execution/{id} should expose host_type, resolved_ip, pgids."""
    from app.core.dependencies import get_command_state_repository
    from app.domain.command import CommandState, CommandStatus, HostType

    fixed_state = CommandState(
        command_id="fixed-id",
        status=CommandStatus.SUCCESS,
        host="node-a01",
        host_type=HostType.HOSTNAME,
        resolved_ip="10.0.1.10",
        port=22,
        username="root",
        ssh_config="default",
        request_id="rid",
        exec_command="ls",
        killable=True,
        pgids=[111, 222],
        exit_code=0,
        output="ok",
    )

    class _StubRepo:
        async def get(self, command_id):
            assert command_id == "fixed-id"
            return fixed_state

    client_with_cluster_repo.app.dependency_overrides[get_command_state_repository] = (
        lambda: _StubRepo()
    )

    token = _get_token(client_with_cluster_repo)
    resp = client_with_cluster_repo.get(
        "/api/v1/command/execution/fixed-id",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["host_type"] == "hostname"
    assert data["resolved_ip"] == "10.0.1.10"
    assert data["pgids"] == [111, 222]
    assert data["exit_status"] == 0
    assert data["output"] == "ok"
