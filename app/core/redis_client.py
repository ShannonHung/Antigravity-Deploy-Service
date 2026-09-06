import logging
from typing import Optional
import redis.asyncio as redis
from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class RedisClient:
    _instance: Optional[redis.Redis] = None
    _binary_instance: Optional[redis.Redis] = None

    @classmethod
    async def get_client(cls) -> redis.Redis:
        if cls._instance is None:
            logger.info(f"Connecting to Redis at {settings.REDIS_URL}")
            cls._instance = redis.from_url(
                settings.REDIS_URL, encoding="utf-8", decode_responses=True
            )
        return cls._instance

    @classmethod
    async def get_binary_client(cls) -> redis.Redis:
        """Return a Redis client that returns raw bytes (no auto-decode).

        Use for values that are binary blobs (e.g. gzip-compressed payloads)
        where the default ``decode_responses=True`` client would crash with
        ``UnicodeDecodeError`` on the first non-utf-8 byte.
        """
        if cls._binary_instance is None:
            logger.info(f"Connecting to Redis (binary) at {settings.REDIS_URL}")
            cls._binary_instance = redis.from_url(
                settings.REDIS_URL,
                decode_responses=False,
            )
        return cls._binary_instance

    @classmethod
    async def _close_one(cls, client: redis.Redis, label: str) -> None:
        try:
            await client.aclose()
        except RuntimeError:
            # The client was created on a different event loop (happens when a
            # TestClient fixture shares the singleton with a previous test's
            # loop).  Disconnect without waiting so we don't block the current
            # loop on a Future that belongs to another one.
            await client.aclose(close_connection_pool=False)

    @classmethod
    async def close(cls):
        if cls._instance is not None:
            logger.info("Closing Redis connection.")
            await cls._close_one(cls._instance, "default")
            cls._instance = None
        if cls._binary_instance is not None:
            logger.info("Closing Redis (binary) connection.")
            await cls._close_one(cls._binary_instance, "binary")
            cls._binary_instance = None
