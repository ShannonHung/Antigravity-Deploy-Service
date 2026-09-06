"""
tests/unit/test_auth_service.py

Unit tests for AuthService — repository is fully mocked.
No file I/O, no network, no JWT verification overhead.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.core.exceptions import AuthException
from app.domain.models import User, UserInDB
from app.services.auth_service import AuthService

# ── Fixtures ──────────────────────────────────────────────────────────────────

HASHED_SECRET = "$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW"


def _make_service(user_in_db: UserInDB | None = None) -> AuthService:
    """Return an AuthService backed by a mocked repository."""
    repo = MagicMock()
    repo.get_by_account = AsyncMock(return_value=user_in_db)
    repo.list_accounts = AsyncMock(return_value=[])
    return AuthService(repo)


# ── authenticate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_authenticate_success():
    user_in_db = UserInDB(
        account="admin",
        hashed_password=HASHED_SECRET,
        scopes=["deploy_api"],
    )
    svc = _make_service(user_in_db)
    user = await svc.authenticate("admin", "secret")
    assert user.account == "admin"
    assert "deploy_api" in user.scopes


@pytest.mark.asyncio
async def test_authenticate_wrong_password():
    user_in_db = UserInDB(
        account="admin",
        hashed_password=HASHED_SECRET,
        scopes=["deploy_api"],
    )
    svc = _make_service(user_in_db)
    with pytest.raises(AuthException):
        await svc.authenticate("admin", "wrongpassword")


@pytest.mark.asyncio
async def test_authenticate_unknown_account():
    svc = _make_service(user_in_db=None)  # repo returns None
    with pytest.raises(AuthException):
        await svc.authenticate("ghost", "secret")


# ── generate_token ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_token_returns_bearer():
    user = User(account="admin", scopes=["deploy_api"])
    svc = _make_service()
    token_data = await svc.generate_token(user)
    assert token_data.token_type == "bearer"
    assert token_data.access_token  # non-empty string
    assert token_data.expires_in > 0


# ── hash_plain_password ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hash_plain_password_differs_from_plain():
    svc = _make_service()
    result = await svc.hash_plain_password("mynewpassword")
    assert result.hashed_password != "mynewpassword"
    assert result.hashed_password.startswith("$2b$")


@pytest.mark.asyncio
async def test_hash_plain_password_is_unique():
    """bcrypt should produce a different hash each call (random salt)."""
    svc = _make_service()
    h1 = (await svc.hash_plain_password("samepassword")).hashed_password
    h2 = (await svc.hash_plain_password("samepassword")).hashed_password
    assert h1 != h2


# ── get_my_scopes / verify_user ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_my_scopes_reflects_user():
    user = User(account="deployer", scopes=["deploy_api"])
    svc = _make_service()
    result = await svc.get_my_scopes(user)
    assert result.account == "deployer"
    assert result.scopes == ["deploy_api"]


@pytest.mark.asyncio
async def test_verify_user_valid():
    user = User(account="admin", scopes=["vm_api"])
    svc = _make_service()
    result = await svc.verify_user(user)
    assert result.valid is True
