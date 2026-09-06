"""
app/api/v1/deploy.py

GitLab pipeline deployment endpoints (v1).
All endpoints require the ``deploy_api`` scope.

Route layout:
  POST /api/v1/deploy/stage                      → trigger pipeline
  GET  /api/v1/deploy/stage/{pipeline_id}        → get pipeline status
  POST /api/v1/deploy/stage/{pipeline_id}/cancel → cancel pipeline
  POST /api/v1/deploy/stage/{pipeline_id}/retry  → retry pipeline
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from app.core.config import get_settings
from app.core.dependencies import (
    get_current_user,
    get_current_user_cookie_or_header,
    get_trace_cache_repository,
)
from app.core.log_viewer_template import LOG_VIEWER_HTML
from app.domain.models import ApiResponse, User
from app.domain.pipeline_models import (
    PipelineData,
    PipelineVariable,
    RunningPipelinesData,
    TriggerPipelineRequest,
    FormattedLogResponse,
)
from app.repositories.gitlab_auth_repository import GitlabAuthRepository
from app.repositories.gitlab_pipeline_repository import GitlabPipelineRepository
from app.repositories.trace_cache_repository import TraceCacheRepository
from app.services.deploy_service import DeployService

_logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/deploy",
    tags=["deploy"],
)


def _get_deploy_service(
    project_id: int | None = None,
    trace_cache: TraceCacheRepository | None = None,
) -> DeployService:
    """Build DeployService with resolved project/token mapping.

    ``trace_cache`` is only used by the job-trace endpoint; other endpoints
    can omit it without losing functionality.
    """
    settings = get_settings()
    target_project_id = project_id or settings.GITLAB_PROJECT_ID
    target_token = settings.GITLAB_TOKEN

    if project_id and project_id != settings.GITLAB_PROJECT_ID:
        auth_repo = GitlabAuthRepository(settings.GITLAB_AUTH_JSON_PATH)
        token = auth_repo.get_token_by_project_id(project_id)
        if token:
            target_token = token
        else:
            _logger.warning(
                "Project ID %s not found in auth mapping, falling back to default token.",
                project_id,
            )

    repo = GitlabPipelineRepository(
        url=settings.GITLAB_URL,
        token=target_token,
        project_id=target_project_id,
        trace_cache=trace_cache,
    )
    return DeployService(repo)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


# ── POST /api/v1/deploy/stage ─────────────────────────────────────────────────


@router.post(
    "/stage",
    response_model=ApiResponse[PipelineData],
    summary="Trigger a GitLab pipeline",
    description=(
        "Triggers a new GitLab pipeline on the specified ref. "
        "The `action` query param is forwarded as the `EXECUTION` pipeline variable. "
        "Body variables are merged in (EXECUTION always wins if duplicated)."
    ),
)
async def trigger_pipeline(
    request: Request,
    action: str = Query(
        ..., description="Pipeline EXECUTION variable value (e.g. test-deploy)"
    ),
    ref_name: str = Query(
        default="main", description="Git branch or tag to run pipeline on"
    ),
    project_id: int | None = Query(None, description="GitLab project ID"),
    body: TriggerPipelineRequest = TriggerPipelineRequest(),
    current_user: Annotated[User, Depends(get_current_user(["deploy_api"]))] = None,
) -> ApiResponse[PipelineData]:
    svc = _get_deploy_service(project_id)
    variables = [
        PipelineVariable(key="SERVICE_FROM", value=current_user.account),
        *body.variables,
    ]
    data = await svc.trigger_pipeline(
        action=action,
        ref=ref_name,
        extra_variables=variables,
    )
    return ApiResponse(data=data, request_id=_request_id(request))


# ── POST /api/v1/deploy/stage/check-running ─────────────────────────────────


@router.post(
    "/stage/check-running",
    response_model=ApiResponse[RunningPipelinesData],
    summary="Check for duplicate running pipelines",
    description=(
        "Returns all active (created / pending / running / …) pipelines on *ref_name* "
        "whose variables exactly match *action* + body variables. "
        "Use this before triggering to preview what would be blocked. "
        "The trigger endpoint performs this check automatically."
    ),
)
async def check_running(
    request: Request,
    action: str = Query(..., description="EXECUTION variable value to match"),
    ref_name: str = Query(default="main", description="Branch or tag to filter on"),
    project_id: int | None = Query(None, description="GitLab project ID"),
    body: TriggerPipelineRequest = TriggerPipelineRequest(),
    current_user: Annotated[User, Depends(get_current_user(["deploy_api"]))] = None,
) -> ApiResponse[RunningPipelinesData]:
    svc = _get_deploy_service(project_id)
    data = await svc.find_duplicate_pipelines(
        action=action,
        ref=ref_name,
        extra_variables=body.variables,
    )
    return ApiResponse(data=data, request_id=_request_id(request))


# ── GET /api/v1/deploy/stage/{pipeline_id} ───────────────────────────────────


@router.get(
    "/stage/{pipeline_id}",
    response_model=ApiResponse[PipelineData],
    summary="Get pipeline status",
    description="Returns the current state of an existing pipeline.",
)
async def get_pipeline(
    request: Request,
    pipeline_id: int,
    project_id: int | None = Query(None, description="GitLab project ID"),
    current_user: Annotated[User, Depends(get_current_user(["deploy_api"]))] = None,
) -> ApiResponse[PipelineData]:
    svc = _get_deploy_service(project_id)
    data = await svc.get_pipeline(pipeline_id)
    return ApiResponse(data=data, request_id=_request_id(request))


# ── POST /api/v1/deploy/stage/{pipeline_id}/cancel ───────────────────────────


@router.post(
    "/stage/{pipeline_id}/cancel",
    response_model=ApiResponse[PipelineData],
    summary="Cancel a pipeline",
    description="Cancels a running pipeline and returns its updated status.",
)
async def cancel_pipeline(
    request: Request,
    pipeline_id: int,
    project_id: int | None = Query(None, description="GitLab project ID"),
    current_user: Annotated[User, Depends(get_current_user(["deploy_api"]))] = None,
) -> ApiResponse[PipelineData]:
    svc = _get_deploy_service(project_id)
    data = await svc.cancel_pipeline(pipeline_id)
    return ApiResponse(data=data, request_id=_request_id(request))


# ── POST /api/v1/deploy/stage/{pipeline_id}/retry ────────────────────────────


@router.post(
    "/stage/{pipeline_id}/retry",
    response_model=ApiResponse[PipelineData],
    summary="Retry a pipeline",
    description="Retries a failed or cancelled pipeline and returns the new state.",
)
async def retry_pipeline(
    request: Request,
    pipeline_id: int,
    project_id: int | None = Query(None, description="GitLab project ID"),
    current_user: Annotated[User, Depends(get_current_user(["deploy_api"]))] = None,
) -> ApiResponse[PipelineData]:
    svc = _get_deploy_service(project_id)
    data = await svc.retry_pipeline(pipeline_id)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/jobs/{job_id}/trace/ui",
    response_model=ApiResponse[FormattedLogResponse],
    summary="Get formatted job logs for UI",
    description="Returns processed HTML lines with timestamps and section markers.",
)
async def get_formatted_job_trace(
    request: Request,
    job_id: int,
    byte_offset: int = Query(
        0,
        ge=0,
        description="Byte offset of the last seen log byte; only newer bytes are returned",
    ),
    line_num: int = Query(
        1, ge=1, description="Line number to assign to the first returned line"
    ),
    project_id: int | None = Query(None, description="GitLab project ID"),
    current_user: User = Depends(get_current_user_cookie_or_header(["deploy_api"])),
    trace_cache: TraceCacheRepository = Depends(get_trace_cache_repository),
) -> ApiResponse[FormattedLogResponse]:
    svc = _get_deploy_service(project_id, trace_cache=trace_cache)
    data = await svc.get_formatted_job_trace(job_id, byte_offset, line_num)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/jobs/{job_id}/view",
    response_class=HTMLResponse,
    summary="View job logs in UI",
    description="Opens a beautiful, auto-refreshing log viewer for the specific job.",
)
async def view_job(
    job_id: int,
    project_id: int | None = Query(None, description="GitLab project ID"),
):
    """Returns a styled HTML log viewer."""
    settings = get_settings()
    target_project_id = project_id or settings.GITLAB_PROJECT_ID

    # Resolve the authoritative job URL from GitLab so the viewer doesn't
    # have to guess the namespace path. Fall back to the GitLab root if
    # the lookup fails — a stale link beats blocking the page from rendering.
    svc = _get_deploy_service(project_id)
    gitlab_root = settings.GITLAB_URL.rstrip("/")
    try:
        job_web_url = await svc.get_job_web_url(job_id)
    # The web_url is decorative; failing to resolve it must not break the viewer.
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _logger.warning(
            "Could not resolve job web_url for viewer | job=%s | %s",
            job_id,
            exc,
        )
        job_web_url = gitlab_root

    trace_url = f"/api/v1/deploy/jobs/{job_id}/trace/ui?project_id={target_project_id}"
    meta_html = (
        f'<div><span class="label">Project ID</span><code>{target_project_id}</code></div>'
        f'<div><span class="label">Job ID</span><code>{job_id}</code></div>'
        f'<div><a class="error-link" href="{job_web_url}" target="_blank" rel="noopener">Open in GitLab</a></div>'
    )
    return LOG_VIEWER_HTML.format(
        title=f"Job Log Viewer | {job_id}",
        heading=f"Job: {job_id}",
        trace_url=trace_url,
        terminal_statuses_json="['success','failed','canceled','skipped','manual']",
        meta_html=meta_html,
    )
