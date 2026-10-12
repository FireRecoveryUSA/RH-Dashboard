"""AI Analytics Worker — projection builder (Phase 3).

Builds a deterministic ``ai_invoice_analytics`` projection document from a
single claim's source data (one ``ai_line_items`` document and its matching
``ai_agent_conversations`` documents). The projection conforms to the frozen
schema in ``docs/ai-analytics/PHASE_0_IMPLEMENTATION_PLAN.md`` Section 9
(APPROVED 2026-08-13).

The builder is a **pure function** — no I/O, no async, no side effects. It
takes already-fetched source documents and returns the projection dict. This
makes it fully unit-testable and keeps the I/O concerns in
``source_repository.py`` and ``projection_repository.py``.

DRY rule (binding — Section 1.2.4 of the Phase 0 plan):
- Reuses ``classify_writeback_status()``, ``calculate_retry_count()``,
  ``confidence_bucket()``, and ``classify_billability()`` from
  ``normalization_core.py``. Does NOT reimplement them.
- Does NOT call ``build_normalized_record()`` because that function joins SQL
  + Mongo data; the worker only has Mongo data. The worker's projection is a
  different shape (the ``ai_invoice_analytics`` collection), and the shared
  *functions* are the DRY boundary, not the record builder.

Source: ``ai_line_items`` document + ``ai_agent_conversations`` documents
(read-only, fetched by ``source_repository.py``).
Destination: the returned dict is persisted by ``projection_repository.py``
in a later phase.
Architectural constraints:
- Deterministic: same inputs → same output (no clock reads; the caller passes
  ``worker_processed_at``).
- Never raises on missing/malformed source data — sets data-quality flags
  instead. A malformed claim must never stop the worker.
- Sets ``projection_schema_version`` from ``worker_config`` so the dashboard
  can handle multiple schema versions gracefully (Section 9.12).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from ai_analytics.fee_schedule import step1_source_fields
from ai_analytics.normalization_core import (
    calculate_retry_count,
    classify_billability,
    classify_writeback_status,
    confidence_bucket,
)

from .config import worker_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data-quality flag values (Section 9.11 — stable, do not rename)
# ---------------------------------------------------------------------------

FLAG_MISSING_AI_LINE_ITEM = "MISSING_AI_LINE_ITEM"
FLAG_MISSING_CLAIM_ID = "MISSING_CLAIM_ID"
FLAG_MULTIPLE_AI_RECORDS_FOR_CLAIM = "MULTIPLE_AI_RECORDS_FOR_CLAIM"
FLAG_MISSING_CONVERSATIONS = "MISSING_CONVERSATIONS"
FLAG_INVALID_CONFIDENCE_VALUE = "INVALID_CONFIDENCE_VALUE"
FLAG_MISSING_UPDATED_TIMESTAMP = "MISSING_UPDATED_TIMESTAMP"
FLAG_RETRY_THREAD_WITHOUT_MAIN_THREAD = "RETRY_THREAD_WITHOUT_MAIN_THREAD"
FLAG_WRITEBACK_STATE_INCONSISTENT = "WRITEBACK_STATE_INCONSISTENT"

# Confidence validation bounds (Phase 0: int 0–100, stored as float).
_CONFIDENCE_MIN = 0.0
_CONFIDENCE_MAX = 100.0

# claim_processing_status values that mean the AI is still working — if
# writeback is already True, that's inconsistent.
_IN_PROGRESS_PROCESSING_STATUSES = {"INITIATED", "IN_PROGRESS"}

# Fields extracted from each ai_line_items.line_items entry for the summary.
# Section 9.8: "Direct copy (summary: item, description, quantity, rate,
# line_item_total)". Phase 10 adds ``resources`` so the Invoice Trace
# endpoint can display nested resources without a cross-cluster read.
_LINE_ITEM_SUMMARY_FIELDS = (
    "item",
    "description",
    "quantity",
    "rate",
    "line_item_total",
    "resources",
)

# Fields extracted from each ai_agent_conversations document for the
# per-conversation summary (Phase 10). Excludes the large payload fields
# (input_data, incident_json, results, output_data) — those stay in the
# source and are fetched on demand by the Invoice Trace endpoint.
_CONVERSATION_SUMMARY_FIELDS = (
    "agent",
    "status",
    "created_at",
    "processing_stage",
    "request_type",
    "execution_time_seconds",
)


def build_projection(
    claim_id: Optional[int],
    ai_line_items: Optional[Dict[str, Any]],
    conversations: List[Dict[str, Any]],
    worker_processed_at: datetime,
) -> Optional[Dict[str, Any]]:
    """Build an ``ai_invoice_analytics`` projection document.

    Arguments:
        claim_id: the claim ID this projection is for. May be ``None`` if the
            caller (change-stream listener) could not extract one — in that
            case the projection carries the ``MISSING_CLAIM_ID`` flag and a
            ``None`` ``_id`` (the caller decides whether to persist it).
        ai_line_items: the source ``ai_line_items`` document (full projection
            from ``mongo_repository.get_ai_line_items_for_claim``), or ``None``
            if no source document was found.
        conversations: the list of ``ai_agent_conversations`` documents for
            this claim (from ``mongo_repository.get_agent_conversations_for_claim``),
            sorted chronologically. May be empty.
        worker_processed_at: the timestamp at which the worker is building
            this projection. Passed in (not read from the clock) so tests are
            deterministic.

    Returns:
        The projection dict conforming to Section 9, or ``None`` if
        ``claim_id`` is ``None`` and there is no source document — in which
        case there is nothing to project (the caller dead-letters the event).

    Raises:
        Never. Malformed source data is captured in ``data_quality_flags``
        rather than raising, so a single bad claim never stops the worker.
    """
    # --- Nothing to project if we have no claim_id AND no source record ----
    if claim_id is None and ai_line_items is None:
        return None

    # Derive claim_id from the source document if the caller didn't pass one
    # (defensive — the change-stream listener should always extract it).
    if claim_id is None and ai_line_items is not None:
        raw = ai_line_items.get("claim_id")
        if raw is not None:
            try:
                claim_id = int(raw)
            except (ValueError, TypeError):
                claim_id = None

    has_ai_record = ai_line_items is not None
    has_conversations = len(conversations) > 0

    # --- Data-quality flags (Section 9.11) -------------------------------
    data_quality_flags: List[str] = []
    if claim_id is None:
        data_quality_flags.append(FLAG_MISSING_CLAIM_ID)
    if not has_ai_record:
        data_quality_flags.append(FLAG_MISSING_AI_LINE_ITEM)
    if not has_conversations:
        data_quality_flags.append(FLAG_MISSING_CONVERSATIONS)

    # Fields that need the source document — default to None when missing.
    confidence = ai_line_items.get("confidence_level") if has_ai_record else None
    updated_at = ai_line_items.get("updated_at") if has_ai_record else None
    thread_id = ai_line_items.get("thread_id") if has_ai_record else None
    retry_thread_id = ai_line_items.get("retry_thread_id") if has_ai_record else None
    claim_processing_status = (
        ai_line_items.get("claim_processing_status") if has_ai_record else None
    )
    agent_exec_status = (
        ai_line_items.get("agent_exec_status") if has_ai_record else None
    )
    writeback_raw = (
        ai_line_items.get("line_items_save_to_rh_status") if has_ai_record else None
    )
    billing_category = (
        ai_line_items.get("billing_category") if has_ai_record else None
    )
    is_billable = ai_line_items.get("is_billable") if has_ai_record else None
    is_billable_not_determined = (
        ai_line_items.get("is_billable_not_determined") if has_ai_record else None
    )

    # Confidence range validation
    if confidence is not None:
        try:
            conf_val = float(confidence)
            if not (_CONFIDENCE_MIN <= conf_val <= _CONFIDENCE_MAX):
                data_quality_flags.append(FLAG_INVALID_CONFIDENCE_VALUE)
        except (ValueError, TypeError):
            data_quality_flags.append(FLAG_INVALID_CONFIDENCE_VALUE)

    # Missing updated_at timestamp
    if has_ai_record and updated_at is None:
        data_quality_flags.append(FLAG_MISSING_UPDATED_TIMESTAMP)

    # Retry thread without main thread (Phase 0: both 0% populated — defensive)
    if retry_thread_id and not thread_id:
        data_quality_flags.append(FLAG_RETRY_THREAD_WITHOUT_MAIN_THREAD)

    # --- Derived normalization (DRY — reuse normalization_core.py) --------
    writeback_state = classify_writeback_status(writeback_raw, claim_processing_status)

    # Writeback inconsistency: writeback=True but processing still in progress.
    if (
        writeback_raw is True
        and claim_processing_status in _IN_PROGRESS_PROCESSING_STATUSES
    ):
        data_quality_flags.append(FLAG_WRITEBACK_STATE_INCONSISTENT)

    retry_count = calculate_retry_count(
        ai_record=ai_line_items,
        retry_thread_id=retry_thread_id,
        agent_exec_status=agent_exec_status,
    )
    retry_evidence = _calculate_retry_evidence(
        ai_line_items, retry_thread_id, agent_exec_status
    )

    billability = classify_billability(
        billing_category=billing_category,
        is_billable=is_billable,
        is_billable_not_determined=is_billable_not_determined,
    )

    conf_bucket = confidence_bucket(confidence)

    # --- Conversation summary (Section 9.7) ------------------------------
    conversation_summary = _summarize_conversations(conversations)

    # --- Per-conversation summaries (Phase 10) ---------------------------
    # Store per-conversation summary dicts so /diagnostics/agents can
    # aggregate from the projection and the Invoice Trace endpoint can
    # show the conversation list without a cross-cluster read. Excludes
    # the large payload fields (input_data, incident_json, results,
    # output_data) — those stay in the source and are fetched on demand.
    conversation_summaries = _summarize_conversation_details(conversations)

    # --- Line items summary (Section 9.8) --------------------------------
    raw_line_items = ai_line_items.get("line_items") if has_ai_record else None
    line_items_summary = _summarize_line_items(raw_line_items)

    # --- Source tracking (Section 9.2) -----------------------------------
    source_ai_line_item_ids: List[Any] = []
    if has_ai_record and ai_line_items.get("_id") is not None:
        source_ai_line_item_ids.append(ai_line_items["_id"])

    source_conversation_ids: List[Any] = [
        c["_id"] for c in conversations if c.get("_id") is not None
    ]

    source_latest_updated_at = updated_at  # single doc → its updated_at is the max
    # If conversations have a newer updated_at, use that. Conversations use
    # created_at (no updated_at field per Phase 0), so the ai_line_items
    # updated_at is the authoritative freshness signal.
    # NOTE: if multiple ai_line_items docs are ever supported, take the max
    # across all of them. For now, single doc → direct copy.

    # --- Identity (Section 9.1) — defensive reads from the source doc -----
    department_id = ai_line_items.get("department_id") if has_ai_record else None
    department_name = ai_line_items.get("department_name") if has_ai_record else None
    run_number = ai_line_items.get("run_number") if has_ai_record else None
    draft_claim_id = ai_line_items.get("draft_claim_id") if has_ai_record else None

    projection: Dict[str, Any] = {
        # 9.1 Identity — _id is claim_id (int), NOT the ObjectId of the source.
        # _id may be None if claim_id derivation failed (the source doc had
        # no/invalid claim_id). In that case the MISSING_CLAIM_ID flag is set
        # and the caller (projection_repository) must decide whether to
        # persist — MongoDB rejects _id=None on insert.
        "_id": claim_id,
        "claim_id": claim_id,
        "department_id": department_id,
        "department_name": department_name,
        "run_number": run_number,
        "draft_claim_id": draft_claim_id,
        # 9.2 Source tracking
        "source_ai_line_item_ids": source_ai_line_item_ids,
        "source_conversation_ids": source_conversation_ids,
        "source_latest_updated_at": source_latest_updated_at,
        "worker_processed_at": worker_processed_at,
        "worker_version": worker_config.worker_version,
        "projection_schema_version": worker_config.projection_schema_version,
        # 9.3 AI processing
        "ai_processing_status": claim_processing_status,
        "agent_execution_status": agent_exec_status,
        "ai_inserted_at": ai_line_items.get("inserted_at") if has_ai_record else None,
        "ai_updated_at": updated_at,
        "ai_completed_at": ai_line_items.get("completed_at") if has_ai_record else None,
        "processing_duration_seconds": (
            ai_line_items.get("processing_time_seconds") if has_ai_record else None
        ),
        # 9.4 Incident / billability
        "is_billable": is_billable,
        "is_billable_not_determined": is_billable_not_determined,
        "billability_state": billability,
        "billing_category": billing_category,
        # Step-1 category/fee-item identification output + intake evidence
        # (present on source docs from ~2026-09+). Schema v4: passthrough of
        # fee_schedule.STEP1_SOURCE_FIELDS so the outcomes funnel can match
        # results against the department fee catalog on either read path.
        **step1_source_fields(ai_line_items if has_ai_record else None),
        # AI routing mode (mirrors Departments.IsSendInvoiceAI) and
        # AI-side cancellation fields on newer source docs.
        "dept_send_auto_invoice_status": (
            ai_line_items.get("dept_send_auto_invoice_status")
            if has_ai_record else None
        ),
        "is_cancelled": (
            ai_line_items.get("is_cancelled") if has_ai_record else None
        ),
        "cancellation_reason": (
            ai_line_items.get("cancellation_reason") if has_ai_record else None
        ),
        "incident_duration_in_minutes": (
            ai_line_items.get("incident_duration_in_minutes") if has_ai_record else None
        ),
        # 9.5 Quality
        "confidence_level": confidence,
        "confidence_bucket": conf_bucket,
        "review_message": ai_line_items.get("review_msg") if has_ai_record else None,
        # 9.6 Retry
        "has_retry": retry_count > 0,
        "retry_count": retry_count,
        "retry_evidence": retry_evidence,
        # 9.7 Conversation summary
        "conversation_count": conversation_summary["conversation_count"],
        "agent_count": conversation_summary["agent_count"],
        "agents": conversation_summary["agents"],
        "processing_stages": conversation_summary["processing_stages"],
        "failed_conversation_count": conversation_summary["failed_conversation_count"],
        "successful_conversation_count": conversation_summary[
            "successful_conversation_count"
        ],
        "conversation_duration_total_seconds": conversation_summary[
            "conversation_duration_total_seconds"
        ],
        # Phase 10: per-conversation summaries for /diagnostics/agents
        # aggregation and Invoice Trace conversation list. Excludes large
        # payload fields (input_data, incident_json, results, output_data).
        "conversation_summaries": conversation_summaries,
        # 9.8 Line items
        "ai_line_item_count": len(line_items_summary),
        "ai_invoice_total": ai_line_items.get("invoice_total") if has_ai_record else None,
        "ai_line_items": line_items_summary,
        # 9.9 RecoveryHub writeback
        "line_items_save_to_rh_status": writeback_raw,
        "writeback_state": writeback_state,
        # 9.10 Data-quality flags
        "has_ai_line_item_record": has_ai_record,
        "has_conversation_records": has_conversations,
        # multiple_ai_records and source_record_count are defensive — the
        # current source_repository returns a single doc. If a future change
        # fetches multiple, these fields become meaningful.
        "multiple_ai_records": False,
        "source_record_count": 1 if has_ai_record else 0,
        "data_quality_flags": data_quality_flags,
        # Phase 10: raw fields needed by Invoice Trace that weren't in
        # the v1 schema. ``conversation_id`` is the linking ID on the
        # ai_line_items doc (NOT the conversation doc's _id).
        # ``thread_id_is_billable`` is 0% populated per Phase 0 but kept
        # for forward compatibility with the trace endpoint.
        "conversation_id": (
            ai_line_items.get("conversation_id") if has_ai_record else None
        ),
        "thread_id_is_billable": (
            ai_line_items.get("thread_id_is_billable") if has_ai_record else None
        ),
    }

    logger.debug(
        "Built projection for claim_id=%s (flags=%s, worker_version=%s).",
        claim_id,
        data_quality_flags or "none",
        worker_config.worker_version,
    )
    return projection


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _calculate_retry_evidence(
    ai_record: Optional[Dict[str, Any]],
    retry_thread_id: Optional[str],
    agent_exec_status: Optional[str],
) -> List[str]:
    """Return the list of evidence sources that indicated a retry.

    Mirrors the sources checked by ``normalization_core.calculate_retry_count``
    so the evidence list is consistent with the count. The evidence names are
    stable projection field values — do not rename them.
    """
    evidence: List[str] = []
    if ai_record and ai_record.get("retry_count") is not None:
        try:
            if int(ai_record["retry_count"]) > 0:
                evidence.append("retry_count_field")
        except (ValueError, TypeError):
            pass
    if retry_thread_id:
        evidence.append("retry_thread_id")
    if agent_exec_status == "retry":
        evidence.append("agent_exec_status")
    return evidence


def _summarize_conversations(
    conversations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Derive the Section 9.7 conversation summary fields.

    Returns a dict with keys: ``conversation_count``, ``agent_count``,
    ``agents``, ``processing_stages``, ``failed_conversation_count``,
    ``successful_conversation_count``, ``conversation_duration_total_seconds``.
    """
    conversation_count = len(conversations)

    agents_set = set()
    stages_set = set()
    failed = 0
    successful = 0
    duration_total = 0.0
    has_any_duration = False

    for conv in conversations:
        agent = conv.get("agent")
        if agent is not None:
            agents_set.add(agent)

        stage = conv.get("processing_stage")
        if stage is not None:
            stages_set.add(stage)

        status = conv.get("status")
        if status == "completed":
            successful += 1
        else:
            # Any non-completed status counts as failed (Section 9.7).
            failed += 1

        duration = conv.get("execution_time_seconds")
        if duration is not None:
            try:
                duration_total += float(duration)
                has_any_duration = True
            except (ValueError, TypeError):
                pass

    return {
        "conversation_count": conversation_count,
        "agent_count": len(agents_set),
        "agents": sorted(agents_set),
        "processing_stages": sorted(stages_set),
        "failed_conversation_count": failed,
        "successful_conversation_count": successful,
        # Phase 0: execution_time_seconds is 0% populated → None when no
        # conversation has a duration. Don't emit 0.0 as a false signal.
        "conversation_duration_total_seconds": duration_total if has_any_duration else None,
    }


