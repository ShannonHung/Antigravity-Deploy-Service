"""
app/services/deploy_service.py

Business logic for pipeline deployment operations.

Depends on PipelineRepository (abstract) — never imports GitLab SDK directly.
"""

from __future__ import annotations

import logging

from app.core.config import get_settings
from app.core.exceptions import ConflictException
from app.domain.pipeline_models import (
    PipelineVariable,
    RunningPipelinesData,
    FormattedLogResponse,
)
from app.core.log_renderer import LogRenderer
from app.repositories.pipeline_repository import PipelineRepository

_logger = logging.getLogger(__name__)


class DeployService:
    """Orchestrates pipeline trigger, status, cancel, and retry operations."""

    def __init__(self, pipeline_repo: PipelineRepository) -> None:
        self._repo = pipeline_repo

    # ── private helpers ───────────────────────────────────────────────────────

    def _build_variables(
        self, action: str, extra_variables: list[PipelineVariable]
    ) -> dict[str, str]:
        """Merge EXECUTION + caller-supplied variables into a flat dict.

        EXECUTION is always set from *action*; extra variables are added
        afterwards so they cannot accidentally override EXECUTION.
        """
        variables: dict[str, str] = {"EXECUTION": action}
        for var in extra_variables:
            variables[var.key] = var.value
        return variables

    # Variables that identify the caller but are irrelevant to duplicate detection.
    _EXCLUDE_FROM_MATCH: frozenset[str] = frozenset({"SERVICE_FROM"})

    def _variables_match(
        self, pipeline: PipelineData, expected: dict[str, str]
    ) -> bool:
        """Return True if a pipeline was triggered with the same business variables.

        SERVICE_FROM (and any other caller-identity keys) are excluded so that
        a pipeline started by user A is correctly detected as a duplicate by user B.
        """
        exclude = self._EXCLUDE_FROM_MATCH
        actual = {v.key: v.value for v in pipeline.variables if v.key not in exclude}
        filtered_expected = {k: v for k, v in expected.items() if k not in exclude}
        return actual == filtered_expected

    # ── public API ────────────────────────────────────────────────────────────

    async def find_duplicate_pipelines(
        self,
        action: str,
        ref: str,
        extra_variables: list[PipelineVariable],
    ) -> RunningPipelinesData:
        """Return all active pipelines on *ref* that share the same variables.

        Matches on the full variable set (EXECUTION + extras) so pipelines
        triggered with different parameters on the same branch are NOT blocked.
        """
        target_vars = self._build_variables(action, extra_variables)
        running = await self._repo.list_running(ref=ref)

        duplicates = [p for p in running if self._variables_match(p, target_vars)]

        _logger.info(
            "Duplicate check | ref=%s | target_vars=%s | active=%d | matches=%d",
            ref,
            target_vars,
            len(running),
            len(duplicates),
        )
        return RunningPipelinesData(
            has_running=bool(duplicates),
            count=len(duplicates),
            pipelines=duplicates,
        )

    async def trigger_pipeline(
        self,
        action: str,
        ref: str,
        extra_variables: list[PipelineVariable],
    ) -> PipelineData:
        """Trigger a new pipeline after verifying no duplicate is already running.

        Raises:
            ConflictException: A running pipeline with identical parameters exists.
        """
        # ── Duplicate guard ───────────────────────────────────────────────────
        result = await self.find_duplicate_pipelines(action, ref, extra_variables)
        if result.has_running:
            existing = result.pipelines[0]
            raise ConflictException(
                f"A pipeline with identical parameters is already running "
                f"(id={existing.id}, status={existing.status}).",
                detail={
                    "pipeline_id": existing.id,
                    "status": existing.status,
                    "web_url": existing.web_url,
                },
            )

        # ── Trigger ───────────────────────────────────────────────────────────
        variables = self._build_variables(action, extra_variables)
        _logger.info(
            "Triggering pipeline | action=%s | ref=%s | variables=%s",
            action,
            ref,
            variables,
        )
        return await self._repo.trigger(ref=ref, variables=variables)

    async def get_pipeline(self, pipeline_id: int) -> PipelineData:
        """Return the current state of a pipeline."""
        return await self._repo.get(pipeline_id)

    async def cancel_pipeline(self, pipeline_id: int) -> PipelineData:
        """Cancel a running pipeline."""
        _logger.info("Cancelling pipeline | id=%s", pipeline_id)
        return await self._repo.cancel(pipeline_id)

    async def retry_pipeline(self, pipeline_id: int) -> PipelineData:
        """Retry a failed or cancelled pipeline."""
        _logger.info("Retrying pipeline | id=%s", pipeline_id)
        return await self._repo.retry(pipeline_id)

    async def get_job_web_url(self, job_id: int) -> str:
        """Return the GitLab UI URL for *job_id*."""
        return await self._repo.get_job_web_url(job_id)

    async def get_formatted_job_trace(
        self,
        job_id: int,
        byte_offset: int = 0,
        line_num: int = 1,
    ) -> FormattedLogResponse:
        """Return only the log lines newer than *byte_offset*.

        The server fetches just the byte range starting at *byte_offset* via
        the repository's HTTP Range request, so each poll transfers only the
        new tail of the log rather than the entire trace. A trailing
        partial line (no terminating newline) is held back and re-fetched
        on the next poll so lines never render half-written.

        Two size caps protect the service and the viewer from runaway logs:
        ``GITLAB_TRACE_SOFT_CAP_BYTES`` flips ``size_warning`` so the UI
        shows a "view in GitLab" banner; ``GITLAB_TRACE_HARD_CAP_BYTES``
        flips ``too_large`` so the UI stops polling entirely. Once the
        hard cap is hit the response carries no new lines — whatever the
        client already rendered stays visible.
        """
        settings = get_settings()
        status, new_text, total_size = await self._repo.get_job_trace_range(
            job_id, byte_offset
        )

        # ── Hard cap: stop serving lines so the viewer can hand off to
        # GitLab. We still pay the cost of *this* fetch (GitLab gives no
        # way to size-check without downloading) but the viewer will stop
        # polling once it sees ``too_large``, so subsequent fetches don't
        # happen.
        if total_size > settings.GITLAB_TRACE_HARD_CAP_BYTES:
            _logger.warning(
                "Job trace exceeded hard cap | job=%s | size=%d | cap=%d",
                job_id,
                total_size,
                settings.GITLAB_TRACE_HARD_CAP_BYTES,
            )
            return FormattedLogResponse(
                job_id=job_id,
                status=status,
                next_byte_offset=byte_offset,
                next_line_num=line_num,
                lines=[],
                total_size=total_size,
                too_large=True,
            )

        size_warning = total_size > settings.GITLAB_TRACE_SOFT_CAP_BYTES

        # Snap to the last newline so we never render a half-flushed line.
        # The trimmed bytes get re-fetched on the next poll.
        next_byte_offset = total_size
        if new_text and not new_text.endswith("\n"):
            last_nl = new_text.rfind("\n")
            if last_nl == -1:
                # No complete line in this slice yet — wait for more bytes.
                return FormattedLogResponse(
                    job_id=job_id,
                    status=status,
                    next_byte_offset=byte_offset,
                    next_line_num=line_num,
                    lines=[],
                    total_size=total_size,
                    size_warning=size_warning,
                )
            held_back = len(new_text) - (last_nl + 1)
            new_text = new_text[: last_nl + 1]
            next_byte_offset = total_size - held_back

        if not new_text:
            return FormattedLogResponse(
                job_id=job_id,
                status=status,
                next_byte_offset=next_byte_offset,
                next_line_num=line_num,
                lines=[],
                total_size=total_size,
                size_warning=size_warning,
            )

        renderer = LogRenderer()
        lines = renderer.render(job_id, new_text, start_line_num=line_num)

        return FormattedLogResponse(
            job_id=job_id,
            status=status,
            next_byte_offset=next_byte_offset,
            next_line_num=line_num + len(lines),
            lines=lines,
            total_size=total_size,
            size_warning=size_warning,
        )
