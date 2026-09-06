"""Optional JSON parsing of a finished command's stdout (?format=json).

The load-bearing rule under test is WHEN parsing is attempted. `CommandState.
output` has four producers and only one of them is real stdout, so a naive
"always try to parse" would emit `parse_failed` on every failed run and drown
the signal that a script actually broke its contract.
"""

import pytest

from app.core.exceptions import CommandExecutionException
from app.domain.command import (
    CommandOutputFormat,
    CommandState,
    CommandStatus,
    CommandWhitelistConfig,
    OutputFormat,
    OutputJsonError,
    PipelineStep,
)
from app.services.command_output_format import ensure_format_allowed, parse_output


def _state(**over):
    base = dict(
        command_id="c1",
        status=CommandStatus.SUCCESS,
        host="h",
        resolved_ip="1.2.3.4",
        port=22,
        username="root",
        ssh_config="default",
        request_id="r1",
        exec_command="x",
        killable=True,
        output_format=CommandOutputFormat.JSON,
        output='{"ok": true}',
    )
    base.update(over)
    return CommandState(**base)


# ── ensure_format_allowed: the caller-vs-contract gate ───────────────────────


def test_raw_format_never_rejected_even_for_text_commands():
    # The backward-compatibility guarantee: an existing caller (no ?format)
    # must never be able to reach the new failure path.
    ensure_format_allowed(
        _state(output_format=CommandOutputFormat.TEXT), OutputFormat.RAW
    )


def test_json_requested_on_text_command_is_rejected():
    state = _state(output_format=CommandOutputFormat.TEXT)
    with pytest.raises(CommandExecutionException) as exc:
        ensure_format_allowed(state, OutputFormat.JSON)
    assert exc.value.http_status == 400
    assert exc.value.detail["output_format"] == "text"


def test_json_requested_on_json_command_is_allowed():
    ensure_format_allowed(_state(), OutputFormat.JSON)


# ── parse_output: the four producers of `output` ─────────────────────────────


def test_success_with_real_stdout_parses():
    parsed, err = parse_output(_state(output='{"a": 1, "b": [2, 3]}'))
    assert parsed == {"a": 1, "b": [2, 3]}
    assert err is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("[1, 2, 3]", [1, 2, 3]),  # top-level array
        ("42", 42),  # bare scalar
        ('"hello"', "hello"),
        ("null", None),
    ],
)
def test_any_json_toplevel_is_accepted(raw, expected):
    # Typed `Any` deliberately: the source may be any small script, and
    # restricting to objects would reject valid payloads.
    parsed, err = parse_output(_state(output=raw))
    assert parsed == expected
    assert err is None


def test_success_with_invalid_json_is_parse_failed():
    # Producer 1 gave us real stdout, but it isn't JSON — the remote script
    # broke the contract its whitelist entry declared.
    parsed, err = parse_output(_state(output="not json at all"))
    assert parsed is None
    assert err is OutputJsonError.PARSE_FAILED


def test_healed_success_with_empty_output_is_output_unavailable():
    # Producer 3: cross-pod heal reconstructs the exit code from the marker and
    # calls mark_success(code, "") — stdout was never recovered. Distinct from a
    # broken script.
    parsed, err = parse_output(_state(output=""))
    assert parsed is None
    assert err is OutputJsonError.OUTPUT_UNAVAILABLE


def test_healed_success_with_none_output_is_output_unavailable():
    parsed, err = parse_output(_state(output=None))
    assert parsed is None
    assert err is OutputJsonError.OUTPUT_UNAVAILABLE


@pytest.mark.parametrize(
    "status",
    [
        CommandStatus.FAILED,
        CommandStatus.RUNNING,
        CommandStatus.KILLING,
        CommandStatus.KILLED,
    ],
)
def test_non_success_states_are_not_applicable(status):
    # Producer 2: on failure `output` is a log TAIL, not stdout. Reporting
    # parse_failed for every failed ansible run would make the ERROR-level
    # parse_failed signal meaningless.
    parsed, err = parse_output(
        _state(status=status, output="TASK [foo] ok\nPLAY RECAP")
    )
    assert parsed is None
    assert err is OutputJsonError.NOT_APPLICABLE