def _summarize_line_items(
    raw_line_items: Optional[Any],
) -> List[Dict[str, Any]]:
    """Extract canonical line-item fields for the projection.

    The source uses historical aliases for item name, rate, and total
    (``label``/``name``, ``amount``, and ``total``). Normalize those aliases
    here so projection and direct-read Invoice Trace responses have identical
    comparison behavior. Missing resources become an empty list, matching
    ``_extract_ai_line_items``.
    """
    if not isinstance(raw_line_items, list):
        return []

    summary: List[Dict[str, Any]] = []
    for entry in raw_line_items:
        if not isinstance(entry, dict):
            continue
        summary.append({
            "item": entry.get("item") or entry.get("label") or entry.get("name"),
            "description": entry.get("description"),
            "quantity": entry.get("quantity"),
            "rate": entry.get("rate") if entry.get("rate") is not None else entry.get("amount"),
            "line_item_total": (
                entry.get("line_item_total")
                if entry.get("line_item_total") is not None
                else entry.get("total")
            ),
            "resources": entry.get("resources") if isinstance(entry.get("resources"), list) else [],
        })
    return summary


def _summarize_conversation_details(
    conversations: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Extract per-conversation summary dicts (Phase 10).

    Each entry contains the summary fields that ``/diagnostics/agents``
    needs for aggregation (agent, status, processing_stage, request_type)
    and that the Invoice Trace endpoint needs for the conversation list
    (created_at, execution_time_seconds, conversation_id). The large
    payload fields (input_data, incident_json, results, output_data) are
    NOT included — they stay in the source and are fetched on demand by
    the trace endpoint.

    Conversations are returned in the same order as the input (the
    caller — ``source_repository`` — sorts chronologically by
    ``created_at``).
    """
    summaries: List[Dict[str, Any]] = []
    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        entry: Dict[str, Any] = {
            "conversation_id": str(conv.get("_id", "")) if conv.get("_id") else None,
        }
        for field in _CONVERSATION_SUMMARY_FIELDS:
            value = conv.get(field)
            # Store timestamps as ISO strings so projection-side date
            # filtering is deterministic across BSON and test backends.
            entry[field] = value.isoformat() if field == "created_at" and hasattr(value, "isoformat") else value
        summaries.append(entry)
    return summaries
