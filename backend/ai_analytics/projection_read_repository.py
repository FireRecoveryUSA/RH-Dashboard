"""Read-side adapter for the AI Analytics Worker projection (Phases 9-10).

When ``settings.AI_ANALYTICS_USE_PROJECTION`` is true, the analytics services
read AI-side fields from the worker's ``ai_invoice_analytics`` projection
collection (in the dashboard-owned Mongo) instead of issuing a per-request
``$in`` query against the operational RecoveryHub_AI Mongo cluster.

This module is the thin adapter between the projection's field names
(Section 9 of the Phase 0 plan) and the field names that
``normalization_core.build_normalized_record`` reads from a raw
``ai_line_items`` document. By mapping projection → ai_record shape, the
existing ``build_normalized_record`` is reused unchanged — the SQL-side
join (business_outcome, cancellation, process_logs) stays identical, and
only the AI-Mongo source is swapped.

Phase 10 additions:
-- ``projection_to_trace_data`` maps the projection to the full field
   shape that ``invoice_trace_service`` needs (including ``line_items``
   with nested ``resources``, ``review_msg``, timestamps,
   ``conversation_id``, ``thread_id_is_billable``). This eliminates the
   ``ai_line_items`` cross-cluster read for the Invoice Trace endpoint.
-- ``get_projection_for_trace`` fetches a single projection by claim_id
   and returns the trace data dict (or ``None`` if no projection).
-- ``aggregate_agent_stats_from_projections`` aggregates per-conversation
   summaries from the projection collection for ``/diagnostics/agents``,
   eliminating the batch cross-cluster read on the conversations
   collection.

Source: ``ai_invoice_analytics`` collection in the dashboard-owned Mongo
(``db_manager.db``, NOT ``db_manager.ai_db``).
Destination: none (read-only).
Architectural constraints:
-- Never reads from RecoveryHub_AI Mongo (the projection is the cache).
-- Returns dicts shaped like raw ``ai_line_items`` documents so
   ``build_normalized_record`` works without modification.
-- Claims with no projection are simply absent from the result dict —
   ``build_normalized_record`` handles ``ai_record=None`` gracefully.
-- ``thread_id`` and ``retry_thread_id`` are not stored in the projection
   (Section 9 does not include them). They are 0% populated in production
   per the Phase 0 audit, so their absence is semantically identical to
   the direct-read path returning ``None`` for them. ``retry_count`` IS
   stored (Section 9.6, 30% populated) and is passed through unchanged.
-- v1 projections (pre-Phase 10) lack ``conversation_summaries``,
   ``resources`` in line items, ``conversation_id``, and
   ``thread_id_is_billable``. The trace adapter returns ``None`` for
   these — the caller falls back to the direct-read path. v2 projections
   carry them (Section 9.12 lazy upgrade).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from ai_analytics_worker.config import worker_config
from .fee_schedule import STEP1_SOURCE_FIELDS

logger = logging.getLogger(__name__)


# Projection field name → raw ai_line_items field name that
# ``build_normalized_record`` reads. Fields with the same name in both
# shapes (see ``_PASSTHROUGH_FIELDS``) don't need a mapping entry — the
# adapter copies them through directly.
_FIELD_MAP: Dict[str, str] = {
    # projection field → ai_record field name
    "ai_processing_status": "claim_processing_status",
    "agent_execution_status": "agent_exec_status",
    "ai_invoice_total": "invoice_total",
    "processing_duration_seconds": "processing_time_seconds",
}

# Fields that exist in the raw ai_line_items doc but are NOT in the
# projection. All are 0% populated in production per the Phase 0 audit,
# so their absence from the projection is semantically identical to the
# direct-read path returning None. Listed here so the adapter can set
# them explicitly to None rather than omitting them — this makes the
# adapted dict a complete stand-in for a raw ai_record.
_MISSING_FROM_PROJECTION: tuple[str, ...] = (
    "thread_id",
    "retry_thread_id",
)

# Fields that pass through unchanged (same name in both shapes). Listed
# explicitly so the adapter is self-documenting and a future schema
# change is caught here rather than silently breaking the cohort.
#
# ``retry_count`` is included here (not in ``_MISSING_FROM_PROJECTION``)
# because the projection DOES store it (Section 9.6) and it is 30%
# populated in production per the Phase 0 audit. The Phase 0 audit also
# found ``thread_id`` / ``retry_thread_id`` are 0% populated — those go
# in ``_MISSING_FROM_PROJECTION`` so they are explicitly None.
_PASSTHROUGH_FIELDS: tuple[str, ...] = (
    "confidence_level",
    "is_billable",
    "billing_category",
    "line_items_save_to_rh_status",
    "retry_count",
    # Schema v3: routing/cancellation fields. Schema v4: step-1
    # category/fee-item identification + intake evidence
    # (fee_schedule.STEP1_SOURCE_FIELDS). Older projections lack these
    # keys — .get() returns None, matching the direct-read path when the
    # source doc doesn't have them.
    *STEP1_SOURCE_FIELDS,
    "dept_send_auto_invoice_status",
    "is_cancelled",
    "cancellation_reason",
)


def projection_to_ai_record(
    projection: Dict[str, Any],
) -> Dict[str, Any]:
    """Map a projection document to the raw ``ai_line_items`` field shape.

    ``build_normalized_record`` reads specific fields from the
    ``ai_record`` argument (e.g. ``claim_processing_status``,
    ``agent_exec_status``, ``confidence_level``). The projection stores
    some of these under different names (e.g. ``ai_processing_status``
    instead of ``claim_processing_status``). This function renames them
    so the projection can be passed to ``build_normalized_record`` as a
    drop-in replacement for a raw ``ai_line_items`` document.

    Fields not in the projection (``thread_id``, ``retry_thread_id``)
    are set to ``None`` — they are 0% populated in production per the
    Phase 0 audit, so this matches the direct-read path's behaviour.

    Arguments:
        projection: a document from the ``ai_invoice_analytics``
            collection.

    Returns:
        A dict with the field names that ``build_normalized_record``
        expects from a raw ``ai_line_items`` document.
    """
    adapted: Dict[str, Any] = {}

    # Passthrough fields (same name in both shapes).
    for field in _PASSTHROUGH_FIELDS:
        adapted[field] = projection.get(field)

    # Renamed fields.
    for proj_field, ai_field in _FIELD_MAP.items():
        adapted[ai_field] = projection.get(proj_field)

    # Fields not in the projection — explicit None for completeness.
    for field in _MISSING_FROM_PROJECTION:
        adapted[field] = None

    # Internal (not part of the raw ai_line_items shape): the projection's
    # own schema version, so the cohort loader can detect outdated
    # projections and refresh them from the source. Missing → 0.
    adapted["_projection_schema_version"] = projection.get(
        "projection_schema_version", 0
    )

    return adapted


async def get_projection_records_for_claim_ids(
    db: Any,
    claim_ids: List[int],
    absent_claim_ids: Optional[Set[int]] = None,
) -> Dict[int, Dict[str, Any]]:
    """Fetch projection documents for a batch of claim IDs.

    Returns a dict keyed by ``claim_id`` → adapted ai_record dict (ready
    to pass to ``build_normalized_record``). Claims with no projection,
    or whose projection records that no ``ai_line_items`` document exists
    (``has_ai_line_item_record=false``), are absent from the result so
    they normalize as ``ai_record_state="missing"`` exactly like the
    direct-read path — ``build_normalized_record`` handles
    ``ai_record=None`` gracefully, so callers iterate the SQL cohort and
    look up each claim_id in this dict, same as the direct-read path.

    Arguments:
        db: the dashboard-owned Motor database handle (``db_manager.db``,
            NOT ``db_manager.ai_db``).
        claim_ids: the claim IDs to fetch projections for.
        absent_claim_ids: optional caller-provided set that is populated
            with the claim IDs whose *current-schema* projection
            explicitly records ``has_ai_line_item_record=false``. Lets
            callers distinguish "no projection / stale projection"
            (refresh from source) from "authoritative negative
            projection" (keep ``missing``; the source read would return
            nothing anyway). Stale negative projections are not added —
            they still warrant a source refresh.

    Returns:
        Dict mapping ``claim_id`` (int) → adapted ai_record dict.
    """
    if not claim_ids:
        return {}

    collection = db[worker_config.PROJECTIONS_COLLECTION]

    try:
        cursor = collection.find(
            {"_id": {"$in": claim_ids}},
        )
        docs = await cursor.to_list(length=len(claim_ids))
    except Exception as e:
        logger.error(
            "Projection read failed for %d claim_ids: %s", len(claim_ids), e
        )
        raise

    by_claim: Dict[int, Dict[str, Any]] = {}
    for doc in docs:
        # The projection's _id is the integer claim_id (Section 9.1).
        claim_id = doc.get("_id")
        if claim_id is None:
            continue
        if not doc.get("has_ai_line_item_record", True):
            if (
                absent_claim_ids is not None
                and doc.get("projection_schema_version", 0)
                >= worker_config.projection_schema_version
            ):
                try:
                    absent_claim_ids.add(int(claim_id))
                except (ValueError, TypeError):
                    pass
            continue
        try:
            cid = int(claim_id)
        except (ValueError, TypeError):
            continue
        by_claim[cid] = projection_to_ai_record(doc)

    logger.info(
        "Projection read: fetched %d/%d projections from ai_invoice_analytics.",
        len(by_claim),
        len(claim_ids),
    )
    return by_claim


# ---------------------------------------------------------------------------
# Phase 10: Invoice Trace read path
# ---------------------------------------------------------------------------

# Additional field mappings for the Invoice Trace endpoint (Phase 10).
# These map projection field names → raw ai_line_items field names that
# ``invoice_trace_service`` reads. Includes the Phase 9 mappings plus
# fields only the trace endpoint needs.
_TRACE_FIELD_MAP: Dict[str, str] = {
    # Phase 9 mappings (also needed by build_normalized_record)
    "ai_processing_status": "claim_processing_status",
    "agent_execution_status": "agent_exec_status",
    "ai_invoice_total": "invoice_total",
    "processing_duration_seconds": "processing_time_seconds",
    # Phase 10 mappings (trace-only fields)
    "review_message": "review_msg",
    "ai_inserted_at": "inserted_at",
    "ai_updated_at": "updated_at",
    "ai_completed_at": "completed_at",
}

_TRACE_PASSTHROUGH_FIELDS: tuple[str, ...] = (
    "confidence_level",
    "is_billable",
    "billing_category",
    "line_items_save_to_rh_status",
    "retry_count",
    "incident_duration_in_minutes",
    # Phase 10 new fields (v2 projections only; v1 returns None)
    "conversation_id",
    "thread_id_is_billable",
    # Schema v3/v4: step-1 identification + intake evidence fields
    # (fee_schedule.STEP1_SOURCE_FIELDS).
    *STEP1_SOURCE_FIELDS,
)

# The projection stores line items under ``ai_line_items``; the trace
# endpoint reads ``line_items`` from the raw ai_record.
_TRACE_RENAMED_LIST_FIELDS: Dict[str, str] = {
    "ai_line_items": "line_items",
}


def projection_to_trace_data(
    projection: Dict[str, Any],
) -> Dict[str, Any]:
    """Map a projection document to the full field shape for Invoice Trace.

    Unlike ``projection_to_ai_record`` (which only maps the fields
    ``build_normalized_record`` reads), this function maps ALL fields
    that ``invoice_trace_service.get_invoice_trace`` reads from the raw
    ``ai_line_items`` document — including ``line_items`` with nested
    ``resources``, ``review_msg``, timestamps, ``conversation_id``, and
    ``thread_id_is_billable``.

    Also extracts ``conversation_summaries`` (v2 projections only) so
    the trace endpoint can show the conversation list without a
    cross-cluster read. The full conversation detail fields
    (input_data, incident_json, results, output_data) are NOT in the
    projection — the trace endpoint still reads those from
    RecoveryHub_AI Mongo.

    Arguments:
        projection: a document from the ``ai_invoice_analytics``
            collection.

    Returns:
        A dict with the field names that ``invoice_trace_service``
        expects from a raw ``ai_line_items`` document, plus a
        ``conversation_summaries`` key.
    """
    adapted: Dict[str, Any] = {}

    # Passthrough fields (same name in both shapes).
    for field in _TRACE_PASSTHROUGH_FIELDS:
        adapted[field] = projection.get(field)

    # Renamed fields.
    for proj_field, ai_field in _TRACE_FIELD_MAP.items():
        adapted[ai_field] = projection.get(proj_field)

    # Renamed list fields (projection name → raw name).
    for proj_field, ai_field in _TRACE_RENAMED_LIST_FIELDS.items():
        adapted[ai_field] = projection.get(proj_field)

    # Fields not in the projection — explicit None for completeness.
    for field in _MISSING_FROM_PROJECTION:
        adapted[field] = None

    # Phase 10 metadata consumed by invoice_trace_service. Keep these
    # separate from the raw ai_line_items shape; the service removes them
    # before exposing raw_ai_record.
    adapted["has_ai_line_item_record"] = projection.get(
        "has_ai_line_item_record", True
    )
    adapted["conversation_summaries"] = projection.get("conversation_summaries") or []

    return adapted


async def get_projection_for_trace(
    db: Any,
    claim_id: int,
) -> Optional[Dict[str, Any]]:
    """Fetch a single projection for the Invoice Trace endpoint.

    Returns the adapted trace data dict (ready to pass to
    ``invoice_trace_service``), or ``None`` if no projection exists for
    the claim. When ``None``, the trace endpoint falls back to the
    direct-read path (RecoveryHub_AI Mongo).

    Arguments:
        db: the dashboard-owned Motor database handle (``db_manager.db``).
        claim_id: the claim ID to fetch the projection for.

    Returns:
        Adapted trace data dict, or ``None``.
    """
    collection = db[worker_config.PROJECTIONS_COLLECTION]

    try:
        doc = await collection.find_one({"_id": claim_id})
    except Exception as e:
        logger.error("Projection trace read failed for claim_id=%s: %s", claim_id, e)
        raise

    if doc is None:
        return None

    return projection_to_trace_data(doc)


# ---------------------------------------------------------------------------
# Phase 10: /diagnostics/agents aggregation from projection
# ---------------------------------------------------------------------------


async def aggregate_agent_stats_from_projections(
    db: Any,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    claim_ids: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    """Aggregate agent stats from projection conversation_summaries.

    Replaces the direct MongoDB aggregation on ``ai_agent_conversations``
    with an aggregation on the ``ai_invoice_analytics`` projection
    collection. Uses ``$unwind`` on ``conversation_summaries`` and
    groups by ``(agent, status, processing_stage, request_type)``.

    v1 projections (no ``conversation_summaries``) contribute nothing —
    their ``conversation_summaries`` field is absent or empty, so
    ``$unwind`` produces no documents for them. This is the lazy-upgrade
    behaviour: stats are incomplete until all projections are refreshed
    to v2, but never incorrect.

    Arguments:
        db: the dashboard-owned Motor database handle (``db_manager.db``).
        start_date: optional ISO date filter on
            ``conversation_summaries.created_at`` (inclusive).
        end_date: optional ISO date filter (exclusive — the caller
            adds one day).
        claim_ids: optional list of claim IDs to restrict the aggregation
            to. The projection ``_id`` is the claim ID.

    Returns:
        List of dicts with keys ``agent``, ``status``,
        ``processing_stage``, ``request_type``, ``count`` — same shape
        as the MongoDB aggregation result from the direct-read path.
    """
    from datetime import datetime as _dt, timedelta as _td

    collection = db[worker_config.PROJECTIONS_COLLECTION]

    # Build the date filter for conversation_summaries.created_at.
    # We use $elemMatch on the array to pre-filter projections that
    # have at least one matching conversation, then $unwind and
    # $match again to filter individual conversations.
    match_stage: Dict[str, Any] = {}
    if start_date or end_date:
        date_filter: Dict[str, Any] = {}
        if start_date:
            date_filter["$gte"] = start_date
        if end_date:
            try:
                end_exclusive = (
                    _dt.fromisoformat(end_date) + _td(days=1)
                ).date().isoformat()
            except ValueError:
                end_exclusive = end_date
            date_filter["$lt"] = end_exclusive
        match_stage["conversation_summaries.created_at"] = date_filter

    pipeline: List[Dict[str, Any]] = [
        # Pre-filter: only projections that have conversation_summaries
        # matching the date range (skips v1 projections entirely).
        {"$match": {"conversation_summaries": {"$ne": [], "$exists": True}}},
    ]
    if claim_ids is not None:
        pipeline.append({"$match": {"_id": {"$in": claim_ids}}})
    if match_stage:
        pipeline.append({"$match": match_stage})

    pipeline.extend([
        # Unwind the conversation_summaries array.
        {"$unwind": "$conversation_summaries"},
        # Filter individual conversations by date (post-unwind).
        {"$match": match_stage} if match_stage else {"$match": {}},
        # Group by (agent, status, processing_stage, request_type).
        {
            "$group": {
                "_id": {
                    "agent": "$conversation_summaries.agent",
                    "status": "$conversation_summaries.status",
                    "processing_stage": "$conversation_summaries.processing_stage",
                    "request_type": "$conversation_summaries.request_type",
                },
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"count": -1}},
    ])

    try:
        cursor = collection.aggregate(pipeline)
        results = await cursor.to_list(length=1000)
    except Exception as e:
        logger.error("Projection agent stats aggregation failed: %s", e)
        raise

    # Convert to the same shape as the direct-read path.
    stats: List[Dict[str, Any]] = []
    for r in results:
        group = r.get("_id", {})
        stats.append({
            "agent": group.get("agent") or "unknown",
            "status": group.get("status") or "unknown",
            "processing_stage": group.get("processing_stage") or "unknown",
            "request_type": group.get("request_type") or "unknown",
            "count": r.get("count", 0),
        })
    return stats
