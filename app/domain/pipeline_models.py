"""
app/domain/pipeline_models.py

Pydantic models specific to the GitLab pipeline (deploy) domain.

Kept separate from models.py to avoid bloating the auth models file.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
import json

from pydantic import BaseModel, Field, field_validator

# ──────────────────────────────────────────────────────────────────────────────
# Request models
# ──────────────────────────────────────────────────────────────────────────────


class PipelineVariable(BaseModel):
    """A single key-value variable passed to a GitLab pipeline."""

    key: str
    value: Any

    @field_validator("value", mode="before")
    @classmethod
    def stringify_complex_types(cls, v: Any) -> str:
        """Convert objects/lists into JSON strings because GitLab expects strings."""
        if isinstance(v, (dict, list)):
            return json.dumps(v, separators=(",", ":"))
        return str(v)


class TriggerPipelineRequest(BaseModel):
    """Body for POST /api/v1/deploy/stage."""

    variables: list[PipelineVariable] = Field(
        default_factory=list,
        description="Additional variables to inject into the pipeline.",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Data payloads (returned inside ApiResponse[T])
# ──────────────────────────────────────────────────────────────────────────────


class JobData(BaseModel):
    """Job summary returned inside PipelineData."""

    id: int
    name: str
    status: str


class DownstreamPipelineRef(BaseModel):
    """A downstream pipeline triggered by a bridge job in the parent pipeline."""

    id: int
    status: str
    project_id: int
    web_url: str = ""
    bridge_name: str = ""


class PipelineData(BaseModel):
    """Pipeline summary returned by all deploy endpoints."""

    id: int
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    tag_list: list[str] = Field(
        default_factory=list,
        description="Unique runner tags across all pipeline jobs.",
    )
    variables: list[PipelineVariable] = Field(
        default_factory=list,
        description="All variables the pipeline was triggered with.",
    )
    jobs: list[JobData] = Field(
        default_factory=list,
        description="All jobs associated with this pipeline.",
    )
    downstream_pipelines: list[DownstreamPipelineRef] = Field(
        default_factory=list,
        description="Downstream pipelines triggered by bridge jobs in this pipeline.",
    )
    ref_name: str = ""
    web_url: str = ""


class CancelRetryData(BaseModel):
    """Minimal acknowledgement returned by cancel / retry."""

    pipeline_id: int
    status: str
    message: str


class RunningPipelinesData(BaseModel):
    """Response for POST /api/v1/deploy/stage/check-running.

    Returns all active pipelines whose ref AND variables match the query.
    ``has_running`` is a convenience flag so callers don't have to inspect
    the list to know if a conflict exists.
    """

    has_running: bool
    count: int
    pipelines: list[PipelineData]


class FormattedLogLine(BaseModel):
    """A single processed log line with metadata."""

    num: int
    content_html: str


class FormattedLogResponse(BaseModel):
    """Incremental slice of processed log lines for UI rendering.

    The UI polls with ``byte_offset`` and ``next_line_num``, and appends
    only the returned ``lines``. ``next_byte_offset`` and ``next_line_num``
    are echoed back on the following request so the server never has to
    re-fetch or re-render bytes the client already has.

    ``size_warning`` flips on when the trace crosses the soft cap; the
    viewer shows a banner but keeps polling. ``too_large`` flips on at
    the hard cap; the viewer must stop polling and direct the user to
    GitLab. ``total_size`` is the current trace size in bytes (used by
    the viewer to display the size in the warning / error UI).
    """

    job_id: int
    status: str
    next_byte_offset: int
    next_line_num: int
    lines: list[FormattedLogLine]
    total_size: int = 0
    size_warning: bool = False
    too_large: bool = False
