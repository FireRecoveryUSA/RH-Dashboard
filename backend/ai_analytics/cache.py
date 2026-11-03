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

# How long a retained good entry keeps serving after an incomplete
# recomputation — a bounded retry window so requests don't repeat the
# expensive read on every call during a partial outage.
_INCOMPLETE_RETRY_TTL = 120  # seconds

# Maximum age of a retained value that may keep being served over incomplete
# recomputations. Past this, the fresh (incomplete) result is returned so the
# caller sees the outage instead of indefinitely stale totals.
_MAX_STALE_AGE = 3600  # seconds

# (expires_at, value, computed_at): computed_at is when the value was last
# produced by a complete computation — renewed stale serves keep the original.
_cache: Dict[str, Tuple[float, Any, float]] = {}


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

            entry = _cache.get(key)
            if entry is not None:
                expires_at, value, _ = entry
                if time.time() < expires_at:
                    logger.debug(f"AI analytics cache hit: {name}")
                    return value

            # Miss or expired — compute. Expiry is measured from write time so
            # a slow compute does not shave its own runtime off the TTL. An
            # expired entry is retained until a cacheable result replaces it:
            # a transient outage's incomplete result must not overwrite the
            # last good value (the claim-comparison warmer does the same via
            # touch_cache).
            result = await fn(*args, **kwargs)
            if getattr(result, "data_complete", True) is False:
                if entry is not None:
                    expires_at, value, computed_at = entry
                    if time.time() - computed_at <= _MAX_STALE_AGE:
                        logger.warning(
                            f"AI analytics cache: keeping stale {name} entry over incomplete result"
                        )
                        # Bounded retry: serve the stale value for a short
                        # window instead of recomputing on every request —
                        # never past the max stale age.
                        _cache[key] = (
                            min(
                                time.time() + _INCOMPLETE_RETRY_TTL,
                                computed_at + _MAX_STALE_AGE,
                            ),
                            value,
                            computed_at,
                        )
                        return value
                    # Older than the max stale age — serve the fresh incomplete
                    # result so the caller sees the outage, not stale totals.
                    logger.warning(
                        f"AI analytics cache: stale {name} entry exceeded max age; returning incomplete result"
                    )
                # No good entry to fall back on — don't cache the incomplete
                # result so the next request retries the source.
                return result
            _cache[key] = (time.time() + ttl, result, time.time())
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
    computed_at: Optional[float] = None,
) -> None:
    """Store ``value`` under the same key the ``cached`` wrapper would use.

    The background warmer refreshes entries unconditionally: a plain call
    would hit the still-valid cached entry and never extend its expiry, so
    the TTL would lapse between warm cycles. Expiry is measured from write
    time.
    """
    if _is_testing():
        return
    _cache[_make_key(func_name, args, kwargs)] = (
        time.time() + ttl,
        value,
        computed_at if computed_at is not None else time.time(),
    )


def touch_cache(
    func_name: str,
    args: tuple,
    kwargs: dict,
    ttl: int = _DEFAULT_TTL,
) -> bool:
    """Extend an existing entry's expiry without changing its value.

    Used by the warmer after a failed refresh: the last good value stays
    servable until a successful warm (or a request) produces a complete
    replacement. Returns False when there is no entry, or the entry's value
    is itself incomplete — extending an incomplete entry would pin an empty
    dashboard in place and stop requests from retrying the source. Returns
    False too when the value is older than ``_MAX_STALE_AGE``: the caller
    should see the outage rather than indefinitely stale totals.
    """
    key = _make_key(func_name, args, kwargs)
    entry = _cache.get(key)
    if (
        entry is None
        or getattr(entry[1], "data_complete", True) is False
        or time.time() - entry[2] > _MAX_STALE_AGE
    ):
        return False
    # Renewal never pushes expiry past the max stale age — the entry must
    # lapse at the deadline so the next request surfaces the outage.
    _cache[key] = (
        min(time.time() + ttl, entry[2] + _MAX_STALE_AGE),
        entry[1],
        entry[2],
    )
    return True


def clear_cache() -> None:
    """Clear all cached entries."""
    _cache.clear()


def cache_size() -> int:
    """Return the number of cached entries."""
    return len(_cache)
