"""
app/core/dependencies.py

FastAPI dependency factory for scope-based access control.

Usage in routes:
    @router.get("/deploy")
    def deploy(user: User = Depends(get_current_user(["deploy_api"]))):
        ...
"""

from __future__ import annotations

from typing import Callable, Optional

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer

from app.core.exceptions import AuthException, ForbiddenException
from app.core.logging import RequestIdFilter
from app.core.security import decode_access_token
from app.domain.models import User

# The tokenUrl must match your actual POST /token route path.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")

# Cookie name set by POST /token so a browser opening an HTML viewer can
# authenticate its same-origin fetch() calls (which cannot carry a Bearer
# header). Kept in one place so /token and the cookie-aware dependency agree.
ACCESS_TOKEN_COOKIE = "access_token"


def set_access_cookie(response, token: str, max_age: int) -> None:
    """Set the access_token cookie used by HTML viewers. One definition shared
    by POST /token and POST /login so their cookie attributes stay identical."""
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=False,  # served over http in dev; set True behind TLS in prod
    )


def safe_next_path(next_path: str | None, fallback: str = "/docs") -> str:
    """Open-redirect guard for the login ``next`` param.

    Only same-origin, absolute *paths* are allowed (must start with a single
    '/'). Anything that could redirect off-site ('//host', 'http://host',
    backslashes, missing leading slash) falls back to a safe local default.
    """
    if not next_path:
        return fallback
    if not next_path.startswith("/"):
        return fallback
    if next_path.startswith("//") or next_path.startswith("/\\"):
        return fallback
    return next_path


def _validate_token(token: str, required: list[str]) -> User:
    """Decode a JWT and enforce scopes. Shared by the header-only and the
    cookie-or-header dependencies so both apply identical rules."""
    payload = decode_access_token(token)  # raises AuthException on failure

    account: str | None = payload.get("sub")
    scopes: list[str] = payload.get("scopes", [])

    if not account:
        raise AuthException("Token is missing subject claim.")

    missing = [s for s in required if s not in scopes]
    if missing:
        raise ForbiddenException(
            f"Token is missing required scopes: {missing}",
            detail={"required": required, "missing": missing},
        )

    RequestIdFilter.set_account(account)
    return User(account=account, scopes=scopes)


def get_current_user(required_scopes: list[str] | None = None) -> Callable:
    """Dependency factory that validates JWT and enforces scope requirements.

    Reads the token from the ``Authorization: Bearer`` header only.

    Args:
        required_scopes: List of scope strings the token must contain ALL of.
                         Pass an empty list or None to only validate the token.

    Returns:
        A FastAPI dependency callable that resolves to the authenticated ``User``.

    Raises:
        AuthException:   Token is missing, invalid, or expired.
        ForbiddenException: Token is valid but lacks a required scope.
    """
    required: list[str] = required_scopes or []

    async def _dependency(token: str = Depends(oauth2_scheme)) -> User:
        return _validate_token(token, required)

    return _dependency


def get_current_user_cookie_or_header(required_scopes: list[str] | None = None) -> Callable:
    """Like ``get_current_user`` but also accepts the JWT from the
    ``access_token`` cookie set by POST /token.

    This is what HTML log viewers poll: the browser opens an unauthed ``/view``
    page whose JS ``fetch()`` cannot attach a Bearer header, but DOES send the
    same-origin cookie automatically. Swagger/API callers keep using the
    header. The header wins if both are present.

    Raises:
        AuthException:   No token in header or cookie, or token invalid/expired.
        ForbiddenException: Token valid but lacks a required scope.
    """
    required: list[str] = required_scopes or []

    async def _dependency(
        request: Request,
        header_token: Optional[str] = Depends(OAuth2PasswordBearer(tokenUrl="/token", auto_error=False)),
    ) -> User:
        token = header_token or request.cookies.get(ACCESS_TOKEN_COOKIE)
        if not token:
            raise AuthException("Not authenticated.")
        return _validate_token(token, required)

    return _dependency

from app.clients.inventory_client import InventoryClient, InventoryTokenManager
from app.core.redis_client import RedisClient
from app.repositories.command_state_repository import CommandStateRepository
from app.repositories.inventory_repository import InventoryRepository
from app.repositories.trace_cache_repository import (
    RedisTraceCache,
    TraceCacheRepository,
)
from app.services.command_service import CommandService
from app.services.inventory_service import InventoryService
from app.core.config import get_settings

_inventory_token_manager: InventoryTokenManager | None = None


def _get_inventory_token_manager() -> InventoryTokenManager:
    global _inventory_token_manager
    if _inventory_token_manager is None:
        s = get_settings()
        _inventory_token_manager = InventoryTokenManager(api_key=s.INVENTORY_API_TOKEN)
    return _inventory_token_manager


def _build_inventory_client() -> InventoryClient:
    s = get_settings()
    # INVENTORY_CA (CA file path) wins; otherwise fall back to the bool switch.
    verify = s.INVENTORY_CA if s.INVENTORY_CA else s.INVENTORY_API_VERIFY_SSL
    return InventoryClient(
        base_url=s.INVENTORY_API_URL,
        token_manager=_get_inventory_token_manager(),
        timeout=s.INVENTORY_API_TIMEOUT_SECONDS,
        verify_ssl=verify,
    )


async def get_command_state_repository() -> CommandStateRepository:
    redis = await RedisClient.get_client()
    return CommandStateRepository(redis)


async def get_inventory_repository() -> InventoryRepository:
    return _build_inventory_client()


async def get_inventory_service(
    repo: InventoryRepository = Depends(get_inventory_repository),
) -> InventoryService:
    s = get_settings()
    return InventoryService(
        repo=repo,
        node_type_map=s.BASTION_NODE_TYPE_MAP,
        slash_map=s.CLUSTER_SLASH_TYPE_MAP,
    )


async def get_command_service(
    repo: CommandStateRepository = Depends(get_command_state_repository),
    inventory_repo: InventoryRepository = Depends(get_inventory_repository),
) -> CommandService:
    return CommandService(repo, inventory_repo)


async def get_trace_cache_repository() -> TraceCacheRepository:
    # Use the binary client: cache stores gzip-compressed bytes which the
    # default decode_responses=True client would fail to read.
    redis = await RedisClient.get_binary_client()
    return RedisTraceCache(redis)
