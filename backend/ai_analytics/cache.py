"""Simple in-memory TTL cache for AI analytics aggregation results.

Caches the result of expensive aggregation functions for a short TTL
(default 60 seconds) keyed by the filter arguments. This is intentionally
a lightweight per-process cache — no external dependency required.

The cache is disabled in TESTING mode to ensure tests always see fresh data.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 60  # seconds

_cache: Dict[str, Tuple[float, Any]] = {}


def _is_testing() -> bool:
    return os.environ.get("TESTING", "").lower() in ("1", "true", "yes")


def _make_key(func_name: str, args: tuple, kwargs: dict) -> str:
    """Build a stable cache key from function name and arguments."""
    try:
        key_data = json.dumps({"args": list(args), "kwargs": kwargs}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        key_data = str(args) + str(sorted(kwargs.items()))
    return hashlib.md5(f"{func_name}:{key_data}".encode()).hexdigest()


def cached(
    ttl: int = _DEFAULT_TTL,
    func_name: Optional[str] = None,
) -> Callable:
    """Decorator that caches the result of an async function for ``ttl`` seconds.

    Only caches when not in TESTING mode.
    """
    def decorator(fn: Callable) -> Callable:
        name = func_name or fn.__name__

        async def wrapper(*args, **kwargs):
            if _is_testing():
                return await fn(*args, **kwargs)

            key = _make_key(name, args, kwargs)
            now = time.time()

            # Check cache
            entry = _cache.get(key)
            if entry is not None:
                expires_at, value = entry
                if now < expires_at:
                    logger.debug(f"AI analytics cache hit: {name}")
                    return value
                else:
                    del _cache[key]

            # Cache miss — compute. Expiry is measured from write time so a
            # slow compute does not shave its own runtime off the TTL.
            result = await fn(*args, **kwargs)
            _cache[key] = (time.time() + ttl, result)
            logger.debug(f"AI analytics cache miss: {name} (cached for {ttl}s)")
            return result

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper

    return decorator


def prime_cache(
    func_name: str,
    args: tuple,
    kwargs: dict,
    value: Any,
    ttl: int = _DEFAULT_TTL,
) -> None:
    """Store ``value`` under the same key the ``cached`` wrapper would use.

    The background warmer refreshes entries unconditionally: a plain call
    would hit the still-valid cached entry and never extend its expiry, so
    the TTL would lapse between warm cycles. Expiry is measured from write
    time.
    """
    if _is_testing():
        return
    _cache[_make_key(func_name, args, kwargs)] = (time.time() + ttl, value)


def clear_cache() -> None:
    """Clear all cached entries."""
    _cache.clear()


def cache_size() -> int:
    """Return the number of cached entries."""
    return len(_cache)
