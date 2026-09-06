"""
app/core/exceptions.py

Custom application exception hierarchy.

Design principles:
  - Every exception carries: error_code, http_status, log_level, source_function
  - A global handler in main.py catches BaseAppException and returns:
      {"error": {"code": "...", "message": "..."}, "request_id": "..."}
  - Unhandled exceptions fall through to a catch-all handler that logs
    the full traceback without leaking internals to the client.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

# ──────────────────────────────────────────────────────────────────────────────
# Base
# ──────────────────────────────────────────────────────────────────────────────


class BaseAppException(Exception):
    """Base class for all application-specific exceptions."""

    http_status: int = 500
    error_code: str = "INTERNAL_ERROR"
    log_level: int = logging.ERROR

    def __init__(
        self,
        message: str,
        *,
        source_function: str = "",
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

        # Auto-detect caller's qualified name when not provided.
        # co_qualname (Python 3.11+) returns e.g. "GitlabPipelineRepository.trigger"
        # so renaming the function or class is reflected here automatically.
        if source_function:
            self.source_function = source_function
        else:
            frame = inspect.currentframe()
            caller = frame.f_back if frame is not None else None
            if caller is not None and "self" in caller.f_locals:
                src_func = (
                    caller.f_locals["self"].__class__.__name__
                    + "."
                    + caller.f_code.co_name
                )
            elif caller is not None:
                src_func = caller.f_code.co_filename + ":" + str(caller.f_lineno)
            else:
                src_func = "unknown"
            self.source_function = src_func

    def log(self, logger: logging.Logger) -> None:
        """Emit a structured log entry at the appropriate level."""
        logger.log(
            self.log_level,
            "[%s] %s | source=%s | detail=%s",
            self.error_code,
            self.message,
            self.source_function or "unknown",
            self.detail,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Concrete exceptions
# ──────────────────────────────────────────────────────────────────────────────


class AuthException(BaseAppException):
    """Raised when authentication fails (invalid credentials / bad token)."""

    http_status = 401
    error_code = "AUTH_ERROR"
    log_level = logging.WARNING


class ForbiddenException(BaseAppException):
    """Raised when a token lacks the required scopes."""

    http_status = 403
    error_code = "FORBIDDEN"
    log_level = logging.WARNING


class NotFoundException(BaseAppException):
    """Raised when a requested resource cannot be found."""

    http_status = 404
    error_code = "NOT_FOUND"
    log_level = logging.INFO


class ValidationException(BaseAppException):
    """Raised for business-logic validation failures."""

    http_status = 422
    error_code = "VALIDATION_ERROR"
    log_level = logging.WARNING


class GitlabOperationException(BaseAppException):
    """Raised when a GitLab API call returns an error.

    Wraps ``gitlab.exceptions.GitlabError`` so callers outside the
    infrastructure layer never need to import python-gitlab directly.
    """

    http_status = 502
    error_code = "GITLAB_ERROR"
    log_level = logging.ERROR


class UpstreamTimeoutException(BaseAppException):
    """Raised when an upstream call (GitLab trace fetch, SSH connect, etc.)
    exceeds its configured timeout. Surfaced as 504 so clients still receive
    a structured JSON error instead of a bare proxy timeout.
    """

    http_status = 504
    error_code = "UPSTREAM_TIMEOUT"
    log_level = logging.WARNING


class UpstreamUnavailableException(BaseAppException):
    """Raised when an upstream system (e.g. SSH target host) is reachable
    on the network layer but cannot complete the request — DNS failure,
    connection refused, host key mismatch, authentication failure, etc.
    Surfaced as 502 because the gateway/upstream is the failing party,
    not the client.
    """

    http_status = 502
    error_code = "UPSTREAM_UNAVAILABLE"
    log_level = logging.WARNING


class ConflictException(BaseAppException):
    """Raised when the requested action conflicts with the current state.

    Example: trying to trigger a pipeline that is already running with
    identical parameters.
    """

    http_status = 409
    error_code = "CONFLICT"
    log_level = logging.WARNING


class ServiceUnavailableException(BaseAppException):
    """Raised when the service temporarily cannot accept the request
    (e.g. running-command pool is full)."""

    http_status = 503
    error_code = "SERVICE_UNAVAILABLE"
    log_level = logging.WARNING


class CommandExecutionException(BaseAppException):
    """Raised for validation or system setup failures before or during execution."""

    http_status = 400
    error_code = "COMMAND_EXECUTION_ERROR"
    log_level = logging.WARNING


class WhitelistConfigurationException(BaseAppException):
    """Raised when a user's allow-commands whitelist file is malformed.

    Surfaced as 500: the request itself is well-formed, but the operator-owned
    config on disk (e.g. an invalid ``validation_regex`` or host pattern) cannot
    be parsed. Distinct from ``COMMAND_EXECUTION_ERROR`` so callers can tell
    "you sent something bad" apart from "the server is misconfigured".
    """

    http_status = 500
    error_code = "WHITELIST_CONFIG_ERROR"
    log_level = logging.ERROR


class ScriptVersionException(BaseAppException):
    """Raised when a target script's version is below the required minimum.

    Surfaced as 412 Precondition Failed: the request is well-formed but the
    control_node script does not meet the version precondition, so the pipeline
    is not run.
    """

    http_status = 412
    error_code = "SCRIPT_VERSION_MISMATCH"
    log_level = logging.WARNING


# ──────────────────────────────────────────────────────────────────────────────
# Global exception handlers
# ──────────────────────────────────────────────────────────────────────────────

_logger = logging.getLogger(__name__)


def _error_body(code: str, message: str, request_id: str, detail: Any = None) -> dict:
    """Build the standard error response body.

    Shape:
        {"error": {"code": "...", "message": "...", "detail": ...}, "request_id": "..."}
    """
    error: dict = {"code": code, "message": message}
    if detail is not None:
        error["detail"] = detail
    return {"error": error, "request_id": request_id}


async def app_exception_handler(
    request: Request, exc: BaseAppException
) -> JSONResponse:
    """Handle all BaseAppException subclasses with a unified JSON error response."""
    request_id: str = getattr(request.state, "request_id", "")
    exc.log(_logger)

    return JSONResponse(
        status_code=exc.http_status,
        content=_error_body(
            code=exc.error_code,
            message=exc.message,
            request_id=request_id,
            detail=exc.detail,
        ),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler for unexpected exceptions.

    Logs the full traceback but returns a generic message to the client
    to avoid leaking internal implementation details.
    """
    request_id: str = getattr(request.state, "request_id", "")
    _logger.exception(
        "Unhandled exception | request_id=%s | path=%s",
        request_id,
        request.url.path,
    )

    return JSONResponse(
        status_code=500,
        content=_error_body(
            code="INTERNAL_SERVER_ERROR",
            message="An unexpected error occurred. Please try again later.",
            request_id=request_id,
        ),
    )
