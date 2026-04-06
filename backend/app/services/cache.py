from __future__ import annotations
"""Redis caching utility with graceful degradation when Redis is unavailable."""
import asyncio
import json
import logging
from typing import Any, Optional
from datetime import timedelta

logger = logging.getLogger(__name__)

# Timeout for individual Redis operations (seconds)
_REDIS_OP_TIMEOUT = 5

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("redis-py not installed. Caching disabled.")


class CacheManager:
    """Async Redis cache manager with JSON serialization."""

    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis_url = redis_url
        self._client: Optional[Any] = None
        self._enabled = False

    async def connect(self, redis_url: Optional[str] = None) -> bool:
        if redis_url:
            self.redis_url = redis_url
        if not REDIS_AVAILABLE:
            return False
        try:
            self._client = redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=_REDIS_OP_TIMEOUT,
            )
            await self._client.ping()
            self._enabled = True
            logger.info(f"Connected to Redis at {self.redis_url}")
            return True
        except Exception as e:
            logger.warning(f"Redis connection failed: {e}. Caching disabled.")
            self._enabled = False
            return False

    async def disconnect(self):
        if self._client:
            await self._client.close()
            self._enabled = False

    async def get(self, key: str) -> Optional[Any]:
        if not self._enabled or not self._client:
            return None
        try:
            value = await asyncio.wait_for(
                self._client.get(key), timeout=_REDIS_OP_TIMEOUT
            )
            return json.loads(value) if value is not None else None
        except asyncio.TimeoutError:
            logger.warning(f"Cache GET timed out for {key}")
            return None
        except Exception as e:
            logger.error(f"Cache GET error for {key}: {e}")
            return None

    async def set(self, key: str, value: Any, ttl: timedelta = timedelta(minutes=5)) -> bool:
        if not self._enabled or not self._client:
            return False
        try:
            serialized = json.dumps(value, default=str)
            await asyncio.wait_for(
                self._client.setex(key, int(ttl.total_seconds()), serialized),
                timeout=_REDIS_OP_TIMEOUT,
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(f"Cache SET timed out for {key}")
            return False
        except Exception as e:
            logger.error(f"Cache SET error for {key}: {e}")
            return False

    async def delete(self, key: str) -> bool:
        if not self._enabled or not self._client:
            return False
        try:
            await asyncio.wait_for(
                self._client.delete(key), timeout=_REDIS_OP_TIMEOUT
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(f"Cache DELETE timed out for {key}")
            return False
        except Exception as e:
            logger.error(f"Cache DELETE error for {key}: {e}")
            return False

    @property
    def enabled(self) -> bool:
        return self._enabled


# Global instance
cache_manager = CacheManager()


def make_cache_key(prefix: str, *args, **kwargs) -> str:
    key_parts = [prefix] + [str(a) for a in args]
    for k, v in sorted(kwargs.items()):
        key_parts.append(f"{k}={v}")
    return ":".join(key_parts).replace(" ", "_").lower()
