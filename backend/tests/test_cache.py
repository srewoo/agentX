from __future__ import annotations
"""Tests for app.services.cache — Redis cache with graceful degradation."""

import json
import pytest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.cache import CacheManager, make_cache_key


# ─────────────────────────────────────────────
# make_cache_key helper
# ─────────────────────────────────────────────

class TestMakeCacheKey:
    def test_simple_prefix(self):
        assert make_cache_key("stocks") == "stocks"

    def test_prefix_with_args(self):
        key = make_cache_key("quote", "RELIANCE", "1d")
        assert key == "quote:reliance:1d"

    def test_kwargs_sorted_alphabetically(self):
        key = make_cache_key("prefix", z="z_val", a="a_val")
        assert key.index("a=a_val") < key.index("z=z_val")

    def test_spaces_replaced_with_underscores(self):
        key = make_cache_key("pre fix", "arg 1")
        assert " " not in key


# ─────────────────────────────────────────────
# CacheManager — disconnected (no Redis)
# ─────────────────────────────────────────────

class TestCacheManagerDisconnected:
    """All operations should return safe defaults when not connected."""

    @pytest.fixture
    def cache(self):
        return CacheManager()

    @pytest.mark.asyncio
    async def test_get_returns_none_when_disabled(self, cache):
        result = await cache.get("any_key")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_returns_false_when_disabled(self, cache):
        result = await cache.set("key", {"data": 1})
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_disabled(self, cache):
        result = await cache.delete("key")
        assert result is False

    def test_enabled_false_by_default(self, cache):
        assert cache.enabled is False

    @pytest.mark.asyncio
    async def test_connect_returns_false_when_redis_unavailable(self):
        cache = CacheManager()
        with patch("app.services.cache.REDIS_AVAILABLE", False):
            result = await cache.connect()
        assert result is False


# ─────────────────────────────────────────────
# CacheManager — connected (mocked Redis)
# ─────────────────────────────────────────────

class TestCacheManagerConnected:
    @pytest.fixture
    def mock_redis_client(self):
        client = AsyncMock()
        client.ping = AsyncMock(return_value=True)
        client.get = AsyncMock(return_value=None)
        client.setex = AsyncMock(return_value=True)
        client.delete = AsyncMock(return_value=1)
        return client

    @pytest.fixture
    async def connected_cache(self, mock_redis_client):
        cache = CacheManager()
        with patch("app.services.cache.REDIS_AVAILABLE", True), \
             patch("app.services.cache.redis") as mock_redis_module:
            mock_redis_module.from_url.return_value = mock_redis_client
            await cache.connect()
        # Manually set client and enabled after the context manager
        cache._client = mock_redis_client
        cache._enabled = True
        return cache

    @pytest.mark.asyncio
    async def test_connect_returns_true_on_success(self, mock_redis_client):
        cache = CacheManager()
        with patch("app.services.cache.REDIS_AVAILABLE", True), \
             patch("app.services.cache.redis") as mock_redis_module:
            mock_redis_module.from_url.return_value = mock_redis_client
            result = await cache.connect()
        assert result is True

    @pytest.mark.asyncio
    async def test_connect_returns_false_on_connection_error(self):
        cache = CacheManager()
        failing_client = AsyncMock()
        failing_client.ping = AsyncMock(side_effect=Exception("refused"))
        with patch("app.services.cache.REDIS_AVAILABLE", True), \
             patch("app.services.cache.redis") as mock_redis_module:
            mock_redis_module.from_url.return_value = failing_client
            result = await cache.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_get_deserializes_json(self, mock_redis_client):
        cache = CacheManager()
        cache._client = mock_redis_client
        cache._enabled = True
        mock_redis_client.get.return_value = json.dumps({"price": 1500.0})

        result = await cache.get("stock:RELIANCE")
        assert result == {"price": 1500.0}

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing_key(self, mock_redis_client):
        cache = CacheManager()
        cache._client = mock_redis_client
        cache._enabled = True
        mock_redis_client.get.return_value = None

        result = await cache.get("missing_key")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_calls_setex_with_correct_ttl(self, mock_redis_client):
        cache = CacheManager()
        cache._client = mock_redis_client
        cache._enabled = True

        ttl = timedelta(minutes=10)
        await cache.set("mykey", {"data": 42}, ttl=ttl)

        mock_redis_client.setex.assert_called_once()
        call_args = mock_redis_client.setex.call_args
        assert call_args[0][0] == "mykey"
        assert call_args[0][1] == 600  # 10 min in seconds
        assert json.loads(call_args[0][2]) == {"data": 42}

    @pytest.mark.asyncio
    async def test_set_returns_true_on_success(self, mock_redis_client):
        cache = CacheManager()
        cache._client = mock_redis_client
        cache._enabled = True

        result = await cache.set("key", "value")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_calls_redis_delete(self, mock_redis_client):
        cache = CacheManager()
        cache._client = mock_redis_client
        cache._enabled = True

        await cache.delete("old_key")
        mock_redis_client.delete.assert_called_once_with("old_key")

    @pytest.mark.asyncio
    async def test_get_returns_none_on_redis_error(self, mock_redis_client):
        cache = CacheManager()
        cache._client = mock_redis_client
        cache._enabled = True
        mock_redis_client.get.side_effect = Exception("connection lost")

        result = await cache.get("key")
        assert result is None  # graceful degradation

    @pytest.mark.asyncio
    async def test_set_returns_false_on_redis_error(self, mock_redis_client):
        cache = CacheManager()
        cache._client = mock_redis_client
        cache._enabled = True
        mock_redis_client.setex.side_effect = Exception("write fail")

        result = await cache.set("key", "val")
        assert result is False
