"""Unit tests for DeployService duplicate detection logic."""

from unittest.mock import AsyncMock

import pytest

from app.domain.pipeline_models import PipelineData, PipelineVariable
from app.services.deploy_service import DeployService

_TRIGGER_FROM = "SERVICE_FROM"


def _make_pipeline(variables: dict[str, str]) -> PipelineData:
    return PipelineData(
        id=1,
        status="running",
        created_at=None,
        updated_at=None,
        started_at=None,
        finished_at=None,
        tag_list=[],
        variables=[PipelineVariable(key=k, value=v) for k, v in variables.items()],
        jobs=[],
        downstream_pipelines=[],
        ref_name="main",
        web_url="",
    )


def _make_service(running: list[PipelineData]) -> DeployService:
    repo = AsyncMock()
    repo.list_running.return_value = running
    return DeployService(pipeline_repo=repo)


async def test_duplicate_detected_same_user():
    """Same user re-triggering same action on same ref is blocked."""
    running = [_make_pipeline({"EXECUTION": "deploy", _TRIGGER_FROM: "alice"})]
    svc = _make_service(running)

    result = await svc.find_duplicate_pipelines(
        action="deploy",
        ref="main",
        extra_variables=[PipelineVariable(key=_TRIGGER_FROM, value="alice")],
    )

    assert result.has_running is True


async def test_duplicate_detected_different_user():
    """Different user triggering same action/ref must also be blocked — TRIGGER_FROM excluded from match."""
    running = [_make_pipeline({"EXECUTION": "deploy", _TRIGGER_FROM: "alice"})]
    svc = _make_service(running)

    result = await svc.find_duplicate_pipelines(
        action="deploy",
        ref="main",
        extra_variables=[PipelineVariable(key=_TRIGGER_FROM, value="bob")],
    )

    assert (
        result.has_running is True
    ), "Different TRIGGER_FROM values must NOT prevent duplicate detection"


async def test_no_duplicate_different_action():
    """Different action on same ref is not a duplicate."""
    running = [_make_pipeline({"EXECUTION": "rollback", _TRIGGER_FROM: "alice"})]
    svc = _make_service(running)

    result = await svc.find_duplicate_pipelines(
        action="deploy",
        ref="main",
        extra_variables=[PipelineVariable(key=_TRIGGER_FROM, value="alice")],
    )

    assert result.has_running is False
