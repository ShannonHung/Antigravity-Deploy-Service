"""
app/core/config.py

Multi-environment settings using Pydantic BaseSettings.
The active environment is selected by the APP_ENV environment variable:
  - dev   → loads .env.dev
  - prod  → loads .env.prod
  - test  → loads .env.test
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Dict, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── App meta ──────────────────────────────────────────────────────────────
    APP_ENV: Literal["dev", "prod", "test"] = "dev"
    APP_NAME: str = "Deploy Service"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # ── JWT ───────────────────────────────────────────────────────────────────
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # ── Storage ───────────────────────────────────────────────────────────────
    USERS_JSON_PATH: str = "data/users.json"

    # ── GitLab ────────────────────────────────────────────────────────────────
    GITLAB_URL: str = "https://gitlab.com/"
    GITLAB_TOKEN: str = ""
    GITLAB_PROJECT_ID: int = 0
    GITLAB_AUTH_JSON_PATH: str = "/data/gitlab_auth.json"
    # TCP-level timeout passed to the python-gitlab HTTP client. Covers all
    # API calls (trigger, get, list, cancel, retry). Set high enough to
    # tolerate a slow internal GitLab instance while still being shorter than
    # any upstream ingress timeout so we return our own 504 first.
    GITLAB_CA: str = ""
    GITLAB_HTTP_TIMEOUT_SECONDS: int = 120
    # Upper bound on a single GitLab trace fetch (asyncio-level guard).
    # Should be ≤ GITLAB_HTTP_TIMEOUT_SECONDS.
    GITLAB_TRACE_TIMEOUT_SECONDS: int = 45
    # Cache TTL for finished-job traces (immutable). A poll for a cached
    # finished job hits Redis and skips GitLab entirely.
    GITLAB_TRACE_CACHE_TTL_SECONDS: int = 86400
    # Soft cap: trace size at which the viewer shows a "log is large"
    # banner pointing to GitLab but keeps rendering. UX hint only.
    GITLAB_TRACE_SOFT_CAP_BYTES: int = 5 * 1024 * 1024
    # Hard cap: trace size at which the service stops returning lines and
    # the viewer switches to the fatal-error panel with a GitLab link.
    # Protects pod memory from runaway logs.
    GITLAB_TRACE_HARD_CAP_BYTES: int = 10 * 1024 * 1024

    # ── SSH Command API ───────────────────────────────────────────────────────
    COMMAND_CONFIG_DIR: str = "data"
    COMMAND_DEFAULT_TIMEOUT: int = 30
    COMMAND_KILL_GRACE_SECONDS: int = 2
    COMMAND_MAX_CONCURRENCY: int = 20
    COMMAND_MAX_RUNNING: int = 50
    SSH_CONNECT_TIMEOUT_SECONDS: int = 30
    # Control_node directory where run-ansible.sh tees per-run logs.
    COMMAND_LOG_DIR: str = "/var/log/deploy-service"
    # Soft cap → CommandTraceResponse.size_warning (banner, keep polling).
    COMMAND_LOG_SOFT_CAP_BYTES: int = 5 * 1024 * 1024
    # Hard cap → CommandTraceResponse.too_large (viewer stops polling).
    COMMAND_LOG_HARD_CAP_BYTES: int = 10 * 1024 * 1024
    # For `logged` commands: on failure, keep only the last N lines of output in
    # Redis as an error summary (full log lives on the control_node / /view).
    # 0 = store nothing even on failure.
    COMMAND_LOG_FAILURE_TAIL_LINES: int = 50

    # ── Inventory API (hostname lookup, cluster node lookup, bastion mappings) ──
    INVENTORY_API_URL: str = "http://localhost:9001"
    INVENTORY_API_TOKEN: str = "fake-inventory-token"
    INVENTORY_API_TIMEOUT_SECONDS: float = 5.0
    INVENTORY_API_VERIFY_SSL: bool = True
    # JSON string mapping node_type → bastion_type, e.g.:
    # BASTION_NODE_TYPE_MAP='{"baremetal": "type1", "virtual-machine": "type2"}'
    BASTION_NODE_TYPE_MAP: Dict[str, str] = {}
    # Node label key used to extract the SSH target IP for HOSTNAME host_type.
    INVENTORY_IP_LABEL: str = "mgmt_ip"
    # Maps slash-presence of a cluster_name to a bastion_type. Keys MUST be
    # "no_slash" and "with_slash". Example:
    #   CLUSTER_SLASH_TYPE_MAP='{"no_slash": "type1", "with_slash": "type2"}'
    CLUSTER_SLASH_TYPE_MAP: Dict[str, str] = {}

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    COMMAND_RESULT_TTL_SECONDS: int = 86400

    _APP_ENV: str = os.getenv("APP_ENV", "dev")
    model_config = SettingsConfigDict(
        # test: only .env.test — never .env.local, so CI/test runs are isolated.
        # other envs: .env (base) → .env.{APP_ENV} → .env.local (local overrides).
        env_file=(
            [".env.test"]
            if os.getenv("APP_ENV") == "test"
            else [".env", f".env.{os.getenv('APP_ENV', 'dev')}", ".env.local"]
        ),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (loaded once per process)."""
    return Settings()
