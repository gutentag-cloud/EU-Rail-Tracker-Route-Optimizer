"""
Redis cache layer with graceful fallback to no-op.
"""

from __future__ import annotations
import json, hashlib, logging
from typing import Any, Optional
from .config import settings

log = logging.getLogger(__name__)

try:
    import redis.asyncio as aioredis
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False


class Cache:
    """Async Redis cache with automatic fallback."""

    def __init__(self) -> None:
        self._redis: Optional[Any] = None
        self.connected = False

    async def connect(self) -> bool:
        if not HAS_REDIS or not settings.redis_url:
            log.info("⚠️  Redis not configured — caching disabled")
            return False
        try:
            self._redis = aioredis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=3,
            )
            await self._redis.ping()
            self.connected = True
            log.info("✅ Redis connected")
            return True
        except Exception as e:
            log.warning(f"⚠️  Redis connection failed: {e} — caching disabled")
            self._redis = None
            self.connected = False
            return False

    async def close(self) -> None:
        if self._redis:
            await self._redis.close()

    @staticmethod
    def _make_key(namespace: str, **kwargs) -> str:
        raw = json.dumps(kwargs, sort_keys=True, default=str)
        h = hashlib.md5(raw.encode()).hexdigest()[:12]
        return f"rail:{namespace}:{h}"

    async def get(self, namespace: str, **kwargs) -> Optional[Any]:
        if not self._redis:
            return None
        try:
            key = self._make_key(namespace, **kwargs)
            data = await self._redis.get(key)
            return json.loads(data) if data else None
        except Exception:
            return None

    async def set(self, namespace: str, value: Any,
                  ttl: int = 60, **kwargs) -> None:
        if not self._redis:
            return
        try:
            key = self._make_key(namespace, **kwargs)
            await self._redis.setex(key, ttl, json.dumps(
                value, default=str
            ))
        except Exception:
            pass

    async def delete_pattern(self, pattern: str) -> int:
        if not self._redis:
            return 0
        try:
            keys = []
            async for key in self._redis.scan_iter(f"rail:{pattern}:*"):
                keys.append(key)
            if keys:
                return await self._redis.delete(*keys)
            return 0
        except Exception:
            return 0

    async def get_stats(self) -> dict:
        if not self._redis:
            return {"connected": False}
        try:
            info = await self._redis.info("memory")
            db_size = await self._redis.dbsize()
            return {
                "connected": True,
                "used_memory": info.get("used_memory_human", "?"),
                "keys": db_size,
            }
        except Exception:
            return {"connected": False}


# Global singleton
cache = Cache()
