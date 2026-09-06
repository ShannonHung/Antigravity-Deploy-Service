"""
app/api/v1/auth.py

Auth endpoints (v1).

All successful responses use ApiResponse[T] envelope:
    {"data": <T>, "request_id": "..."}
Error responses (handled globally):
    {"error": {"code": "...", "message": "..."}, "request_id": "..."}
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app.core.dependencies import get_current_user
from app.domain.models import (
    ApiResponse,
    HashPasswordRequest,
    HashPasswordData,
    MyScopesData,
    VerifyData,
    User,
)
from app.services.auth_service import AuthService
from app.repositories.json_user_repository import JsonUserRepository
from app.core.config import get_settings

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _get_auth_service() -> AuthService:
    """Build AuthService with the configured repository."""
    settings = get_settings()
    repo = JsonUserRepository(settings.USERS_JSON_PATH)
    return AuthService(repo)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


# ── GET /api/v1/auth/verify ───────────────────────────────────────────────────


@router.get(
    "/verify",
    response_model=ApiResponse[VerifyData],
    summary="Verify token validity",
    description="Returns the account name and scopes embedded in the provided JWT.",
)
async def verify(
    request: Request,
    current_user: User = Depends(get_current_user()),
    svc: AuthService = Depends(_get_auth_service),
) -> ApiResponse[VerifyData]:
    verify_data = await svc.verify_user(current_user)
    return ApiResponse(data=verify_data, request_id=_request_id(request))


# ── POST /api/v1/auth/hash-password ──────────────────────────────────────────


@router.post(
    "/hash-password",
    response_model=ApiResponse[HashPasswordData],
    summary="Hash a plain-text password",
    description=(
        "Returns a bcrypt hash of the provided password. "
        "Paste the result into ``data/users.json`` to change a password. "
        "No authentication required."
    ),
)
async def hash_password_endpoint(
    request: Request,
    body: HashPasswordRequest,
    svc: AuthService = Depends(_get_auth_service),
) -> ApiResponse[HashPasswordData]:
    hash_data = await svc.hash_plain_password(body.password)
    return ApiResponse(data=hash_data, request_id=_request_id(request))


# ── GET /api/v1/auth/my-scopes ───────────────────────────────────────────────


@router.get(
    "/my-scopes",
    response_model=ApiResponse[MyScopesData],
    summary="Inspect current token scopes",
    description="Returns the scopes granted to the authenticated caller's token.",
)
async def my_scopes(
    request: Request,
    current_user: User = Depends(get_current_user()),
    svc: AuthService = Depends(_get_auth_service),
) -> ApiResponse[MyScopesData]:
    scopes_data = await svc.get_my_scopes(current_user)
    return ApiResponse(data=scopes_data, request_id=_request_id(request))
