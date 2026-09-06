import pytest
from app.services.command_service import CommandService, CommandExecutionException
from app.domain.command import CommandState, CommandStatus


def _make_state() -> CommandState:
    return CommandState(
        command_id="cmd-1",
        status=CommandStatus.RUNNING,
        host="localhost",
        resolved_ip="127.0.0.1",
        port=22,
        username="admin",
        ssh_config="default",
        request_id="req-1",
        exec_command="run-ansible.sh --playbook fail.yml",
        killable=True,
    )


def test_mark_failed_stores_exit_code_and_output():
    """A failed command must retain the exit code and output, mirroring
    mark_success, so the poll endpoint can surface WHY it failed (e.g. a
    non-zero ansible exit) instead of returning null/null."""
    state = _make_state()
    state.mark_failed("non-zero exit", exit_code=2, output="PLAY RECAP ... failed=1")

    assert state.status == CommandStatus.FAILED
    assert state.message == "non-zero exit"
    assert state.exit_code == 2
    assert state.output == "PLAY RECAP ... failed=1"


def test_mark_failed_exit_code_and_output_optional():
    """exit_code/output are optional — failures without a process result
    (e.g. capacity rejection, SSH error) still work."""
    state = _make_state()
    state.mark_failed("ssh connect failed")

    assert state.status == CommandStatus.FAILED
    assert state.message == "ssh connect failed"
    assert state.exit_code is None
    assert state.output is None


def test_anti_injection_pass():
    svc = CommandService(None)  # repo not used for this method
    svc._executor._validate_anti_injection("safe_string_123")


def test_anti_injection_fail():
    svc = CommandService(None)
    with pytest.raises(CommandExecutionException):
        svc._executor._validate_anti_injection("ls; rm -rf /")
    with pytest.raises(CommandExecutionException):
        svc._executor._validate_anti_injection("$(whoami)")
    with pytest.raises(CommandExecutionException):
        svc._executor._validate_anti_injection("a & b")
    with pytest.raises(CommandExecutionException):
        svc._executor._validate_anti_injection("a | b")
    with pytest.raises(CommandExecutionException):
        svc._executor._validate_anti_injection("`whoami`")


# ── bastion_type wiring ────────────────────────────────────────────────────

import app.services.command_service as svc_module
from unittest.mock import AsyncMock, MagicMock

from app.domain.command import (
    CommandExecutionRequest,
    CommandOption,
    HostType,
)
from app.repositories.inventory_repository import BastionMapping
from app.repositories.inventory_repository import ClusterNodeInfo, ClusterRef, NodeInfo
from tests.fixtures.cluster import InMemoryInventoryRepository


def _service_for_bastion(inventory_repo):
    """Build a CommandService with minimum deps for _prepare_execution to run."""
    state_repo = MagicMock()
    state_repo.save = AsyncMock()
    return CommandService(
        repo=state_repo,
        inventory_repo=inventory_repo,
    )


def _node_lookup(
    cluster: str, node_type: str = "baremetal"
) -> InMemoryInventoryRepository:
    return InMemoryInventoryRepository(
        nodes={
            "n1": ClusterNodeInfo(
                node_type=node_type,
                node=NodeInfo(id="1", name="n1", labels={}),
                cluster=ClusterRef(id="1", name=cluster),
            )
        }
    )


def _inventory(
    cluster: str, type_name: str, ip: str, node_type: str = "baremetal"
) -> InMemoryInventoryRepository:
    return InMemoryInventoryRepository(
        nodes={
            "n1": ClusterNodeInfo(
                node_type=node_type,
                node=NodeInfo(id="1", name="n1", labels={}),
                cluster=ClusterRef(id="1", name=cluster),
            )
        },
        mappings={
            type_name: [
                BastionMapping(
                    patterns=[".*"],
                    runner="r",
                    bastion="b",
                    bastion_ip=ip,
                )
            ]
        },
    )


async def test_bastion_type_explicit_in_option_overrides_node_type_map(monkeypatch):
    """When option.bastion_type='type2' is set, it overrides node_type_map lookup."""
    monkeypatch.setattr(
        svc_module.settings, "BASTION_NODE_TYPE_MAP", {"baremetal": "type1"}
    )
    # Only 'type2' has a mapping; if the resolver used node_type_map it would pick type1 and 404.
    inventory_repo = _inventory(
        "any-cluster", "type2", "10.10.10.10", node_type="baremetal"
    )
    svc = _service_for_bastion(inventory_repo)

    req = CommandExecutionRequest(
        command_name="list_file",
        host="n1",
        host_type=HostType.BASTION,
        port=22,
        username="root",
        ssh_config="default",
        option=CommandOption(timeout_seconds=30, bastion_type="type2"),
        arguments={"key_word": "ssh"},
    )
    ctx = await svc._executor._prepare_execution("test_admin", "rid", req)
    assert ctx.resolved_host.ip == "10.10.10.10"
    assert ctx.resolved_host.metadata["bastion_type"] == "type2"


async def test_bastion_type_derived_from_node_type_map_when_no_option(monkeypatch):
    """When option.bastion_type is absent, node_type is looked up in BASTION_NODE_TYPE_MAP."""
    monkeypatch.setattr(
        svc_module.settings, "BASTION_NODE_TYPE_MAP", {"baremetal": "type1"}
    )
    inventory_repo = _inventory(
        "any-cluster", "type1", "10.20.30.40", node_type="baremetal"
    )
    svc = _service_for_bastion(inventory_repo)

    req = CommandExecutionRequest(
        command_name="list_file",
        host="n1",
        host_type=HostType.BASTION,
        port=22,
        username="root",
        ssh_config="default",
        option=CommandOption(timeout_seconds=30),
        arguments={"key_word": "ssh"},
    )
    ctx = await svc._executor._prepare_execution("test_admin", "rid", req)
    assert ctx.resolved_host.ip == "10.20.30.40"
    assert ctx.resolved_host.metadata["bastion_type"] == "type1"


async def test_unknown_node_type_not_in_map_raises_with_clear_message(monkeypatch):
    """When node_type has no entry in BASTION_NODE_TYPE_MAP, raise CommandExecutionException
    with the unknown node_type and the current map contents in the message."""
    monkeypatch.setattr(
        svc_module.settings, "BASTION_NODE_TYPE_MAP", {"baremetal": "type1"}
    )
    inventory_repo = _inventory(
        "any-cluster", "type1", "10.0.0.1", node_type="unknown-hw"
    )
    svc = _service_for_bastion(inventory_repo)

    req = CommandExecutionRequest(
        command_name="list_file",
        host="n1",
        host_type=HostType.BASTION,
        port=22,
        username="root",
        ssh_config="default",
        arguments={"key_word": "ssh"},
    )

    with pytest.raises(CommandExecutionException) as exc_info:
        await svc._executor._prepare_execution("test_admin", "rid", req)

    msg = str(exc_info.value)
    assert "unknown-hw" in msg
    assert "baremetal" in msg
