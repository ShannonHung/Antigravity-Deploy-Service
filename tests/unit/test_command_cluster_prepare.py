import pytest

from app.core.config import get_settings
from app.domain.command import CommandExecutionRequest, HostType
from app.repositories.inventory_repository import BastionMapping
from app.services.command_service import CommandService
from tests.fixtures.cluster import InMemoryInventoryRepository


class _FakeRepo:
    async def get(self, *a, **k): ...


@pytest.fixture(autouse=True)
def _slash_map(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(
        s, "CLUSTER_SLASH_TYPE_MAP", {"no_slash": "type1", "with_slash": "type2"}
    )
    # command_service caches `settings` at import; patch its module-level copy too.
    import app.services.command_service as cs

    monkeypatch.setattr(
        cs.settings,
        "CLUSTER_SLASH_TYPE_MAP",
        {"no_slash": "type1", "with_slash": "type2"},
        raising=False,
    )
    yield


async def test_prepare_execution_resolves_cluster_host(tmp_path, monkeypatch):
    # Whitelist allowing the resolved bastion IP and a trivial command.
    import json

    cfg = {
        "name": "admin",
        "allow_hosts": ["10\\.1\\.0\\.1"],
        "deny_hosts": [],
        "allow_commands": [
            {
                "command_name": "noop",
                "pipeline": [{"command": ["true"]}],
                "arguments": [],
            }
        ],
    }
    (tmp_path / "allow-commands-admin.json").write_text(json.dumps(cfg))
    (tmp_path / "SSH-default.json").write_text(
        json.dumps({"auth_method": "key", "key_base64": "Zm9v"})
    )
    monkeypatch.setattr(
        "app.services.command_service.settings.COMMAND_CONFIG_DIR",
        str(tmp_path),
        raising=False,
    )

    inv = InMemoryInventoryRepository(
        mappings={
            "type1": [
                BastionMapping(
                    patterns=["taiwan-.*"],
                    runner="r",
                    bastion="b",
                    bastion_ip="10.1.0.1",
                )
            ]
        }
    )
    svc = CommandService(_FakeRepo(), inventory_repo=inv)
    req = CommandExecutionRequest(
        command_name="noop",
        host="taiwan-taipei-my-cluster",
        host_type=HostType.CLUSTER,
        username="root",
    )
    ctx = await svc._executor._prepare_execution("admin", "req-1", req)
    assert ctx.resolved_host.ip == "10.1.0.1"
