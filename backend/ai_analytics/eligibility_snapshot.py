"""Dashboard-owned snapshot of department AI eligibility.

``get_ai_participation_map`` derives eligibility from the fee-tile
configuration in the operational RecoveryHub_AI Mongo. Projection-mode
analytics must not depend on that cluster per request, so the last
successful full map is persisted in the dashboard-owned Mongo and served
from there while fresh. The operational source is consulted only to refresh
a missing or expired snapshot; if that refresh fails, the stale snapshot is
still served (and reported as ``stale``) rather than failing the request.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from ai_analytics_worker.config import worker_config

logger = logging.getLogger(__name__)

ParticipationMap = Dict[int, Dict[str, Any]]
ParticipationFetcher = Callable[[Any, Any], Awaitable[Optional[ParticipationMap]]]

STATUS_AVAILABLE = "available"
STATUS_STALE = "stale"
STATUS_UNAVAILABLE = "unavailable"

# Snapshot document schema. v1 stored a boolean-only participation map
# (no fee catalogs); v2 entries may also carry ``fee_catalog`` /
# ``fee_catalog_version`` for step-1 result matching. A stored doc whose
# ``schema_version`` is not current is reported as never-refreshed so a
# fresh legacy snapshot is still refreshed immediately — it remains
# servable as the stale fallback if the source read fails.
SNAPSHOT_SCHEMA_VERSION = 2


async def read_snapshot(
    db: Any,
) -> Tuple[Optional[ParticipationMap], Optional[datetime]]:
    """Return (participation_map, refreshed_at) or (None, None)."""
    try:
        doc = await db[worker_config.AI_ELIGIBILITY_SNAPSHOT_COLLECTION].find_one(
            {"_id": worker_config.AI_ELIGIBILITY_SNAPSHOT_ID}
        )
    except Exception as exc:
        logger.warning("AI eligibility snapshot read failed: %r", exc)
        return None, None
    if not doc:
        return None, None
    raw = doc.get("participation") or {}
    participation: ParticipationMap = {}
    for key, info in raw.items():
        try:
            participation[int(key)] = dict(info)
        except (TypeError, ValueError):
            continue
    refreshed_at = doc.get("refreshed_at")
    if doc.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        # Legacy/unknown-shape snapshot — not a valid fresh snapshot, but
        # still usable as the stale fallback when the source is down.
        refreshed_at = None
    elif isinstance(refreshed_at, datetime) and refreshed_at.tzinfo is None:
        refreshed_at = refreshed_at.replace(tzinfo=UTC)
    return participation, refreshed_at


async def write_snapshot(db: Any, participation: ParticipationMap) -> None:
    """Persist a full participation map (best effort)."""
    try:
        await db[worker_config.AI_ELIGIBILITY_SNAPSHOT_COLLECTION].replace_one(
            {"_id": worker_config.AI_ELIGIBILITY_SNAPSHOT_ID},
            {
                "_id": worker_config.AI_ELIGIBILITY_SNAPSHOT_ID,
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "participation": {
                    str(dept_id): info for dept_id, info in participation.items()
                },
                "refreshed_at": datetime.now(UTC),
            },
            upsert=True,
        )
    except Exception as exc:
        logger.warning("AI eligibility snapshot write failed: %r", exc)


def _is_fresh(refreshed_at: Optional[datetime]) -> bool:
    if refreshed_at is None:
        return False
    ttl = timedelta(seconds=worker_config.AI_ELIGIBILITY_SNAPSHOT_TTL_SECONDS)
    return datetime.now(UTC) - refreshed_at < ttl


async def resolve_participation(
    ai_db: Any,
    db: Any,
    *,
    fetch: ParticipationFetcher,
    prefer_snapshot: bool,
) -> Tuple[Optional[ParticipationMap], str]:
    """Return (participation_map, source_status).

    ``fetch(ai_db, department_ids)`` reads the operational source (normally
    ``ai_adoption_service.get_ai_participation_map``).

    ``prefer_snapshot`` (projection mode) serves a fresh snapshot without
    touching ``ai_db``. Otherwise — or when the snapshot is missing or
    expired — the operational source is read for *all* departments and the
    snapshot is rewritten. A failed source read falls back to whatever
    snapshot exists (``stale``) and reports ``unavailable`` only when there
    is nothing to serve.
    """
    snapshot: Optional[ParticipationMap] = None
    refreshed_at: Optional[datetime] = None
    if db is not None:
        snapshot, refreshed_at = await read_snapshot(db)
        if prefer_snapshot and snapshot is not None and _is_fresh(refreshed_at):
            return snapshot, STATUS_AVAILABLE

    try:
        live = await fetch(ai_db, None)
    except Exception as exc:
        logger.error("AI participation lookup failed: %r", exc)
        live = None

    if live is not None:
        if db is not None:
            await write_snapshot(db, live)
        return live, STATUS_AVAILABLE

    if snapshot is not None:
        logger.warning(
            "AI analytics: department AI configuration unavailable; serving "
            "eligibility snapshot from %s.",
            refreshed_at.isoformat() if refreshed_at else "unknown time",
        )
        return snapshot, STATUS_STALE
    return None, STATUS_UNAVAILABLE
