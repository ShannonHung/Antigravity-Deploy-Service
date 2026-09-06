"""
app/repositories/gitlab_auth_repository.py

Repository for fetching GitLab authentication details from a JSON mapping file.
The file format is expected to be a list of objects, each containing:
name, project_id, token.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TypedDict, List, Optional

_logger = logging.getLogger(__name__)


class GitlabAuthEntry(TypedDict):
    name: str
    project_id: str  # JSON might have it as string or int
    token: str


class GitlabAuthRepository:
    """Reads GitLab project-to-token mappings from a JSON file."""

    def __init__(self, json_path: str | Path) -> None:
        self._path = Path(json_path)

    def _load(self) -> List[dict]:
        """Load the JSON file. Returns empty list if file not found."""
        if not self._path.exists():
            _logger.warning("GitLab auth JSON file not found | path=%s", self._path)
            return []

        try:
            with self._path.open(encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, IOError) as exc:
            _logger.error(
                "Failed to read GitLab auth JSON | path=%s | error=%s", self._path, exc
            )
            return []

    def get_token_by_project_id(self, project_id: int) -> Optional[str]:
        """Search for a token matching the given project_id.

        Matches project_id regardless of whether it's stored as int or string in JSON.
        """
        records = self._load()
        pid_str = str(project_id)

        for record in records:
            # Flexible matching for project_id (int or string)
            record_pid = str(record.get("project_id", ""))
            if record_pid == pid_str:
                return record.get("token")

        return None
