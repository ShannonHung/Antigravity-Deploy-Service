"""
app/services/auth_service.py

Business logic for authentication and token management.

Depends on UserRepository (abstract) — never imports a concrete backend.
All cryptographic work is delegated to app.core.security.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from app.core.config import get_settings
from app.core.exceptions import AuthException
from app.core.security import (
    create_access_token,
    hash_password,
    verify_password,
)
from app.domain.models import (
    HashPasswordData,
    MyScopesData,
    TokenData,
    User,
    VerifyData,
)
from app.repositories.user_repository import UserRepository

_logger = logging.getLogger(__name__)


class AuthService:
    """Handles authentication, token issuance, and credential utilities."""

    def __init__(self, user_repo: UserRepository) -> None:
        self._repo = user_repo

    # ── Public methods ─────────────────────────────────────────────────────────

    async def authenticate(self, account: str, password: str) -> User:
        """Verify credentials and return a public User domain object.

        Raises:
            AuthException: If the account does not exist or the password is wrong.
        """
        user_in_db = await self._repo.get_by_account(account)
        if user_in_db is None or not verify_password(
            password, user_in_db.hashed_password
        ):
            _logger.warning("Authentication failed | account=%s", account)
            raise AuthException(
                "Invalid account or password.",
            )

        _logger.info("Authentication successful | account=%s", account)
        return User(account=user_in_db.account, scopes=user_in_db.scopes)

    async def generate_token(self, user: User) -> TokenData:
        """Issue a JWT access token for the given user.

        Args:
            user: Authenticated domain user object.

        Returns:
            TokenData with the encoded JWT and metadata.
        """
        settings = get_settings()
        expires_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        token = create_access_token(
            data={"sub": user.account, "scopes": user.scopes},
            expires_delta=expires_delta,
        )
        _logger.info("Token issued | account=%s | scopes=%s", user.account, user.scopes)
        return TokenData(
            access_token=token,
            token_type="bearer",
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    async def hash_plain_password(self, plain: str) -> HashPasswordData:
        """Hash a plain-text password using bcrypt.

        Used by POST /api/v1/auth/hash-password so operators can generate
        new password hashes to paste into users.json.
        """
        hashed = hash_password(plain)
        return HashPasswordData(hashed_password=hashed)

    async def get_my_scopes(self, user: User) -> MyScopesData:
        """Return the scopes embedded in the caller's token."""
        return MyScopesData(account=user.account, scopes=user.scopes)

    async def verify_user(self, user: User) -> VerifyData:
        """Confirm that a token is valid and return its claims."""
        return VerifyData(account=user.account, scopes=user.scopes, valid=True)
