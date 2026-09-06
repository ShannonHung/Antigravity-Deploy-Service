"""Optional whitelist arguments (required: bool).

A single generic `run_ansible` entry needs per-playbook optional args (clock
needs clock_count/interval; ping does not). `required` lets one entry declare
args that may be omitted; omitted optional args are simply not injected.
"""

import pytest
from app.domain.command import (
    CommandArgumentConfig,
    CommandExecutionRequest,
    CommandWhitelistConfig,
    PipelineStep,
    SSHConnectionConfig,
    ExecutionContext,
    HostType,
)
from app.repositories.host_resolver import ResolvedHost
from app.services.command_service import CommandService
from app.core.exceptions import CommandExecutionException


def test_argument_required_defaults_true():
    a = CommandArgumentConfig(name="inventory", type="string")
    assert a.required is True
    b = CommandArgumentConfig(name="limit", type="string", required=False)
    assert b.required is False


# ── validation: optional args may be omitted ─────────────────────────────────

import asyncio
from unittest.mock import AsyncMock, MagicMock


def _whitelist(cmd):
    from app.domain.command import UserCommandWhitelist

    return UserCommandWhitelist(name="admin", allow_commands=[cmd])


def _svc_for_prepare(cmd, monkeypatch):
    svc = CommandService(repo=MagicMock(), inventory_repo=None)
    monkeypatch.setattr(
        svc._executor, "_load_user_whitelist", lambda u: _whitelist(cmd)
    )
    monkeypatch.setattr(
        svc._ssh,
        "_load_ssh_config",
        lambda t: SSHConnectionConfig(auth_method="key", key_base64="x"),
    )

    fake_resolver = MagicMock()
    fake_resolver.resolve = AsyncMock(
        return_value=ResolvedHost(ip="1.2.3.4", source_input="localhost")
    )
    monkeypatch.setattr(
        "app.services.command_executor.create_host_resolver",
        lambda *a, **k: fake_resolver,
    )
    return svc


def _req(args):
    return CommandExecutionRequest(
        command_name="run_ansible",
        host="localhost",
        port=2224,
        username="root",
        ssh_config="control_node",
        arguments=args,
    )


CMD = CommandWhitelistConfig(
    command_name="run_ansible",
    killable=True,
    logged=True,
    pipeline=[
        PipelineStep(
            command=[
                "/x/run-ansible.sh",
                "--inventory",
                "{inventory}",
                "--limit",
                "{limit}",
            ]
        )
    ],
    arguments=[
        CommandArgumentConfig(
            name="inventory", type="string", validation_regex=r"^[a-z./]+$"
        ),
        CommandArgumentConfig(
            name="limit",
            type="string",
            required=False,
            validation_regex=r"^[a-z0-9,]+$",
        ),
    ],
)


async def test_missing_optional_arg_is_allowed(monkeypatch):
    svc = _svc_for_prepare(CMD, monkeypatch)
    # No 'limit' supplied — must NOT raise.
    ctx = await svc._executor._prepare_execution(
        "admin", "r1", _req({"inventory": "a/b.ini"})
    )
    assert ctx is not None


async def test_missing_required_arg_still_raises(monkeypatch):
    svc = _svc_for_prepare(CMD, monkeypatch)
    with pytest.raises(CommandExecutionException):
        await svc._executor._prepare_execution(
            "admin", "r1", _req({"limit": "node1"})
        )  # inventory missing


async def test_supplied_optional_arg_is_regex_validated(monkeypatch):
    svc = _svc_for_prepare(CMD, monkeypatch)
    with pytest.raises(CommandExecutionException):
        # 'limit' supplied but violates its regex (uppercase).
        await svc._executor._prepare_execution(
            "admin", "r1", _req({"inventory": "a/b.ini", "limit": "NODE!"})
        )


# ── _build_pipeline drops the flag+value pair of an omitted optional arg ──────


def _ctx_for_build(args, run_id=None):
    req = _req(args)
    ctx = ExecutionContext(
        username="admin",
        request_id="r1",
        command_name="run_ansible",
        raw_request=req,
        cmd_config=CMD,
        ssh_config=SSHConnectionConfig(auth_method="key", key_base64="x"),
        resolved_host=ResolvedHost(ip="1.2.3.4", source_input="localhost"),
    )
    ctx.run_id = run_id
    return ctx


def test_build_pipeline_keeps_supplied_optional():
    svc = CommandService(repo=None, inventory_repo=None)
    flat = svc._executor._pipeline_builder.build(
        _ctx_for_build({"inventory": "a/b.ini", "limit": "node1"})
    )[0]
    assert flat == ["/x/run-ansible.sh", "--inventory", "a/b.ini", "--limit", "node1"]


def test_build_pipeline_drops_omitted_optional_flag_and_value():
    svc = CommandService(repo=None, inventory_repo=None)
    flat = svc._executor._pipeline_builder.build(
        _ctx_for_build({"inventory": "a/b.ini"})
    )[0]
    # --limit and its {limit} value must both be gone; required parts stay.
    assert flat == ["/x/run-ansible.sh", "--inventory", "a/b.ini"]
    assert "--limit" not in flat
    assert not any("{" in tok for tok in flat)
