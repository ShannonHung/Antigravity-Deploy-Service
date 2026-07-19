"""Fast-path output backfill: a failed `logged` command has an empty SSH
channel (output → /dev/null on the target). The executor must backfill the
persisted output from the control_node log tail so the API shows the failure.

These tests exercise the pure policy seam `_maybe_backfill_output`, which
decides whether to read the log tail, keeping the async task wiring untested
here (covered by integration).
"""
from unittest.mock import AsyncMock, MagicMock

from app.domain.command import CommandState, CommandStatus
from app.services.command_service import CommandService


def _state(**over):
    base = dict(
        command_id="c1", status=CommandStatus.RUNNING, host="h",
        resolved_ip="1.2.3.4", port=2224, username="root",
        ssh_config="control_node", request_id="r1", exec_command="x",
        killable=True, run_log_path="/var/log/deploy-service/c1.log",
    )
    base.update(over)
    return CommandState(**base)


def _executor():
    return CommandService(repo=None, inventory_repo=None)._executor


async def test_backfill_reads_tail_when_logged_failed_and_empty(monkeypatch):
    ex = _executor()
    monkeypatch.setattr(
        ex._ssh, "_read_log_tail", AsyncMock(return_value="fatal: clone failed\n"))
    out = await ex._maybe_backfill_output(
        state=_state(), logged=True, success=False, output="")
    assert out == "fatal: clone failed\n"


async def test_backfill_skipped_when_output_present(monkeypatch):
    ex = _executor()
    reader = AsyncMock(return_value="SHOULD-NOT-BE-USED")
    monkeypatch.setattr(ex._ssh, "_read_log_tail", reader)
    out = await ex._maybe_backfill_output(
        state=_state(), logged=True, success=False, output="real channel output")
    assert out == "real channel output"
    reader.assert_not_awaited()


async def test_backfill_skipped_when_success(monkeypatch):
    ex = _executor()
    reader = AsyncMock(return_value="SHOULD-NOT-BE-USED")
    monkeypatch.setattr(ex._ssh, "_read_log_tail", reader)
    out = await ex._maybe_backfill_output(
        state=_state(), logged=True, success=True, output="")
    assert out == ""
    reader.assert_not_awaited()


async def test_backfill_skipped_when_not_logged(monkeypatch):
    ex = _executor()
    reader = AsyncMock(return_value="SHOULD-NOT-BE-USED")
    monkeypatch.setattr(ex._ssh, "_read_log_tail", reader)
    out = await ex._maybe_backfill_output(
        state=_state(), logged=False, success=False, output="")
    assert out == ""
    reader.assert_not_awaited()


async def test_backfill_keeps_empty_when_tail_none(monkeypatch):
    ex = _executor()
    monkeypatch.setattr(ex._ssh, "_read_log_tail", AsyncMock(return_value=None))
    out = await ex._maybe_backfill_output(
        state=_state(), logged=True, success=False, output="")
    assert out == ""
