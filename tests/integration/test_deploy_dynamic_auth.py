"""
tests/integration/test_deploy_dynamic_auth.py
"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from app.core.config import get_settings
from app.domain.pipeline_models import PipelineData


@pytest.fixture
def mock_repo():
    with patch("app.api.v1.deploy.GitlabPipelineRepository") as mock:
        yield mock


@pytest.fixture
def mock_auth(tmp_path):
    settings = get_settings()
    auth_file = tmp_path / "gitlab_auth.json"
    auth_data = [
        {"name": "special-project", "project_id": 999, "token": "special-token"}
    ]
    auth_file.write_text(json.dumps(auth_data))

    # Update settings for the test
    original_path = settings.GITLAB_AUTH_JSON_PATH
    settings.GITLAB_AUTH_JSON_PATH = str(auth_file)
    yield auth_file
    settings.GITLAB_AUTH_JSON_PATH = original_path


def test_trigger_pipeline_with_custom_project(client, mock_repo, mock_auth):
    # Setup mock return value
    mock_instance = mock_repo.return_value
    mock_instance.trigger = AsyncMock(
        return_value=PipelineData(id=123, status="running", ref_name="main")
    )
    mock_instance.list_running = AsyncMock(return_value=[])

    # Bypass auth by patching decode_access_token
    with patch(
        "app.core.dependencies.decode_access_token",
        return_value={"sub": "admin", "scopes": ["deploy_api"]},
    ):
        response = client.post(
            "/api/v1/deploy/stage?action=test-deploy&ref_name=main&project_id=999",
            json={"variables": []},
            headers={"Authorization": "Bearer fake-token"},
        )

    assert response.status_code == 200
    mock_repo.assert_called_with(
        url=get_settings().GITLAB_URL,
        token="special-token",
        project_id=999,
        trace_cache=None,
    )


def test_trigger_pipeline_default_project(client, mock_repo):
    # Setup mock return value
    mock_instance = mock_repo.return_value
    mock_instance.trigger = AsyncMock(
        return_value=PipelineData(id=123, status="running", ref_name="main")
    )
    mock_instance.list_running = AsyncMock(return_value=[])

    with patch(
        "app.core.dependencies.decode_access_token",
        return_value={"sub": "admin", "scopes": ["deploy_api"]},
    ):
        response = client.post(
            "/api/v1/deploy/stage?action=test-deploy&ref_name=main",
            json={"variables": []},
            headers={"Authorization": "Bearer fake-token"},
        )

    assert response.status_code == 200
    settings = get_settings()
    mock_repo.assert_called_with(
        url=settings.GITLAB_URL,
        token=settings.GITLAB_TOKEN,
        project_id=settings.GITLAB_PROJECT_ID,
        trace_cache=None,
    )
