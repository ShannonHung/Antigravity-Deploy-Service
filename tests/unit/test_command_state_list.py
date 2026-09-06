import pytest

from app.domain.command import CommandState, CommandStatus
from app.repositories.command_state_repository import CommandStateRepository


class _FakeRedis:
    """Minimal async Redis stand-in for scan_iter + get."""

    def __init__(self, data: dict[str, str]):
        self._data = data

    async def scan_iter(self, match=None):
        import fnmatch

        for k in list(self._data.keys()):
            if match is None or fnmatch.fnmatch(k, match):
                yield k

    async def get(self, key):
        return self._data.get(key)


def _state(cid, status):
    return CommandState(
        command_id=cid,
        status=status,
        host="h",
        resolved_ip="1.1.1.1",
        port=22,
        username="root",
        ssh_config="default",
        request_id="r",
        exec_command="true",
        killable=False,
    ).model_dump_json()


async def test_list_states_filters_by_status():
    redis = _FakeRedis(
        {
            "command:a": _state("a", CommandStatus.RUNNING),
            "command:b": _state("b", CommandStatus.KILLING),
            "command:c": _state("c", CommandStatus.SUCCESS),
            "other:x": "garbage",
        }
    )
    repo = CommandStateRepository(redis)
    result = await repo.list_states({CommandStatus.RUNNING, CommandStatus.KILLING})
    ids = sorted(s.command_id for s in result)
    assert ids == ["a", "b"]


async def test_list_states_skips_unparseable():
    redis = _FakeRedis(
        {
            "command:a": _state("a", CommandStatus.RUNNING),
            "command:bad": "not-json",
        }
    )
    repo = CommandStateRepository(redis)
    result = await repo.list_states()
    assert [s.command_id for s in result] == ["a"]


async def test_list_states_no_filter_returns_all_parseable():
    redis = _FakeRedis(
        {
            "command:a": _state("a", CommandStatus.RUNNING),
            "command:c": _state("c", CommandStatus.SUCCESS),
        }
    )
    repo = CommandStateRepository(redis)
    result = await repo.list_states()
    assert sorted(s.command_id for s in result) == ["a", "c"]
