"""
app/core/security.py

JWT utilities and password hashing.
All cryptographic operations live here — never in routers or services.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
import bcrypt as _bcrypt

from app.core.config import get_settings
from app.core.exceptions import AuthException

# ──────────────────────────────────────────────────────────────────────────────
# Password helpers
# ──────────────────────────────────────────────────────────────────────────────


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain*."""
    return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches the stored *hashed* password."""
    return _bcrypt.checkpw(plain.encode(), hashed.encode())


# ──────────────────────────────────────────────────────────────────────────────
# JWT helpers
# ──────────────────────────────────────────────────────────────────────────────


def create_access_token(
    data: dict[str, Any],
    expires_delta: timedelta | None = None,
) -> str:
    """Encode a JWT access token.

    Args:
        data: Payload to embed (must include at least ``sub``).
        expires_delta: Custom TTL; falls back to settings value.
    """
    settings = get_settings()
    payload = data.copy()
    expire = datetime.now(tz=timezone.utc) + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload["exp"] = expire
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT access token.

    Returns:
        The decoded payload dict.

    Raises:
        AuthException: If the token is expired, invalid, or cannot be decoded.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        return payload
    except JWTError as exc:
        raise AuthException(
            "Invalid or expired token.",
            detail=str(exc),
        ) from exc
