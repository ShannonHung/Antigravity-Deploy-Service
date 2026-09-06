"""
tests/unit/test_gitlab_pipeline_repository.py

Unit tests for GitlabPipelineRepository helpers that do not require
a real GitLab connection. The python-gitlab objects are mocked.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import gitlab.exceptions

from app.repositories.gitlab_pipeline_repository import GitlabPipelineRepository


def _make_repo() -> GitlabPipelineRepository:
    """Build a repo without hitting the network — the helper under test
    does not use the gitlab.Gitlab client directly; it only uses the
    *project* object that is passed into it."""
    return GitlabPipelineRepository(
        url="http://example.invalid",
        token="dummy",
        project_id=1,
    )


def _make_pipeline_mock(
    bridges: list = (), jobs: list = (), variables: list = ()
) -> MagicMock:
    """Return a mocked python-gitlab Pipeline object with sub-resource lists."""
    pipeline = MagicMock()
    pipeline.bridges.list.return_value = list(bridges)
    pipeline.jobs.list.return_value = list(jobs)
    pipeline.variables.list.return_value = list(variables)
    return pipeline


def test_collect_downstream_pipelines_returns_fired_bridges():
    bridge = SimpleNamespace(
        name="trigger:deploy-prod",
        downstream_pipeline={
            "id": 999,
            "status": "running",
            "web_url": "http://gitlab.example/-/pipelines/999",
            "project_id": 42,
        },
    )
    pipeline = _make_pipeline_mock(bridges=[bridge])
    repo = _make_repo()

    result = repo._collect_downstream_pipelines(pipeline, pipeline_id=1)

    assert len(result) == 1
    entry = result[0]
    assert entry.id == 999
    assert entry.status == "running"
    assert entry.web_url == "http://gitlab.example/-/pipelines/999"
    assert entry.project_id == 42
    assert entry.bridge_name == "trigger:deploy-prod"


def test_collect_downstream_pipelines_skips_bridges_without_downstream():
    fired = SimpleNamespace(
        name="trigger:deploy-prod",
        downstream_pipeline={
            "id": 101,
            "status": "pending",
            "web_url": "http://gitlab.example/-/pipelines/101",
            "project_id": 42,
        },
    )
    pending = SimpleNamespace(
        name="trigger:deploy-staging",
        downstream_pipeline=None,
    )
    pipeline = _make_pipeline_mock(bridges=[fired, pending])
    repo = _make_repo()

    result = repo._collect_downstream_pipelines(pipeline, pipeline_id=1)

    assert len(result) == 1
    assert result[0].id == 101
    assert result[0].bridge_name == "trigger:deploy-prod"


def test_collect_downstream_pipelines_preserves_cross_project_id():
    """A downstream in a different GitLab project must surface its own
    project_id so the client can route the follow-up call correctly."""
    bridge = SimpleNamespace(
        name="trigger:other-repo",
        downstream_pipeline={
            "id": 555,
            "status": "running",
            "web_url": "http://gitlab.example/other/-/pipelines/555",
            "project_id": 7777,
        },
    )
    pipeline = _make_pipeline_mock(bridges=[bridge])
    repo = _make_repo()

    result = repo._collect_downstream_pipelines(pipeline, pipeline_id=1)

    assert result[0].project_id == 7777


def test_collect_downstream_pipelines_returns_empty_on_gitlab_error():
    pipeline = MagicMock()
    pipeline.bridges.list.side_effect = gitlab.exceptions.GitlabListError("boom")
    repo = _make_repo()

    result = repo._collect_downstream_pipelines(pipeline, pipeline_id=1)

    assert result == []


def test_to_pipeline_data_includes_downstream_pipelines():
    """_to_pipeline_data must populate downstream_pipelines on the returned PipelineData."""
    bridge = SimpleNamespace(
        name="trigger:deploy-prod",
        downstream_pipeline={
            "id": 999,
            "status": "running",
            "web_url": "http://gitlab.example/-/pipelines/999",
            "project_id": 42,
        },
    )
    pipeline = _make_pipeline_mock(bridges=[bridge])
    pipeline.id = 1
    pipeline.status = "running"
    pipeline.created_at = "2026-05-16T00:00:00Z"
    pipeline.updated_at = "2026-05-16T00:00:00Z"
    pipeline.started_at = None
    pipeline.finished_at = None
    pipeline.ref = "main"
    pipeline.web_url = "http://gitlab.example/-/pipelines/1"

    repo = _make_repo()
    data = repo._to_pipeline_data(pipeline)

    assert len(data.downstream_pipelines) == 1
    assert data.downstream_pipelines[0].id == 999
    assert data.downstream_pipelines[0].bridge_name == "trigger:deploy-prod"