def test_parse_failure_reason_is_a_closed_enum_not_decoder_text():
    # The raw JSONDecodeError quotes the offending fragment of remote output,
    # which may be sensitive. The reason must therefore be one of the three
    # fixed enum members and never carry decoder detail. (The response-body
    # leak check lives in the integration test, which sees the serialized JSON.)
    secret = '{"token": "s3cr3t-do-not-echo"'  # truncated → invalid
    parsed, err = parse_output(_state(output=secret))
    assert parsed is None
    assert err in set(OutputJsonError)
    assert err is OutputJsonError.PARSE_FAILED


def test_rejection_detail_does_not_echo_command_output():
    # ensure_format_allowed builds a `detail` dict that reaches the client via
    # the error envelope; it must describe the CONFIG, never the output.
    state = _state(
        output_format=CommandOutputFormat.TEXT,
        output='{"token": "s3cr3t-do-not-echo"}',
    )
    with pytest.raises(CommandExecutionException) as exc:
        ensure_format_allowed(state, OutputFormat.JSON)
    assert "s3cr3t" not in repr(exc.value.detail)
    assert "s3cr3t" not in str(exc.value)


def test_parse_failed_logged_at_error(caplog):
    with caplog.at_level("ERROR"):
        parse_output(_state(output="nope"))
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_output_unavailable_logged_at_warning_not_error(caplog):
    with caplog.at_level("WARNING"):
        parse_output(_state(output=""))
    assert any(r.levelname == "WARNING" for r in caplog.records)
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_not_applicable_is_not_logged(caplog):
    with caplog.at_level("WARNING"):
        parse_output(_state(status=CommandStatus.FAILED, output="tail"))
    assert caplog.records == []


# ── contract snapshot on CommandState ────────────────────────────────────────


def test_output_format_defaults_to_text_for_states_written_by_older_pods():
    # A state serialised before this field existed must still deserialise
    # during a rolling upgrade, and must default to the safe/no-op value.
    base = dict(
        command_id="c1",
        status=CommandStatus.SUCCESS,
        host="h",
        resolved_ip="1.2.3.4",
        port=22,
        username="root",
        ssh_config="default",
        request_id="r1",
        exec_command="x",
        killable=True,
    )
    assert CommandState(**base).output_format is CommandOutputFormat.TEXT


# ── whitelist validation: combinations that could never produce JSON ─────────


def _cfg(**over):
    base = dict(
        command_name="report_json",
        pipeline=[PipelineStep(command=["python3", "report.py"])],
        output_format=CommandOutputFormat.JSON,
    )
    base.update(over)
    return CommandWhitelistConfig(**base)


def test_json_output_format_alone_is_valid():
    assert _cfg().output_format is CommandOutputFormat.JSON


def test_json_output_format_rejects_logged_commands():
    # A logged command persists NO output on success (_apply_output_policy
    # returns None), so ?format=json could only ever report
    # output_unavailable — an alarm the operator cannot act on. Fail at load.
    with pytest.raises(ValueError, match="logged=true"):
        _cfg(logged=True)


def test_json_output_format_rejects_fire_and_forget_commands():
    # disconnects_ssh commands return no command_id and never write a
    # CommandState, so the poll endpoint is unreachable for them.
    with pytest.raises(ValueError, match="disconnects_ssh=true"):
        _cfg(disconnects_ssh=True)


@pytest.mark.parametrize("flag", ["logged", "disconnects_ssh"])
def test_text_output_format_still_allows_those_flags(flag):
    # The new rule constrains output_format json only — existing commands that
    # use these flags must keep loading unchanged.
    cfg = _cfg(output_format=CommandOutputFormat.TEXT, **{flag: True})
    assert cfg.output_format is CommandOutputFormat.TEXT
