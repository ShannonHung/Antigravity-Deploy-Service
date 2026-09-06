import pytest
from app.domain.command import (
    CommandExecutionRequest,
    CommandWhitelistConfig,
    PipelineStep,
    SSHConnectionConfig,
    ExecutionContext,
    HostType,
)
from app.repositories.host_resolver import ResolvedHost
from app.services.command_service import CommandService


def _ctx(cmd_config, run_id=None):
    req = CommandExecutionRequest(
        command_name="run_ansible_ping_all",
        host="localhost",
        host_type=HostType.IP,
        port=2224,
        username="root",
        ssh_config="control_node",
        arguments={"inventory": "taipei/multinode.ini"},
    )
    ctx = ExecutionContext(
        username="admin",
        request_id="r1",
        command_name="run_ansible_ping_all",
        raw_request=req,
        cmd_config=cmd_config,
        ssh_config=SSHConnectionConfig(auth_method="key", key_base64="x"),
        resolved_host=ResolvedHost(ip="1.2.3.4", source_input="localhost"),
    )
    ctx.run_id = run_id
    return ctx


def _svc():
    return CommandService(repo=None, inventory_repo=None)


def test_logged_command_resolves_run_id_placeholder():
    cfg = CommandWhitelistConfig(
        command_name="run_ansible_ping_all",
        logged=True,
        pipeline=[
            PipelineStep(
                command=[
                    "/x/run-ansible.sh",
                    "--inventory",
                    "{inventory}",
                    "--log-dir",
                    "/var/log/ansible-runs",
                    "--run-id",
                    "{run_id}",
                ]
            )
        ],
        arguments=[],
    )
    ctx = _ctx(cfg, run_id="abc-123")
    pipeline = _svc()._executor._pipeline_builder.build(ctx)
    flat = pipeline[0]
    assert "{run_id}" not in flat
    assert "abc-123" in flat
    assert flat[flat.index("--run-id") + 1] == "abc-123"


def test_compute_log_path():
    path = _svc()._executor._compute_log_path("abc-123")
    assert path == "/var/log/deploy-service/abc-123.log"


import asyncio
from unittest.mock import AsyncMock, MagicMock
from app.domain.command import CommandState, CommandStatus


async def test_handle_async_persists_run_log_path(monkeypatch):
    cfg = CommandWhitelistConfig(
        command_name="run_ansible_ping_all",
        logged=True,
        killable=True,
        pipeline=[PipelineStep(command=["/x/run-ansible.sh", "--run-id", "{run_id}"])],
    )
    ctx = _ctx(cfg, run_id="fixed-id")
    ctx.run_log_path = "/var/log/ansible-runs/fixed-id.log"
    ctx.conn = MagicMock()
    ctx.pipeline_cmds = [["/x/run-ansible.sh", "--run-id", "fixed-id"]]

    repo = MagicMock()
    saved = {}

    async def fake_save(state, ttl):
        saved["state"] = state

    repo.save = AsyncMock(side_effect=fake_save)
    repo.update = AsyncMock()

    svc = CommandService(repo=repo, inventory_repo=None)

    # Stop the background task from actually running SSH.
    monkeypatch.setattr(
        svc._executor, "_execute_pipeline", AsyncMock(return_value=MagicMock())
    )
    monkeypatch.setattr(
        svc._executor, "_collect_output", AsyncMock(return_value=(0, "ok"))
    )
    monkeypatch.setattr(svc._executor, "_store_result", AsyncMock())

    resp = await svc._executor._handle_async_execution(ctx, command_id="fixed-id")
    assert resp.command_id == "fixed-id"
    assert saved["state"].run_log_path == "/var/log/ansible-runs/fixed-id.log"


async def test_state_username_is_ssh_account_not_login_account(monkeypatch):
    """CommandState.username must be the SSH account (req.username='root'),
    not the deploy-service login account (context.username='admin'). Reading
    the log back and cross-pod kill reconnect via SSH using state.username."""
    cfg = CommandWhitelistConfig(
        command_name="run_ansible_ping_all",
        logged=True,
        killable=True,
        pipeline=[PipelineStep(command=["/x/run-ansible.sh"])],
    )
    ctx = _ctx(cfg, run_id="fixed-id")  # req.username='root', context.username='admin'
    ctx.conn = MagicMock()
    ctx.pipeline_cmds = [["/x/run-ansible.sh"]]

    repo = MagicMock()
    saved = {}

    async def fake_save(state, ttl):
        saved["state"] = state

    repo.save = AsyncMock(side_effect=fake_save)
    repo.update = AsyncMock()

    svc = CommandService(repo=repo, inventory_repo=None)
    monkeypatch.setattr(
        svc._executor, "_execute_pipeline", AsyncMock(return_value=MagicMock())
    )
    monkeypatch.setattr(
        svc._executor, "_collect_output", AsyncMock(return_value=(0, "ok"))
    )
    monkeypatch.setattr(svc._executor, "_store_result", AsyncMock())

    await svc._executor._handle_async_execution(ctx, command_id="fixed-id")
    assert saved["state"].username == "root"
