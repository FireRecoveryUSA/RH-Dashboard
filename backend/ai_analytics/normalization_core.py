"""Pure normalization / business-mapping functions for AI Analytics.

These functions take raw SQL and Mongo records and produce the normalized
runtime views described in the Phase 0 data contract. They contain no I/O
and are fully unit-testable.

This module is the single source of truth (DRY) for all normalization logic
shared between the direct-read analytics services (``outcome_service.py``,
``diagnostics_service.py``, ``invoice_trace_service.py``) and the AI Analytics
Worker (``backend/ai_analytics_worker/``). Both consumers import from here —
neither reimplements this logic.

Key findings from Phase 0 that drive these mappings:
- Status 4 is terminal for BOTH released and cancelled. Status 5 does not
  exist. The distinction is made via process logs and cancellation records.
- ``is_billable``, ``thread_id``, ``retry_thread_id`` are 0% populated.
  Use ``billing_category`` and ``retry_count`` instead.
- ``processing_time_seconds`` lives on ``ai_line_items``, not on
  ``ai_agent_conversations``.
- ``claim_processing_status`` has additional value ``BILLING_LEVEL_NOT_ENABLED``.
- ``agent_exec_status`` has additional value ``completed_with_issues``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence

from .fee_schedule import step1_source_fields
from .reason_normalization import normalize_reason

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Business outcome classification
# ---------------------------------------------------------------------------

# Process log text values that indicate terminal business actions
RELEASED_LOG_TEXT = "Invoice to Insurance - Released"
CANCELLED_LOG_TEXT = "Invoice to Insurance - Cancelled"

# AI invoice process statuses (from Phase 0 verification)
STATUS_INITIAL = 1       # Ready to Invoice Insurance
STATUS_PROCESSING = 2    # Line item being created / pending
STATUS_TERMINAL = 4      # Terminal — released OR cancelled (check logs)
STATUS_POST_RELEASE = 7  # Post-release (confirm receipt / payment received)
STATUS_ACTIVE_REVIEW = 9 # Line items created, awaiting human action

TERMINAL_STATUSES = {STATUS_TERMINAL, STATUS_POST_RELEASE}
PENDING_STATUSES = {STATUS_INITIAL, STATUS_PROCESSING, STATUS_ACTIVE_REVIEW}


def classify_business_outcome(
    ai_inv_process_status: Optional[int],
    has_released_log: bool = False,
    has_cancelled_log: bool = False,
    has_cancellation_record: bool = False,
) -> str:
    """Classify the business outcome of an AI invoice.

    Returns one of: ``released``, ``cancelled_rejected``, ``pending``,
    ``unknown``.

    Classification priority:
    1. Cancellation record or cancelled log → cancelled_rejected
    2. Released log (and no cancellation) → released
    3. Pending statuses (1, 2, 9) → pending
    4. Anything else → unknown
    """
    # Cancelled takes priority — a cancellation record is definitive
    if has_cancellation_record or has_cancelled_log:
        return "cancelled_rejected"

    # Released: has released log and no cancellation evidence
    if has_released_log:
        return "released"

    # Pending: known non-terminal statuses
    if ai_inv_process_status in PENDING_STATUSES:
        return "pending"

    # Status 4 without explicit released/cancelled logs — try to infer
    if ai_inv_process_status == STATUS_TERMINAL:
        # Terminal but no logs found — treat as unknown rather than guessing
        return "unknown"

    # Status 7 without released log but it's a post-release state
    if ai_inv_process_status == STATUS_POST_RELEASE:
        return "released"

    return "unknown"


def is_terminal_outcome(outcome: str) -> bool:
    """True if the outcome is a terminal business disposition."""
    return outcome in ("released", "cancelled_rejected")


def calculate_release_rate(released: int, cancelled: int) -> Optional[float]:
    """Release rate = released / (released + cancelled).

    Returns None if denominator is zero (pending excluded from denominator).
    """
    denominator = released + cancelled
    if denominator == 0:
        return None
    return round(released / denominator * 100, 2)


def calculate_rejection_rate(released: int, cancelled: int) -> Optional[float]:
    """Rejection rate = cancelled / (released + cancelled).

    Returns None if denominator is zero.
    """
    denominator = released + cancelled
    if denominator == 0:
        return None
    return round(cancelled / denominator * 100, 2)


# ---------------------------------------------------------------------------
# AI execution outcome classification
# ---------------------------------------------------------------------------

# claim_processing_status values that represent a completed (non-error) state
AI_COMPLETED_STATUSES = {"COMPLETED"}

# claim_processing_status values that represent a non-enabled / non-error state
AI_NOT_ENABLED_STATUSES = {"BILLING_LEVEL_NOT_ENABLED"}

# agent_exec_status values that represent success
AGENT_SUCCESS_STATUSES = {"success", "completed_with_issues"}

# agent_exec_status values that represent failure
AGENT_ERROR_STATUSES = {"error"}

# agent_exec_status values that are still running
AGENT_IN_PROGRESS_STATUSES = {"pending", "in_progress", "retry"}


def classify_ai_execution_outcome(
    claim_processing_status: Optional[str],
    agent_exec_status: Optional[str],
) -> str:
    """Classify the AI execution outcome.

    Returns one of: ``completed``, ``failed``, ``not_enabled``, ``in_progress``,
    ``unknown``.
    """
    if claim_processing_status in AI_COMPLETED_STATUSES:
        if agent_exec_status in AGENT_ERROR_STATUSES:
            return "failed"
        return "completed"

    if claim_processing_status in AI_NOT_ENABLED_STATUSES:
        return "not_enabled"

    if claim_processing_status == "ERROR":
        return "failed"

    if claim_processing_status in ("INITIATED", "IN_PROGRESS"):
        return "in_progress"

    if claim_processing_status == "CANCELLED":
        return "failed"

    # Fall back to agent_exec_status
    if agent_exec_status in AGENT_IN_PROGRESS_STATUSES:
        return "in_progress"
    if agent_exec_status in AGENT_ERROR_STATUSES:
        return "failed"

    return "unknown"


# ---------------------------------------------------------------------------
# Writeback normalization
# ---------------------------------------------------------------------------

def classify_writeback_status(
    line_items_save_to_rh_status: Optional[bool],
    claim_processing_status: Optional[str] = None,
) -> str:
    """Normalize the writeback status.

    Returns one of: ``success``, ``not_required``, ``pending``,
    ``failed_or_not_saved``, ``unknown``.
    """
    if line_items_save_to_rh_status is True:
        return "success"

    if line_items_save_to_rh_status is False:
        # If billing level not enabled, writeback was not required
        if claim_processing_status in AI_NOT_ENABLED_STATUSES:
            return "not_required"
        # If still in progress, writeback may be pending
        if claim_processing_status in ("INITIATED", "IN_PROGRESS"):
            return "pending"
        return "failed_or_not_saved"

    return "unknown"


# ---------------------------------------------------------------------------
# Retry detection
# ---------------------------------------------------------------------------

def calculate_retry_count(
    ai_record: Optional[Dict[str, Any]] = None,
    retry_thread_id: Optional[str] = None,
    agent_exec_status: Optional[str] = None,
) -> int:
    """Calculate retry count from all available evidence.

    Phase 0 found that ``retry_thread_id`` and ``thread_id`` are 0% populated,
    so we rely on the ``retry_count`` field on the ai_line_items document and
    the ``agent_exec_status = 'retry'`` signal.
    """
    count = 0

    # Primary: retry_count field on ai_line_items (discovered in Phase 0)
    if ai_record and ai_record.get("retry_count") is not None:
        try:
            count = max(count, int(ai_record["retry_count"]))
        except (ValueError, TypeError):
            pass

    # Secondary: retry_thread_id presence (forward compatibility)
    if retry_thread_id:
        count = max(count, 1)

    # Tertiary: agent_exec_status = 'retry'
    if agent_exec_status == "retry":
        count = max(count, 1)

    return count


def has_retry(ai_record: Optional[Dict[str, Any]] = None, **kwargs) -> bool:
    """Boolean retry detection for filtering."""
    return calculate_retry_count(ai_record=ai_record, **kwargs) > 0


# ---------------------------------------------------------------------------
# Processing duration
# ---------------------------------------------------------------------------

def calculate_processing_duration(
    ai_record: Optional[Dict[str, Any]] = None,
    processing_time_seconds: Optional[float] = None,
) -> Optional[float]:
    """Extract processing duration in seconds.

    Phase 0 found that ``ai_agent_conversations.execution_time_seconds`` is
    0% populated, but ``ai_line_items.processing_time_seconds`` IS populated.
    """
    if processing_time_seconds is not None:
        return float(processing_time_seconds)

    if ai_record and ai_record.get("processing_time_seconds") is not None:
        try:
            return float(ai_record["processing_time_seconds"])
        except (ValueError, TypeError):
            pass

    return None


def calculate_duration_percentiles(
    durations: Sequence[float],
) -> Dict[str, Optional[float]]:
    """Calculate avg, p50, p90, p95 from a list of durations.

    Returns a dict with keys ``avg``, ``p50``, ``p90``, ``p95``. Values are
    None if the input list is empty.
    """
    if not durations:
        return {"avg": None, "p50": None, "p90": None, "p95": None}

    sorted_durations = sorted(durations)
    n = len(sorted_durations)

    def percentile(p: float) -> float:
        idx = int(round(p / 100 * (n - 1)))
        return round(sorted_durations[idx], 2)

    avg = round(sum(sorted_durations) / n, 2)

    return {
        "avg": avg,
        "p50": percentile(50),
        "p90": percentile(90),
        "p95": percentile(95),
    }


# ---------------------------------------------------------------------------
# Confidence bucketing
# ---------------------------------------------------------------------------

CONFIDENCE_BUCKETS = [
    (0, 49, "0-49"),
    (50, 69, "50-69"),
    (70, 79, "70-79"),
    (80, 89, "80-89"),
    (90, 100, "90-100"),
]


def confidence_bucket(confidence: Optional[float]) -> str:
    """Map a confidence value (0–100) to a bucket label.

    Returns ``"unknown"`` for None or out-of-range values.
    """
    if confidence is None:
        return "unknown"
    try:
        val = float(confidence)
    except (ValueError, TypeError):
        return "unknown"
    for low, high, label in CONFIDENCE_BUCKETS:
        if low <= val <= high:
            return label
    return "unknown"


# ---------------------------------------------------------------------------
# Human intervention detection
# ---------------------------------------------------------------------------

def detect_human_intervention(
    process_logs: Optional[List[Dict[str, Any]]] = None,
    ai_line_items: Optional[List[Dict[str, Any]]] = None,
    final_line_items: Optional[List[Dict[str, Any]]] = None,
    review_msg: Optional[str] = None,
    retry_count: int = 0,
) -> str:
    """Detect human intervention from available evidence.

    Returns one of: ``confirmed_human_intervention``,
    ``likely_human_intervention``, ``no_evidence_of_human_intervention``,
    ``unknown``.
    """
    evidence_count = 0

    # 1. Process logs contain user activity (non-system user)
    if process_logs:
        for log in process_logs:
            user_type_id = log.get("user_type_id")
            # user_type_id 1 appears to be system/AI; non-1 suggests human
            if user_type_id is not None and user_type_id != 1:
                evidence_count += 2  # strong evidence
                break

    # 2. AI line items differ from final RH line items
    if ai_line_items is not None and final_line_items is not None:
        if _line_items_differ(ai_line_items, final_line_items):
            evidence_count += 1

    # 3. Review message indicates review/correction
    if review_msg and _review_msg_indicates_correction(review_msg):
        evidence_count += 1

    # 4. Retry after review
    if retry_count > 0:
        evidence_count += 1

    if evidence_count >= 2:
        return "confirmed_human_intervention"
    if evidence_count == 1:
        return "likely_human_intervention"
    if evidence_count == 0:
        return "no_evidence_of_human_intervention"
    return "unknown"


def _line_items_differ(
    ai_items: List[Dict[str, Any]],
    final_items: List[Dict[str, Any]],
) -> bool:
    """Check if AI-generated line items differ from final RH line items."""
    if len(ai_items) != len(final_items):
        return True
    for ai, final in zip(ai_items, final_items):
        ai_item = ai.get("item") or ai.get("label") or ""
        final_item = final.get("item") or ""
        if ai_item != final_item:
            return True
        ai_total = ai.get("line_item_total") or ai.get("rate", 0) * ai.get("quantity", 0)
        final_total = (final.get("rate") or 0) * (final.get("quantity") or 0)
        if abs(float(ai_total or 0) - float(final_total or 0)) > 0.01:
            return True
    return False


def _review_msg_indicates_correction(review_msg: str) -> bool:
    """Check if a review message indicates human review or correction."""
    indicators = [
        "incorrect", "error", "wrong", "fix", "correct", "adjust",
        "review", "update", "change", "modify", "revise",
    ]
    text = review_msg.lower()
    return any(word in text for word in indicators)


# ---------------------------------------------------------------------------
# Billability classification (Phase 4)
# ---------------------------------------------------------------------------

def classify_billability(
    billing_category: Optional[str],
    is_billable: Optional[bool] = None,
    is_billable_not_determined: Optional[bool] = None,
) -> Dict[str, bool]:
    """Classify billability state from available fields.

    Phase 0 found that ``is_billable`` and ``is_billable_not_determined`` are
    0% populated. We use ``billing_category`` as the primary signal:

    - ``billing_category IS NOT NULL`` → billability determined
    - ``billing_category IS NULL`` → billability undetermined

    Returns a dict with keys ``determined``, ``undetermined``, ``billable``,
    ``not_billable``.
    """
    # Forward-compatible: use is_billable if populated
    if is_billable is not None:
        return {
            "determined": not is_billable_not_determined,
            "undetermined": bool(is_billable_not_determined),
            "billable": is_billable is True,
            "not_billable": is_billable is False,
        }

    # Current production: use billing_category
    if billing_category is not None:
        return {
            "determined": True,
            "undetermined": False,
            "billable": True,  # having a billing category means billable
            "not_billable": False,
        }

    return {
        "determined": False,
        "undetermined": True,
        "billable": False,
        "not_billable": False,
    }


# ---------------------------------------------------------------------------
# AI eligibility (department configuration)
# ---------------------------------------------------------------------------

# fee_send_option values that qualify a fee tile for AI processing. The
# tile-level configuration (use_in_ai_process + fee_send_option) is the
# authoritative eligibility signal — the legacy Departments.IsSendInvoiceAI
# flag does not gate AI invoicing.
AI_SEND_OPTIONS = {"auto", "queued", "limited_auto"}

ELIGIBILITY_ELIGIBLE = "eligible"
ELIGIBILITY_NOT_CONFIGURED = "not_configured"
ELIGIBILITY_UNKNOWN = "unknown"


def classify_fees(fees: Any) -> Dict[str, Any]:
    """Determine AI status from a list of finalized fee/resource records."""
    if not isinstance(fees, list):
        fees = []
    qualifying = [
        f
        for f in fees
        if isinstance(f, dict)
        and f.get("use_in_ai_process")
        and f.get("fee_send_option") in AI_SEND_OPTIONS
    ]
    options = {f.get("fee_send_option") for f in qualifying}

    if not qualifying:
        return {
            "uses_ai": False,
            "ai_mode": "not_using_ai",
            "qualifying_fee_count": 0,
            "has_auto": False,
            "has_queued": False,
            "has_limited_auto": False,
        }

    if len(options) > 1:
        mode = "mixed"
    elif "auto" in options:
        mode = "auto"
    elif "queued" in options:
        mode = "queued"
    else:
        mode = "limited_auto"

    return {
        "uses_ai": True,
        "ai_mode": mode,
        "qualifying_fee_count": len(qualifying),
        "has_auto": "auto" in options,
        "has_queued": "queued" in options,
        "has_limited_auto": "limited_auto" in options,
    }


def classify_ai_eligibility(
    dept_uses_ai: Optional[bool],
    writeback_status: Optional[str] = None,
    business_outcome: Optional[str] = None,
) -> str:
    """Classify whether a claim's department was set up for AI billing.

    Returns ``eligible``, ``not_configured``, or ``unknown``. ``None`` for
    ``dept_uses_ai`` means the configuration source could not be read.
    Actual AI billing activity — a successful writeback or a terminal grid
    outcome — proves the claim was eligible when it ran and overrides the
    current configuration.
    """
    if writeback_status == "success" or business_outcome in (
        "released",
        "cancelled_rejected",
    ):
        return ELIGIBILITY_ELIGIBLE

    if dept_uses_ai is True:
        return ELIGIBILITY_ELIGIBLE
    if dept_uses_ai is False:
        return ELIGIBILITY_NOT_CONFIGURED
    return ELIGIBILITY_UNKNOWN


# ---------------------------------------------------------------------------
# Normalized record builder
# ---------------------------------------------------------------------------

def build_normalized_record(
    sql_row: Dict[str, Any],
    ai_record: Optional[Dict[str, Any]] = None,
    cancellation: Optional[Dict[str, Any]] = None,
    process_logs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a single normalized invoice record from SQL + Mongo data.

    This is the core runtime join function. It does NOT persist anything.
    """
    claim_id = sql_row.get("claim_id")
    # SQL Server returns the column as "AI_inv_process_status" (preserve case)
    ai_inv_process_status = sql_row.get("AI_inv_process_status") or sql_row.get("ai_inv_process_status")

    # Check process logs for terminal actions
    has_released = False
    has_cancelled = False
    business_user_id = None
    if process_logs:
        for log in process_logs:
            text = log.get("log_text", "")
            if text == RELEASED_LOG_TEXT:
                has_released = True
                business_user_id = log.get("user_id")
            elif text == CANCELLED_LOG_TEXT:
                has_cancelled = True
                business_user_id = log.get("user_id")

    has_cancellation = cancellation is not None
    # Newer ai_line_items docs carry their own cancellation flag — treat it
    # as cancellation evidence alongside the SQL details table.
    ai_cancelled = bool(ai_record.get("is_cancelled")) if ai_record else False

    outcome = classify_business_outcome(
        ai_inv_process_status=ai_inv_process_status,
        has_released_log=has_released,
        has_cancelled_log=has_cancelled,
        has_cancellation_record=has_cancellation or ai_cancelled,
    )

    # AI record state
    ai_record_state = "present" if ai_record else "missing"

    # AI processing fields
    claim_processing_status = ai_record.get("claim_processing_status") if ai_record else None
    agent_exec_status = ai_record.get("agent_exec_status") if ai_record else None
    confidence = ai_record.get("confidence_level") if ai_record else None
    writeback = classify_writeback_status(
        ai_record.get("line_items_save_to_rh_status") if ai_record else None,
        claim_processing_status,
    )
    retry = calculate_retry_count(
        ai_record=ai_record,
        retry_thread_id=ai_record.get("retry_thread_id") if ai_record else None,
        agent_exec_status=agent_exec_status,
    )

    # Rejection reason
    raw_reason = None
    raw_reason_descr = None
    normalized_category = None
    reason_id = None
    if cancellation:
        reason_id = cancellation.get("reason_id")
        raw_reason = cancellation.get("raw_reason") or cancellation.get("reason")
        raw_reason_descr = cancellation.get("reason_descr") or cancellation.get("reason_description")
        normalized = normalize_reason(reason_id, raw_reason, raw_reason_descr)
        normalized_category = normalized["normalized_category"]
    elif ai_cancelled and ai_record.get("cancellation_reason"):
        # Cancellation recorded only on the AI-side document
        raw_reason = ai_record["cancellation_reason"]
        normalized = normalize_reason(None, raw_reason, None)
        normalized_category = normalized["normalized_category"]

    if (
        outcome == "cancelled_rejected"
        and normalized_category in (None, "unknown")
        and not raw_reason
    ):
        # The claim was cancelled but no reason was recorded on either the
        # SQL cancellation detail row or the AI-side document — distinct
        # from "unknown", which means a reason WAS recorded but didn't map.
        normalized_category = "no_reason_recorded"

    return {
        "claim_id": claim_id,
        "invoice_number": sql_row.get("invoice_number"),
        "department_id": sql_row.get("dept_id"),
        "department_name": sql_row.get("department_name"),
        "department_state": sql_row.get("department_state"),
        "run_number": sql_row.get("run_number"),
        "claim_created_at": sql_row.get("claim_created_at"),
        "business_status_date": sql_row.get("business_status_date") or sql_row.get("date_of_submitted"),
        "ai_business_updated_at": sql_row.get("ai_business_updated_at"),
        "business_outcome": outcome,
        "raw_rejection_reason": raw_reason,
        "raw_rejection_description": raw_reason_descr,
        "normalized_rejection_category": normalized_category,
        "ai_inv_process_status": ai_inv_process_status,
        "ai_processing_status": claim_processing_status,
        "agent_execution_status": agent_exec_status,
        "is_billable": ai_record.get("is_billable") if ai_record else None,
        "billing_category": ai_record.get("billing_category") if ai_record else None,
        # Step-1 category/fee-item identification output + intake evidence
        # (fee_schedule.STEP1_SOURCE_FIELDS; present on docs from ~2026-09+)
        **step1_source_fields(ai_record),
        # Snapshot of the department's IsSendInvoiceAI flag at processing
        # time (2 = AI invoicing enabled → line items go to the review
        # grid; 0 = evaluated only, never writes line items back). It is
        # NOT a routing decision — there is no straight-through path.
        "dept_send_auto_invoice_status": (
            ai_record.get("dept_send_auto_invoice_status") if ai_record else None
        ),
        "ai_cancelled": ai_cancelled,
        "confidence": confidence,
        "writeback_status": writeback,
        "retry_count": retry,
        "thread_id": ai_record.get("thread_id") if ai_record else None,
        "ai_record_state": ai_record_state,
        "business_record_state": "present",
        "invoice_total": ai_record.get("invoice_total") if ai_record else None,
        "amount_invoiced": sql_row.get("amount_invoiced"),
        "processing_time_seconds": ai_record.get("processing_time_seconds") if ai_record else None,
        "business_user_id": business_user_id,
    }


def index_ai_records_by_claim_id(
    ai_records: List[Dict[str, Any]],
) -> Dict[int, Dict[str, Any]]:
    """Index AI records by claim_id.

    Phase 0 found no duplicate claim_ids in production, but we keep the code
    defensive: if duplicates exist, the most recent (by updated_at) wins.
    """
    by_claim: Dict[int, Dict[str, Any]] = {}
    for record in ai_records:
        claim_id = record.get("claim_id")
        if claim_id is None:
            continue
        try:
            cid = int(claim_id)
        except (ValueError, TypeError):
            continue
        existing = by_claim.get(cid)
        if existing is None:
            by_claim[cid] = record
        else:
            # Keep the most recently updated record
            existing_updated = existing.get("updated_at")
            new_updated = record.get("updated_at")
            if new_updated and (not existing_updated or new_updated > existing_updated):
                by_claim[cid] = record
    return by_claim
