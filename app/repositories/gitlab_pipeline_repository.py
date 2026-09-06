"""
app/repositories/gitlab_pipeline_repository.py

GitLab-backed implementation of PipelineRepository using python-gitlab.

Error handling strategy:
  - All ``gitlab.exceptions.GitlabError`` subclasses are caught here and
    re-raised as ``GitlabOperationException`` so the service / router layer
    never needs to import the GitLab SDK.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import gitlab
import gitlab.exceptions
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.exceptions import (
    GitlabOperationException,
    NotFoundException,
    UpstreamTimeoutException,
)
from app.domain.pipeline_models import (
    DownstreamPipelineRef,
    JobData,
    PipelineData,
    PipelineVariable,
)
from app.repositories.pipeline_repository import PipelineRepository
from app.repositories.trace_cache_repository import TraceCacheRepository

_logger = logging.getLogger(__name__)

# Pipeline statuses that represent an in-progress (not yet finished) run.
_ACTIVE_STATUSES = [
    "created",
    "waiting_for_resource",
    "preparing",
    "pending",
    "running",
]

# Job statuses for which the trace is guaranteed immutable and safe to cache.
_TERMINAL_JOB_STATUSES = frozenset({"success", "failed", "canceled", "skipped"})


class GitlabPipelineRepository(PipelineRepository):
    """Uses python-gitlab to talk to the GitLab Pipelines API."""

    def __init__(
        self,
        url: str,
        token: str,
        project_id: int,
        trace_cache: TraceCacheRepository | None = None,
        http_timeout: int | None = None,
    ) -> None:
        settings = get_settings()
        timeout = (
            http_timeout
            if http_timeout is not None
            else settings.GITLAB_HTTP_TIMEOUT_SECONDS
        )
        ssl_verify: str | bool = settings.GITLAB_CA if settings.GITLAB_CA else True
        self._gl = gitlab.Gitlab(
            url=url, private_token=token, timeout=timeout, ssl_verify=ssl_verify
        )
        self._project_id = project_id
        self._trace_cache = trace_cache
        self._project_cache: Any = None
        _logger.debug(
            "GitlabPipelineRepository init | url=%s | project=%s | http_timeout=%ss",
            url,
            project_id,
            timeout,
        )

    # ── private helpers ───────────────────────────────────────────────────────

    def _get_project(self) -> Any:
        if self._project_cache is not None:
            return self._project_cache
        _logger.debug("GitLab request | op=projects.get | project=%s", self._project_id)
        t0 = time.monotonic()
        try:
            project = self._gl.projects.get(self._project_id)
            _logger.debug(
                "GitLab response | op=projects.get | project=%s | elapsed=%.2fs",
                self._project_id,
                time.monotonic() - t0,
            )
            self._project_cache = project
            return project
        except gitlab.exceptions.GitlabAuthenticationError as exc:
            _logger.error(
                "GitLab authentication failed | project=%s | elapsed=%.2fs | %s",
                self._project_id,
                time.monotonic() - t0,
                exc,
            )
            raise GitlabOperationException(
                "GitLab authentication failed — GITLAB_TOKEN is invalid or expired.",
                detail=str(exc),
            ) from exc
        except gitlab.exceptions.GitlabGetError as exc:
            _logger.error(
                "Failed to get project | id=%s | elapsed=%.2fs | %s",
                self._project_id,
                time.monotonic() - t0,
                exc,
            )
            raise GitlabOperationException(
                f"GitLab project {self._project_id} not accessible.",
                detail=str(exc),
            ) from exc

    def _collect_jobs_and_tags(
        self, pipeline: Any, pipeline_id: int
    ) -> tuple[list[JobData], list[str]]:
        """Fetch jobs once and derive both JobData list and tag list from it."""
        try:
            t0 = time.monotonic()
            raw_jobs = pipeline.jobs.list()
            _logger.debug(
                "GitLab response | op=jobs.list | project=%s | pipeline_id=%s"
                " | count=%d | elapsed=%.2fs",
                self._project_id,
                pipeline_id,
                len(raw_jobs),
                time.monotonic() - t0,
            )
            tags: set[str] = set()
            jobs: list[JobData] = []
            for job in raw_jobs:
                tags.update(getattr(job, "tag_list", []))
                jobs.append(JobData(id=job.id, name=job.name, status=job.status))
            return jobs, sorted(tags)
        except gitlab.exceptions.GitlabError:
            return [], []

    def _collect_variables(
        self, pipeline: Any, pipeline_id: int
    ) -> list[PipelineVariable]:
        """Return all variables the pipeline was triggered with."""
        try:
            t0 = time.monotonic()
            raw = pipeline.variables.list()
            _logger.debug(
                "GitLab response | op=variables.list | project=%s | pipeline_id=%s"
                " | count=%d | elapsed=%.2fs",
                self._project_id,
                pipeline_id,
                len(raw),
                time.monotonic() - t0,
            )
            return [PipelineVariable(key=v.key, value=v.value) for v in raw]
        except gitlab.exceptions.GitlabError:
            return []

    def _collect_downstream_pipelines(
        self, pipeline: Any, pipeline_id: int
    ) -> list[DownstreamPipelineRef]:
        """Return downstream pipelines triggered by bridge jobs in this pipeline."""
        try:
            t0 = time.monotonic()
            bridges = pipeline.bridges.list(get_all=True)
            _logger.debug(
                "GitLab response | op=bridges.list | project=%s | pipeline_id=%s"
                " | count=%d | elapsed=%.2fs",
                self._project_id,
                pipeline_id,
                len(bridges),
                time.monotonic() - t0,
            )
            result: list[DownstreamPipelineRef] = []
            for bridge in bridges:
                downstream = getattr(bridge, "downstream_pipeline", None)
                if not downstream:
                    continue
                result.append(
                    DownstreamPipelineRef(
                        id=downstream["id"],
                        status=downstream["status"],
                        web_url=downstream.get("web_url", ""),
                        project_id=downstream["project_id"],
                        bridge_name=getattr(bridge, "name", ""),
                    )
                )
            return result
        except (gitlab.exceptions.GitlabError, KeyError, TypeError):
            return []

    def _to_pipeline_data(self, pipeline: Any) -> PipelineData:
        """Map a full pipeline object → PipelineData with all sub-resources.

        Use for GET /stage/{id} where the caller needs jobs, variables, and
        downstream pipelines. For write operations (trigger/cancel/retry) use
        _to_pipeline_data_minimal to avoid unnecessary sub-resource fetches.
        """
        pid: int = pipeline.id
        jobs, tag_list = self._collect_jobs_and_tags(pipeline, pid)
        variables = self._collect_variables(pipeline, pid)
        downstream = self._collect_downstream_pipelines(pipeline, pid)

        return PipelineData(
            id=pid,
            status=pipeline.status,
            created_at=getattr(pipeline, "created_at", None),
            updated_at=getattr(pipeline, "updated_at", None),
            started_at=getattr(pipeline, "started_at", None),
            finished_at=getattr(pipeline, "finished_at", None),
            tag_list=tag_list,
            variables=variables,
            jobs=jobs,
            downstream_pipelines=downstream,
            ref_name=getattr(pipeline, "ref", ""),
            web_url=getattr(pipeline, "web_url", ""),
        )

    def _to_pipeline_data_minimal(self, pipeline: Any) -> PipelineData:
        """Map a pipeline object → PipelineData with pipeline-level fields only.

        Does not fetch jobs, variables, or downstream pipelines. Use for
        trigger/cancel/retry responses where sub-resources are not yet
        meaningful or not needed by the caller.
        """
        return PipelineData(
            id=pipeline.id,
            status=pipeline.status,
            created_at=getattr(pipeline, "created_at", None),
            updated_at=getattr(pipeline, "updated_at", None),
            started_at=getattr(pipeline, "started_at", None),
            finished_at=getattr(pipeline, "finished_at", None),
            tag_list=[],
            variables=[],
            jobs=[],
            downstream_pipelines=[],
            ref_name=getattr(pipeline, "ref", ""),
            web_url=getattr(pipeline, "web_url", ""),
        )

    # ── interface implementation ───────────────────────────────────────────────

    async def trigger(self, ref: str, variables: dict[str, str]) -> PipelineData:
        gl_vars = [{"key": k, "value": v} for k, v in variables.items()]
        _logger.info(
            "GitLab request | op=pipelines.create | project=%s | ref=%s | variables=%s",
            self._project_id,
            ref,
            variables,
        )
        t0 = time.monotonic()

        def _sync() -> PipelineData:
            project = self._get_project()
            try:
                pipeline = project.pipelines.create({"ref": ref, "variables": gl_vars})
                _logger.info(
                    "GitLab response | op=pipelines.create | project=%s | pipeline_id=%s"
                    " | ref=%s | status=%s | elapsed=%.2fs",
                    self._project_id,
                    pipeline.id,
                    ref,
                    getattr(pipeline, "status", "unknown"),
                    time.monotonic() - t0,
                )
                return self._to_pipeline_data_minimal(pipeline)
            except gitlab.exceptions.GitlabCreateError as exc:
                _logger.error(
                    "GitLab error | op=pipelines.create | project=%s | ref=%s"
                    " | elapsed=%.2fs | %s",
                    self._project_id,
                    ref,
                    time.monotonic() - t0,
                    exc,
                )
                raise GitlabOperationException(
                    "Failed to trigger GitLab pipeline.",
                    detail=str(exc),
                ) from exc

        return await run_in_threadpool(_sync)

    async def get(self, pipeline_id: int) -> PipelineData:
        _logger.debug(
            "GitLab request | op=pipelines.get | project=%s | pipeline_id=%s",
            self._project_id,
            pipeline_id,
        )
        t0 = time.monotonic()

        def _sync() -> PipelineData:
            project = self._get_project()
            try:
                pipeline = project.pipelines.get(pipeline_id)
                _logger.debug(
                    "GitLab response | op=pipelines.get | project=%s | pipeline_id=%s"
                    " | status=%s | elapsed=%.2fs",
                    self._project_id,
                    pipeline_id,
                    getattr(pipeline, "status", "unknown"),
                    time.monotonic() - t0,
                )
                return self._to_pipeline_data(pipeline)
            except gitlab.exceptions.GitlabGetError as exc:
                _logger.error(
                    "GitLab error | op=pipelines.get | project=%s | pipeline_id=%s"
                    " | elapsed=%.2fs | %s",
                    self._project_id,
                    pipeline_id,
                    time.monotonic() - t0,
                    exc,
                )
                if "404" in str(exc):
                    raise NotFoundException(
                        f"Pipeline {pipeline_id} not found.",
                    ) from exc
                raise GitlabOperationException(
                    f"Failed to fetch pipeline {pipeline_id}.",
                    detail=str(exc),
                ) from exc

        return await run_in_threadpool(_sync)

    async def cancel(self, pipeline_id: int) -> PipelineData:
        _logger.info(
            "GitLab request | op=pipelines.cancel | project=%s | pipeline_id=%s",
            self._project_id,
            pipeline_id,
        )
        t0 = time.monotonic()

        def _sync() -> PipelineData:
            project = self._get_project()
            try:
                pipeline = project.pipelines.get(pipeline_id)
                pipeline.cancel()
                pipeline = project.pipelines.get(pipeline_id)  # refresh state
                _logger.info(
                    "GitLab response | op=pipelines.cancel | project=%s | pipeline_id=%s"
                    " | status=%s | elapsed=%.2fs",
                    self._project_id,
                    pipeline_id,
                    getattr(pipeline, "status", "unknown"),
                    time.monotonic() - t0,
                )
                return self._to_pipeline_data_minimal(pipeline)
            except gitlab.exceptions.GitlabError as exc:
                _logger.error(
                    "GitLab error | op=pipelines.cancel | project=%s | pipeline_id=%s"
                    " | elapsed=%.2fs | %s",
                    self._project_id,
                    pipeline_id,
                    time.monotonic() - t0,
                    exc,
                )
                raise GitlabOperationException(
                    f"Failed to cancel pipeline {pipeline_id}.",
                    detail=str(exc),
                ) from exc

        return await run_in_threadpool(_sync)

    async def retry(self, pipeline_id: int) -> PipelineData:
        _logger.info(
            "GitLab request | op=pipelines.retry | project=%s | pipeline_id=%s",
            self._project_id,
            pipeline_id,
        )
        t0 = time.monotonic()

        def _sync() -> PipelineData:
            project = self._get_project()
            try:
                pipeline = project.pipelines.get(pipeline_id)
                pipeline.retry()
                pipeline = project.pipelines.get(pipeline_id)  # refresh state
                _logger.info(
                    "GitLab response | op=pipelines.retry | project=%s | pipeline_id=%s"
                    " | status=%s | elapsed=%.2fs",
                    self._project_id,
                    pipeline_id,
                    getattr(pipeline, "status", "unknown"),
                    time.monotonic() - t0,
                )
                return self._to_pipeline_data_minimal(pipeline)
            except gitlab.exceptions.GitlabError as exc:
                _logger.error(
                    "GitLab error | op=pipelines.retry | project=%s | pipeline_id=%s"
                    " | elapsed=%.2fs | %s",
                    self._project_id,
                    pipeline_id,
                    time.monotonic() - t0,
                    exc,
                )
                raise GitlabOperationException(
                    f"Failed to retry pipeline {pipeline_id}.",
                    detail=str(exc),
                ) from exc

        return await run_in_threadpool(_sync)

    async def list_running(self, ref: str) -> list[PipelineData]:
        """Return all active-state pipelines on *ref* with only their variables.

        Variables are the only field needed for duplicate-detection in
        DeployService._variables_match. Jobs, tags, and downstream pipelines
        are intentionally omitted to minimise GitLab round-trips.

        GitLab’s list endpoint only accepts a single status per request, so we
        issue one request per active status and deduplicate by ID. For each
        stub we fetch variables via the /variables sub-resource (one extra
        request per pipeline) — no full pipelines.get() needed.
        """
        _logger.debug(
            "GitLab request | op=pipelines.list_running | project=%s | ref=%s"
            " | statuses=%s",
            self._project_id,
            ref,
            _ACTIVE_STATUSES,
        )
        t0 = time.monotonic()

        def _sync() -> list[PipelineData]:
            project = self._get_project()
            seen_ids: set[int] = set()
            result: list[PipelineData] = []
            try:
                for status in _ACTIVE_STATUSES:
                    page = project.pipelines.list(
                        status=status,
                        ref=ref,
                        get_all=False,
                        per_page=50,
                    )
                    for p in page:
                        if p.id in seen_ids:
                            continue
                        seen_ids.add(p.id)
                        t_vars = time.monotonic()
                        try:
                            raw_vars = project.pipelines.get(p.id).variables.list()
                            variables = [
                                PipelineVariable(key=v.key, value=v.value)
                                for v in raw_vars
                            ]
                        except gitlab.exceptions.GitlabError:
                            variables = []
                        _logger.debug(
                            "GitLab response | op=variables.list (list_running) | project=%s"
                            " | pipeline_id=%s | count=%d | elapsed=%.2fs",
                            self._project_id,
                            p.id,
                            len(variables),
                            time.monotonic() - t_vars,
                        )
                        result.append(
                            PipelineData(
                                id=p.id,
                                status=p.status,
                                created_at=getattr(p, "created_at", None),
                                updated_at=getattr(p, "updated_at", None),
                                started_at=getattr(p, "started_at", None),
                                finished_at=getattr(p, "finished_at", None),
                                tag_list=[],
                                variables=variables,
                                jobs=[],
                                downstream_pipelines=[],
                                ref_name=getattr(p, "ref", ""),
                                web_url=getattr(p, "web_url", ""),
                            )
                        )
            except gitlab.exceptions.GitlabError as exc:
                _logger.error(
                    "GitLab error | op=pipelines.list_running | project=%s | ref=%s"
                    " | elapsed=%.2fs | %s",
                    self._project_id,
                    ref,
                    time.monotonic() - t0,
                    exc,
                )
                raise GitlabOperationException(
                    f"Failed to list running pipelines on ref=’{ref}’.",
                    detail=str(exc),
                ) from exc

            _logger.debug(
                "GitLab response | op=pipelines.list_running | project=%s | ref=%s"
                " | found=%d | elapsed=%.2fs",
                self._project_id,
                ref,
                len(result),
                time.monotonic() - t0,
            )
            return result

        return await run_in_threadpool(_sync)

    async def get_job_web_url(self, job_id: int) -> str:
        def _fetch() -> str:
            project = self._get_project()
            return project.jobs.get(job_id).web_url

        try:
            return await run_in_threadpool(_fetch)
        except gitlab.exceptions.GitlabGetError as exc:
            if "404" in str(exc):
                raise NotFoundException(f"Job {job_id} not found.") from exc
            raise GitlabOperationException(
                f"Failed to fetch web URL for job {job_id}.",
                detail=str(exc),
            ) from exc
        except gitlab.exceptions.GitlabError as exc:
            raise GitlabOperationException(
                f"Failed to fetch web URL for job {job_id}.",
                detail=str(exc),
            ) from exc

    async def get_job_trace_range(
        self, job_id: int, byte_offset: int
    ) -> tuple[str, str, int]:
        """Return ``(status, new_text, total_size)`` for the trace tail.

        GitLab's trace endpoint does not honor HTTP Range headers — it always
        returns the full body. We therefore enforce *byte_offset* server-side
        by slicing locally. To avoid re-downloading the full trace on every
        poll once a job has finished, terminal-status traces are cached in
        Redis (gzip-compressed). Subsequent polls of a finished job are
        served entirely from cache with zero GitLab requests.
        """
        settings = get_settings()

        # ── Fast path: finished-job trace served from cache ───────────────
        if self._trace_cache is not None:
            cached = await self._trace_cache.get(self._project_id, job_id)
            if cached is not None:
                cached_status, cached_bytes = cached
                tail = cached_bytes[byte_offset:] if byte_offset else cached_bytes
                return (
                    cached_status,
                    tail.decode("utf-8", errors="replace"),
                    len(cached_bytes),
                )

        def _fetch() -> tuple[str, bytes, bytes]:
            """Return ``(status, full_trace_bytes, tail_bytes)``."""
            project = self._get_project()
            job = project.jobs.get(job_id)
            path = f"{job.manager.path}/{job.encoded_id}/trace"
            # GitLab ignores Range headers on this endpoint; we always get
            # the full body back and slice locally below.
            resp = self._gl.http_get(path, raw=True, streamed=False)
            full = resp.content
            tail = full[byte_offset:] if byte_offset else full
            return job.status, full, tail

        try:
            status, full, tail = await asyncio.wait_for(
                run_in_threadpool(_fetch),
                timeout=settings.GITLAB_TRACE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise UpstreamTimeoutException(
                f"GitLab trace fetch for job {job_id} exceeded "
                f"{settings.GITLAB_TRACE_TIMEOUT_SECONDS}s.",
            ) from exc
        except gitlab.exceptions.GitlabGetError as exc:
            if "404" in str(exc):
                raise NotFoundException(f"Job {job_id} not found.") from exc
            raise GitlabOperationException(
                f"Failed to fetch trace range for job {job_id}.",
                detail=str(exc),
            ) from exc
        except gitlab.exceptions.GitlabError as exc:
            raise GitlabOperationException(
                f"Failed to fetch trace range for job {job_id}.",
                detail=str(exc),
            ) from exc

        # Write cache once the job is terminal. The trace is immutable from
        # this point on, so future polls bypass GitLab entirely.
        if self._trace_cache is not None and status in _TERMINAL_JOB_STATUSES and full:
            try:
                await self._trace_cache.set(
                    self._project_id,
                    job_id,
                    status,
                    full,
                    settings.GITLAB_TRACE_CACHE_TTL_SECONDS,
                )
            # Best-effort enrichment; any SDK error must degrade rather than fail the request.
            except Exception as exc:  # pylint: disable=broad-exception-caught
                _logger.warning(
                    "Trace cache write failed | project=%s job=%s | %s",
                    self._project_id,
                    job_id,
                    exc,
                )

        return status, tail.decode("utf-8", errors="replace"), len(full)
