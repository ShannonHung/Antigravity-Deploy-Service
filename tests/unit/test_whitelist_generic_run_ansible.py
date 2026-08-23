"""The whitelist exposes a single generic `run_ansible` entry (playbook chosen
by a regex-restricted argument, optional --limit) plus a dedicated
`run_ansible_clock` for the live-log demo (optional clock_count/interval)."""
import json
from pathlib import Path

import pytest

from app.domain.command import (
    UserCommandWhitelist, CommandExecutionRequest, ExecutionContext,
    SSHConnectionConfig,
)
from app.repositories.host_resolver import ResolvedHost
from app.services.command_service import CommandService
from app.core.exceptions import CommandExecutionException

DATA = Path(__file__).resolve().parents[2] / "data"


def _wl():
    # The tracked template, not the untracked machine-specific real file —
    # these assert the whitelist's shape, which the template carries.
    return UserCommandWhitelist(
        **json.loads((DATA / "allow-commands-admin.example.json").read_text())
    )


def _cmd(name):
    return next(c for c in _wl().allow_commands if c.command_name == name)


def test_generic_run_ansible_exists_with_playbook_arg():
    cmd = _cmd("run_ansible")
    assert cmd.logged is True and cmd.killable is True
    args = {a.name: a for a in cmd.arguments}
    assert "playbook" in args and "inventory" in args
    # playbook is regex-restricted to the baked-in playbooks.
    assert args["playbook"].validation_regex, "playbook must have a whitelist regex"
    assert args["playbook"].required is True
    # limit is optional.
    assert args["limit"].required is False


def test_playbook_regex_allows_known_rejects_unknown():
    import re
    rx = {a.name: a for a in _cmd("run_ansible").arguments}["playbook"].validation_regex
    assert re.match(rx, "ping.yml")
    assert re.match(rx, "fail.yml")
    assert not re.match(rx, "evil.yml")
    assert not re.match(rx, "../etc/passwd")


def test_clock_entry_kept_with_optional_tunables():
    cmd = _cmd("run_ansible_clock")
    args = {a.name: a for a in cmd.arguments}
    assert args["clock_count"].required is False
    assert args["clock_interval"].required is False


def _ctx(cmd, args, run_id="rid"):
    req = CommandExecutionRequest(
        command_name=cmd.command_name, host="localhost", port=2224,
        username="root", ssh_config="control_node", arguments=args,
    )
    ctx = ExecutionContext(
        username="admin", request_id="r1", command_name=cmd.command_name,
        raw_request=req, cmd_config=cmd,
        ssh_config=SSHConnectionConfig(auth_method="key", key_base64="x"),
        resolved_host=ResolvedHost(ip="1.2.3.4", source_input="localhost"),
    )
    ctx.run_id = run_id
    return ctx


def test_build_generic_with_limit():
    svc = CommandService(repo=None, inventory_repo=None)
    flat = svc._executor._pipeline_builder.build(_ctx(_cmd("run_ansible"),
                                    {"playbook": "ping.yml", "inventory": "t/m.ini", "limit": "node1"}))[0]
    assert "ping.yml" in flat
    assert flat[flat.index("--limit") + 1] == "node1"
    assert "{" not in " ".join(flat)


def test_build_generic_without_limit_drops_flag():
    svc = CommandService(repo=None, inventory_repo=None)
    flat = svc._executor._pipeline_builder.build(_ctx(_cmd("run_ansible"),
                                    {"playbook": "ping.yml", "inventory": "t/m.ini"}))[0]
    assert "--limit" not in flat
    assert "{" not in " ".join(flat)
    assert flat[flat.index("--run-id") + 1] == "rid"


# ── clock tunables are independent: each --extra-vars pair stands alone ───────

def _build_clock(args):
    svc = CommandService(repo=None, inventory_repo=None)
    return svc._executor._pipeline_builder.build(_ctx(_cmd("run_ansible_clock"), args))[0]


def test_clock_both_tunables():
    flat = _build_clock({"inventory": "t/m.ini", "clock_count": 30, "clock_interval": 2})
    assert "clock_count=30" in flat and "clock_interval=2" in flat
    assert "{" not in " ".join(flat)


def test_clock_no_tunables_uses_defaults():
    flat = _build_clock({"inventory": "t/m.ini"})
    assert not any("clock_count" in t or "clock_interval" in t for t in flat)
    assert "{" not in " ".join(flat)


def test_clock_only_count_keeps_count_drops_interval():
    # The bug we guard against: filling one must not silently drop it.
    flat = _build_clock({"inventory": "t/m.ini", "clock_count": 30})
    assert "clock_count=30" in flat
    assert not any("clock_interval" in t for t in flat)
    assert "{" not in " ".join(flat)
