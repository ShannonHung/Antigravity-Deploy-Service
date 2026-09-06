from types import SimpleNamespace

from app.services.pipeline_builder import PipelineBuilder


def _arg(name, required=True):
    return SimpleNamespace(name=name, required=required)


def _ctx(pipeline, arguments, arg_defs, run_id=None):
    cmd_config = SimpleNamespace(arguments=arg_defs, pipeline=pipeline)
    raw_request = SimpleNamespace(arguments=arguments)
    return SimpleNamespace(
        cmd_config=cmd_config, raw_request=raw_request, run_id=run_id
    )


def test_build_resolves_placeholders():
    pipeline = [SimpleNamespace(command=["ls", "{dir}"])]
    ctx = _ctx(pipeline, {"dir": "/tmp"}, [_arg("dir")])
    assert PipelineBuilder().build(ctx) == [["ls", "/tmp"]]


def test_build_injects_run_id():
    pipeline = [SimpleNamespace(command=["run", "{run_id}"])]
    ctx = _ctx(pipeline, {}, [], run_id="abc123")
    assert PipelineBuilder().build(ctx) == [["run", "abc123"]]


def test_build_strips_omitted_optional_with_preceding_flag():
    pipeline = [SimpleNamespace(command=["cmd", "--limit", "{limit}"])]
    ctx = _ctx(pipeline, {"limit": None}, [_arg("limit", required=False)])
    assert PipelineBuilder().build(ctx) == [["cmd"]]
