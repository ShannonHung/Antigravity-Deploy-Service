"""
tests/conftest.py

Shared pytest fixtures.
Sets APP_ENV=test before any import so Settings picks up .env.test.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

# Must be set BEFORE importing app modules that call get_settings()
os.environ.setdefault("APP_ENV", "test")

from fastapi.testclient import TestClient
from app.main import create_app

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_SSH_FIXTURE_PATH = _FIXTURES_DIR / "SSH-default.json"


def _generate_ssh_default_fixture() -> None:
    """Write a throwaway ed25519 key into tests/fixtures/SSH-default.json.

    The key is generated fresh on every test run and is never connected to
    anywhere — it only exists so create_authenticator() has something valid
    to base64-decode during _prepare_execution. Kept out of git via
    .gitignore (committing private-key material, even fake ones, trips
    secret scanners and trains people to ignore real findings).
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization as _ser

        _key = Ed25519PrivateKey.generate()
        _pem = _key.private_bytes(
            encoding=_ser.Encoding.PEM,
            format=_ser.PrivateFormat.OpenSSH,
            encryption_algorithm=_ser.NoEncryption(),
        )
        key_b64 = base64.b64encode(_pem).decode()
    except Exception:
        with tempfile.TemporaryDirectory() as td:
            key_path = Path(td) / "id_ed25519"
            subprocess.run(
                [
                    "ssh-keygen",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-f",
                    str(key_path),
                    "-C",
                    "test-fixture-key",
                    "-q",
                ],
                check=True,
            )
            key_b64 = base64.b64encode(key_path.read_bytes()).decode()
    _SSH_FIXTURE_PATH.write_text(
        json.dumps(
            {
                "host": "localhost",
                "port": 2223,
                "username": "root",
                "auth_method": "key",
                "key_base64": key_b64,
            }
        )
    )


@pytest.fixture(scope="session", autouse=True)
def _ssh_default_fixture():
    """Materialise tests/fixtures/SSH-default.json before tests, remove after."""
    _generate_ssh_default_fixture()
    yield
    _SSH_FIXTURE_PATH.unlink(missing_ok=True)


@pytest.fixture(scope="session")
def client() -> TestClient:
    """TestClient backed by the full app with test settings."""
    app = create_app()
    with TestClient(app) as c:
        yield c
