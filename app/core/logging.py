"""
app/core/logging.py

Request-ID middleware and logging configuration.

X-Coordination-ID behaviour:
  - If the incoming request has an X-Coordination-ID header, that value is used
    as the request_id and echoed back in X-Coordination-ID response header.
  - If the header is absent a UUID4 is generated internally for logging only;
    it is NOT added to the response headers (the caller didn't ask for one).
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

COORDINATION_ID_HEADER = "X-Coordination-ID"


# ──────────────────────────────────────────────────────────────────────────────
# Log filter — injects request_id into every log record
# ──────────────────────────────────────────────────────────────────────────────

_request_id_var: ContextVar[str] = ContextVar("request_id", default="N/A")
_account_var: ContextVar[str] = ContextVar("account", default="-")


class RequestIdFilter(logging.Filter):
    """Logging filter that adds ``request_id`` and ``username`` to every log record.

    Values are stored in ContextVars so each asyncio coroutine (request) has
    its own isolated copy — concurrent requests never bleed into each other's
    log lines.

    ``set_account`` is called by ``get_current_user`` after JWT validation so
    that all log lines emitted during an authenticated request automatically
    carry the caller's identity without each logger having to pass it manually.
    """

    @staticmethod
    def set_request_id(value: str) -> None:
        _request_id_var.set(value)

    @staticmethod
    def set_account(value: str) -> None:
        _account_var.set(value)

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()  # type: ignore[attr-defined]
        # ``username`` may be overridden per-call via extra={"username": ...};
        # fall back to the request-scoped account set by the auth dependency.
        if not hasattr(record, "username"):
            record.username = _account_var.get()  # type: ignore[attr-defined]
        if not hasattr(record, "command_id"):
            record.command_id = "-"  # type: ignore[attr-defined]
        if not hasattr(record, "host"):
            record.host = "-"  # type: ignore[attr-defined]
        if not hasattr(record, "port"):
            record.port = "-"  # type: ignore[attr-defined]
        return True


class HealthCheckFilter(logging.Filter):
    """Filter out /health and /metrics access logs to reduce noise."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "/health" not in msg and "/metrics" not in msg


_request_id_filter = RequestIdFilter()
_health_filter = HealthCheckFilter()


# ──────────────────────────────────────────────────────────────────────────────
# Middleware
# ──────────────────────────────────────────────────────────────────────────────


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assign a request_id to every incoming request.

    Priority:
      1. X-Coordination-ID request header  → use as-is, echo in response header
      2. No header present                  → generate UUID4 for internal logging,
                                              do NOT add to response headers
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        coordination_id = request.headers.get(COORDINATION_ID_HEADER)
        from_client = coordination_id is not None
        request_id = coordination_id or str(uuid.uuid4())

        request.state.request_id = request_id
        RequestIdFilter.set_request_id(request_id)
        RequestIdFilter.set_account("-")

        response = await call_next(request)

        if from_client:
            # Echo back only when the client explicitly provided the header
            response.headers[COORDINATION_ID_HEADER] = request_id

        return response


# ──────────────────────────────────────────────────────────────────────────────
# Setup helper
# ──────────────────────────────────────────────────────────────────────────────


def setup_logging(log_level: str = "INFO") -> None:
    """Configure root logger with request_id in the format string."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    handler = logging.StreamHandler()
    handler.addFilter(_request_id_filter)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | req=%(request_id)s | "
            "user=%(username)s | cmd=%(command_id)s | "
            "target=%(host)s:%(port)s | "
            "%(name)s:%(lineno)d | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    # ── Uvicorn specific ───────────────────────────────────────────────────────
    # Filter out /health from access logs
    logging.getLogger("uvicorn.access").addFilter(_health_filter)
