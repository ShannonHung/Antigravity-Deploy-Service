"""Logged commands must survive deploy-service going away.

Root cause of the exit-141 bug: a logged run's stdout/stderr flowed back over
the SSH channel (asyncssh.PIPE). When deploy-service died, the channel closed,
tee got SIGPIPE, and the SIGPIPE cascade killed docker/ansible mid-run
(128+13=141).

Fix (option 1 + blind-spot): for logged commands, redirect the run's
stdout/stderr to the control_node log file and detach stdin (< /dev/null), so
the work no longer depends on the channel. Keep a two-line stderr handshake
(PGID + READY) so deploy-service can still (a) capture the PGID for kills and
(b) detect a start-up failure (command never reached exec) instead of hanging
in RUNNING forever.
"""

from app.services.command_service import CommandService


def _svc():
    return CommandService(repo=None, inventory_repo=None)


# ── the wrapper for a NON-logged step is unchanged (output still streamed) ─────


def test_non_logged_wrapper_streams_output_over_channel():
    # run_log_path=None → legacy behaviour: no redirect, output goes to the PIPE.
    wrapper = _svc()._executor._build_step_wrapper(run_log_path=None)
    joined = " ".join(wrapper)
    assert "setsid" in joined
    assert "echo $$ >&2" in joined  # PGID handshake kept
    assert joined.endswith('exec "$@" _')  # exec is the last thing — no redirect
    assert "/dev/null" not in joined  # not detached: output streams back
    assert "READY" not in joined  # no READY handshake for non-logged


# ── the wrapper for a LOGGED step detaches from the channel ───────────────────


def test_logged_wrapper_severs_channel_and_detaches():
    log = "/var/log/ansible-runs/abc.log"
    wrapper = _svc()._executor._build_step_wrapper(run_log_path=log)
    # The wrapper is [..., "sh", "-c", <script>, "_"]; inspect the sh script.
    script = wrapper[wrapper.index("-c") + 1]
    assert "echo $$ >&2" in script  # PGID handshake still first
    assert "READY" in script  # start-up confirmation line
    # Output severed from the SSH channel (→ /dev/null), stdin detached. The run
    # SCRIPT itself tees to the log file; we must NOT redirect to it too (double
    # write). Severing the channel is what stops the SIGPIPE cascade (exit 141).
    assert 'exec "$@" > /dev/null 2>&1 < /dev/null' in script
    # The handshake echoes must come BEFORE exec, so they reach the channel even
    # though exec's own output is redirected away.
    assert script.index("READY") < script.index('exec "$@"')


# ── blind-spot B: start-up failure is detected, not hung ──────────────────────

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.domain.command import (
    CommandExecutionRequest,
    CommandWhitelistConfig,
    PipelineStep,
    SSHConnectionConfig,
    ExecutionContext,
    HostType,
    RunningCommandEntry,
)
from app.repositories.host_resolver import ResolvedHost
import app.services.command_service as cs


def _logged_ctx(command_id):
    cfg = CommandWhitelistConfig(
        command_name="run_ansible",
        logged=True,
        killable=True,
        pipeline=[PipelineStep(command=["/x/run-ansible.sh", "--run-id", "{run_id}"])],
    )
    req = CommandExecutionRequest(
        command_name="run_ansible",
        host="localhost",
        host_type=HostType.IP,
        port=2224,
        username="root",
        ssh_config="control_node",
        arguments={},
    )
    ctx = ExecutionContext(
        username="admin",
        request_id="r1",
        command_name="run_ansible",
        raw_request=req,
        cmd_config=cfg,
        ssh_config=SSHConnectionConfig(auth_method="key", key_base64="x"),
        resolved_host=ResolvedHost(ip="1.2.3.4", source_input="localhost"),
    )
    ctx.run_id = command_id
    ctx.run_log_path = f"/var/log/ansible-runs/{command_id}.log"
    ctx.conn = MagicMock()
    ctx.pipeline_cmds = [["/x/run-ansible.sh", "--run-id", command_id]]
    return ctx


def _fake_process(stderr_lines, returncode=0):
    """A fake asyncssh process whose stderr yields the given handshake lines."""
    p = MagicMock()
    lines = list(stderr_lines)

    async def readline():
        return lines.pop(0) if lines else ""

    p.stderr.readline = AsyncMock(side_effect=readline)
    p.stdout = MagicMock()
    p.wait = AsyncMock()
    p.returncode = returncode
    return p


async def test_logged_startup_failure_raises_when_no_ready(monkeypatch):
    # The command died before exec (e.g. run-ansible.sh not found): the channel
    # gives the PGID line maybe, but EOF instead of READY. This MUST surface as
    # a failure, not hang the run in RUNNING.
    cmd_id = "blind-spot"
    ctx = _logged_ctx(cmd_id)
    cs.pool_add(
        cmd_id,
        RunningCommandEntry(
            host_ip="1.2.3.4",
            killable=True,
            conn=ctx.conn,
        ),
    )
    # stderr yields a PGID then EOF — never READY.
    proc = _fake_process(["906\n", ""])
    ctx.conn.create_process = AsyncMock(return_value=proc)

    svc = CommandService(repo=MagicMock(), inventory_repo=None)
    try:
        with __import__("pytest").raises(Exception):
            await svc._executor._execute_pipeline(ctx, cmd_id, "preview")
    finally:
        cs.pool_remove(cmd_id)


async def test_logged_startup_success_captures_pgid(monkeypatch):
    # Happy path: PGID then READY → proceed, PGID captured for kills.
    cmd_id = "ok-start"
    ctx = _logged_ctx(cmd_id)
    entry = RunningCommandEntry(host_ip="1.2.3.4", killable=True, conn=ctx.conn)
    cs.pool_add(cmd_id, entry)
    proc = _fake_process(["906\n", "READY\n"])
    ctx.conn.create_process = AsyncMock(return_value=proc)

    svc = CommandService(repo=MagicMock(), inventory_repo=None)
    try:
        final = await svc._executor._execute_pipeline(ctx, cmd_id, "preview")
    finally:
        cs.pool_remove(cmd_id)
    assert final is proc
    assert entry.pgids == [906]
