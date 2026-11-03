"""Tests for the AI analytics TTL cache."""

import asyncio
import os
import pytest
from unittest.mock import patch, AsyncMock

from ai_analytics.cache import cached, clear_cache, cache_size


class TestTtlCache:
    def setup_method(self):
        clear_cache()

    def test_cache_disabled_in_testing(self):
        """When TESTING=true, cache should be bypassed."""
        call_count = 0

        @cached(ttl=60)
        async def fn():
            nonlocal call_count
            call_count += 1
            return call_count

        # TESTING is set to "true" in conftest
        result1 = asyncio.run(fn())
        result2 = asyncio.run(fn())
        assert result1 == 1
        assert result2 == 2  # Not cached — called twice

    def test_cache_works_when_not_testing(self):
        """When TESTING is not set, cache should return cached result."""
        # Temporarily clear TESTING
        old = os.environ.get("TESTING", "")
        os.environ["TESTING"] = ""
        try:
            clear_cache()
            call_count = 0

            @cached(ttl=60)
            async def fn():
                nonlocal call_count
                call_count += 1
                return call_count

            result1 = asyncio.run(fn())
            result2 = asyncio.run(fn())
            assert result1 == 1
            assert result2 == 1  # Cached — same result
            assert call_count == 1
        finally:
            os.environ["TESTING"] = old

    def test_clear_cache(self):
        old = os.environ.get("TESTING", "")
        os.environ["TESTING"] = ""
        try:
            call_count = 0

            @cached(ttl=60)
            async def fn():
                nonlocal call_count
                call_count += 1
                return call_count

            asyncio.run(fn())
            assert cache_size() == 1
            clear_cache()
            assert cache_size() == 0
            asyncio.run(fn())
            assert call_count == 2
        finally:
            os.environ["TESTING"] = old
