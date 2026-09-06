"""
app/repositories/json_user_repository.py

JSON file-backed implementation of UserRepository.

The file is read on every call (no in-process caching) so that changes to
users.json are picked up without restarting the service — suitable for
small-scale deployments.  Replace with a caching layer or a proper DB
implementation (e.g. SqlUserRepository) when needed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.core.exceptions import NotFoundException
from app.domain.models import UserInDB
from app.repositories.user_repository import UserRepository

_logger = logging.getLogger(__name__)


class JsonUserRepository(UserRepository):
    """Reads user records from a JSON file.

    Expected file format::

        [
            {
                "account": "admin",
                "hashed_password": "<bcrypt hash>",
                "scopes": ["deploy_api", "vm_api"]
            }
        ]
    """

    def __init__(self, json_path: str | Path) -> None:
        self._path = Path(json_path)

    # ── private helpers ───────────────────────────────────────────────────────

    def _load(self) -> list[dict]:
        if not self._path.exists():
            _logger.error("Users JSON file not found | path=%s", self._path)
            raise NotFoundException(
                f"Users data file not found at '{self._path}'.",
            )
        with self._path.open(encoding="utf-8") as fh:
            return json.load(fh)

    # ── interface implementation ───────────────────────────────────────────────

    async def get_by_account(self, account: str) -> UserInDB | None:
        records = self._load()
        for record in records:
            if record.get("account") == account:
                _logger.debug("User found | account=%s", account)
                return UserInDB(**record)
        _logger.debug("User not found | account=%s", account)
        return None

    async def list_accounts(self) -> list[str]:
        records = self._load()
        return [r["account"] for r in records if "account" in r]
