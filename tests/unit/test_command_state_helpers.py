from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.command_state_helpers import StateHelpers
from app.domain.command import CommandState, CommandStatus


def _helpers(repo=None, ssh=None):
    return StateHelpers(repo=repo or MagicMock(), ssh=ssh or MagicMock())


def test_exit_marker_path_swaps_log_suffix():
    h = _helpers()
    assert h._exit_marker_path("/runs/abc.log") == "/runs/abc.exit"


def test_exit_marker_path_appends_when_no_log_suffix():
    h = _helpers()
    assert h._exit_marker_path("/runs/abc") == "/runs/abc.exit"


async def test_heal_returns_state_unchanged_when_no_marker(monkeypatch):
    h = _helpers()
    state = CommandState(
        command_id="c1",
        status=CommandStatus.RUNNING,
        run_log_path="/runs/c1.log",
        host="h",
        resolved_ip="1.2.3.4",
        port=2224,
        username="root",
        ssh_config="control_node",
        request_id="r1",
        exec_command="x",
        killable=True,
    )
    # No marker yet -> _read_run_exit_marker returns None -> passthrough.
    h._read_run_exit_marker = AsyncMock(return_value=None)
    result = await h._heal_from_marker(state)
    assert result is state
