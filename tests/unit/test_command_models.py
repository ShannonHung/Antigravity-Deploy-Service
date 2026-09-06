from app.domain.command import (
    CommandState,
    CommandStatus,
    CommandWhitelistConfig,
    PipelineStep,
    CommandLogLine,
    CommandTraceResponse,
)


def test_command_state_run_log_path_defaults_none():
    state = CommandState(
        command_id="c1",
        status=CommandStatus.RUNNING,
        host="h",
        resolved_ip="1.2.3.4",
        port=22,
        username="root",
        ssh_config="default",
        request_id="r1",
        exec_command="echo hi",
        killable=True,
    )
    assert state.run_log_path is None
    state.run_log_path = "/var/log/ansible-runs/c1.log"
    assert state.run_log_path.endswith("c1.log")


def test_whitelist_logged_defaults_false():
    cfg = CommandWhitelistConfig(
        command_name="x",
        pipeline=[PipelineStep(command=["echo", "hi"])],
    )
    assert cfg.logged is False
    cfg2 = CommandWhitelistConfig(
        command_name="y",
        logged=True,
        pipeline=[PipelineStep(command=["echo", "hi"])],
    )
    assert cfg2.logged is True


def test_command_trace_response_shape():
    resp = CommandTraceResponse(
        command_id="c1",
        status="running",
        next_byte_offset=10,
        next_line_num=3,
        lines=[CommandLogLine(num=1, content_html="<span>hi</span>")],
    )
    assert resp.total_size == 0
    assert resp.size_warning is False
    assert resp.too_large is False
    assert resp.lines[0].num == 1
