"""
app/repositories/trace_cache_repository.py

Cache for finished GitLab job traces.

A finished job's trace is immutable, so caching the full bytes lets us serve
incremental polls (UI sends byte_offset; we slice locally) without hitting
GitLab again. Stored gzip-compressed because CI traces compress ~80–90%.

Layered as: abstract ``TraceCacheRepository`` interface + ``RedisTraceCache``
implementation, so the GitLab repository depends only on the contract.
"""

from __future__ import annotations

import gzip
import logging
from abc import ABC, abstractmethod
from datetime import timedelta

from redis.asyncio import Redis

_logger = logging.getLogger(__name__)


class TraceCacheRepository(ABC):
    """Abstract contract for caching immutable finished-job traces."""

    @abstractmethod
    async def get(self, project_id: int, job_id: int) -> tuple[str, bytes] | None:
        """Return ``(status, raw_trace_bytes)``, or ``None`` on miss."""

    @abstractmethod
    async def set(
        self,
        project_id: int,
        job_id: int,
        status: str,
        raw: bytes,
        ttl_seconds: int,
    ) -> None:
        """Store *status* + *raw* trace bytes with *ttl_seconds* expiry."""


class RedisTraceCache(TraceCacheRepository):
    """Redis-backed cache that gzip-compresses ``status\\n + trace bytes``.

    Cache layout: ``gzip(status_str.encode() + b"\\n" + raw_trace_bytes)``.
    Status is a short job-status string ("success" / "failed" / ...) that
    never contains a newline, so a single split on the first ``\\n`` is
    unambiguous. Co-locating status with bytes means a cache hit needs zero
    GitLab requests.
    """

    PREFIX = "gitlab:trace"

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    def _key(self, project_id: int, job_id: int) -> str:
        return f"{self.PREFIX}:{project_id}:{job_id}"

    async def get(self, project_id: int, job_id: int) -> tuple[str, bytes] | None:
        blob = await self._redis.get(self._key(project_id, job_id))
        if blob is None:
            return None
        if isinstance(blob, str):
            blob = blob.encode("latin-1")
        try:
            decompressed = gzip.decompress(blob)
        except (OSError, gzip.BadGzipFile) as exc:
            _logger.warning(
                "Discarding corrupted trace cache | project=%s job=%s | %s",
                project_id,
                job_id,
                exc,
            )
            return None

        nl = decompressed.find(b"\n")
        if nl == -1:
            _logger.warning(
                "Malformed trace cache (no status delimiter) | project=%s job=%s",
                project_id,
                job_id,
            )
            return None
        status = decompressed[:nl].decode("ascii", errors="replace")
        raw = decompressed[nl + 1 :]
        return status, raw

    async def set(
        self,
        project_id: int,
        job_id: int,
        status: str,
        raw: bytes,
        ttl_seconds: int,
    ) -> None:
        payload = status.encode("ascii", errors="replace") + b"\n" + raw
        compressed = gzip.compress(payload, compresslevel=6)
        await self._redis.setex(
            self._key(project_id, job_id),
            timedelta(seconds=ttl_seconds),
            compressed,
        )
