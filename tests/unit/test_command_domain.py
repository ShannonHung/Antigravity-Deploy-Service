import pytest
from pydantic import ValidationError

from app.domain.command import (
    CommandExecutionRequest,
    CommandExecutionResponse,
    CommandState,
    CommandStatus,
    HostType,
)


def test_host_type_enum_values():
    assert HostType.IP == "ip"
    assert HostType.BASTION == "bastion"
    assert HostType.HOSTNAME == "hostname"


def test_request_defaults_host_type_to_ip():
    req = CommandExecutionRequest(
        command_name="ls",
        host="10.0.0.1",
        username="root",
    )
    assert req.host_type == HostType.IP


def test_request_accepts_explicit_host_type():
    req = CommandExecutionRequest(
        command_name="ls",
        host="node-a01",
        username="root",
        host_type="hostname",
    )
    assert req.host_type == HostType.HOSTNAME


def test_request_rejects_unknown_host_type():
    with pytest.raises(ValidationError):
        CommandExecutionRequest(
            command_name="ls",
            host="x",
            username="root",
            host_type="dns",
        )


def test_command_state_has_resolved_ip_and_host_type():
    state = CommandState(
        command_id="abc",
        status=CommandStatus.RUNNING,
        host="node-a01",
        host_type=HostType.HOSTNAME,
        resolved_ip="10.0.1.10",
        port=22,
        username="root",
        ssh_config="default",
        request_id="rid",
        exec_command="ls",
        killable=True,
        pgids=[],
    )
    assert state.host_type == HostType.HOSTNAME
    assert state.resolved_ip == "10.0.1.10"


def test_response_defaults_for_new_metadata_fields():
    resp = CommandExecutionResponse(status="running")
    assert resp.host_type is None
    assert resp.resolved_ip is None
    assert resp.pgids == []


def test_response_accepts_new_metadata_fields():
    resp = CommandExecutionResponse(
        status="running",
        host_type=HostType.HOSTNAME,
        resolved_ip="10.0.1.10",
        pgids=[1234, 5678],
    )
    assert resp.host_type == HostType.HOSTNAME
    assert resp.resolved_ip == "10.0.1.10"
    assert resp.pgids == [1234, 5678]
