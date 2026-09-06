"""
tests/unit/test_gitlab_auth_repository.py
"""

import json
import pytest
from pathlib import Path
from app.repositories.gitlab_auth_repository import GitlabAuthRepository


def test_get_token_by_project_id_found(tmp_path):
    auth_file = tmp_path / "gitlab_auth.json"
    data = [
        {"name": "project1", "project_id": 123, "token": "token123"},
        {"name": "project2", "project_id": "456", "token": "token456"},
    ]
    auth_file.write_text(json.dumps(data))

    repo = GitlabAuthRepository(auth_file)

    # Test with int
    assert repo.get_token_by_project_id(123) == "token123"
    # Test with str-backed int
    assert repo.get_token_by_project_id(456) == "token456"


def test_get_token_by_project_id_not_found(tmp_path):
    auth_file = tmp_path / "gitlab_auth.json"
    data = [{"name": "project1", "project_id": 123, "token": "token123"}]
    auth_file.write_text(json.dumps(data))

    repo = GitlabAuthRepository(auth_file)
    assert repo.get_token_by_project_id(999) is None


def test_get_token_by_project_id_missing_file(tmp_path):
    repo = GitlabAuthRepository(tmp_path / "missing.json")
    assert repo.get_token_by_project_id(123) is None


def test_get_token_by_project_id_malformed_json(tmp_path):
    auth_file = tmp_path / "bad.json"
    auth_file.write_text("not json")

    repo = GitlabAuthRepository(auth_file)
    assert repo.get_token_by_project_id(123) is None
