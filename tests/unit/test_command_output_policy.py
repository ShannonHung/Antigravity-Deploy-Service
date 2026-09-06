from app.services.command_service import CommandService
from app.core.config import get_settings


def _svc():
    return CommandService(repo=None, inventory_repo=None)


def test_non_logged_keeps_full_output():
    out = "\n".join(f"line{i}" for i in range(200))
    assert (
        _svc()._executor._apply_output_policy(logged=False, success=True, output=out)
        == out
    )
    assert (
        _svc()._executor._apply_output_policy(logged=False, success=False, output=out)
        == out
    )


def test_logged_success_drops_output():
    out = "\n".join(f"line{i}" for i in range(200))
    assert (
        _svc()._executor._apply_output_policy(logged=True, success=True, output=out)
        is None
    )


def test_logged_failure_keeps_last_50_lines():
    get_settings.cache_clear()
    n = get_settings().COMMAND_LOG_FAILURE_TAIL_LINES  # 50
    out = "\n".join(f"line{i}" for i in range(200))
    result = _svc()._executor._apply_output_policy(
        logged=True, success=False, output=out
    )
    kept = result.split("\n")
    assert len(kept) == n
    assert kept[0] == f"line{200 - n}"
    assert kept[-1] == "line199"


def test_logged_failure_shorter_than_tail_kept_whole():
    out = "only\ntwo lines"
    result = _svc()._executor._apply_output_policy(
        logged=True, success=False, output=out
    )
    assert result == out


def test_logged_failure_with_empty_output():
    assert _svc()._executor._apply_output_policy(
        logged=True, success=False, output=""
    ) in (None, "")
