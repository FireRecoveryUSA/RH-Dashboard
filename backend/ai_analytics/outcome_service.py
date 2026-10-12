"""Outcome analytics service — builds business-level AI success analytics.

Combines SQL + Mongo data at request time into normalized runtime views.
Follows the batch retrieval pattern from the plan (Section 28):
1. Query SQL cohort once
2. Extract all claim_ids
3. Query Mongo with $in
4. Query SQL cancellation details in one batch
5. Query SQL process logs in one batch
6. Normalize in memory
7. Aggregate
"""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    AiAnalyticsFilters,
    AiOutcomeSummary,
    AiOutcomeTrendPoint,
    AiRejectionReasonStat,
    AiRejectionReasonBreakdown,
    AiDepartmentOutcomeStat,
    AiPipelineStageStat,
    AiPipelineStageBreakdown,
    AiBillabilityStat,
    AiInvoiceListItem,
    AiInvoiceCohortResponse,
)
from . import sql_repository as sql_repo
from . import projection_read_repository as projection_repo
from .normalization import (
    build_normalized_record,
    classify_business_outcome,
    is_terminal_outcome,
    calculate_release_rate,
    calculate_rejection_rate,
    classify_ai_execution_outcome,
    classify_writeback_status,
    calculate_retry_count,
    classify_billability,
    index_ai_records_by_claim_id,
    RELEASED_LOG_TEXT,
    CANCELLED_LOG_TEXT,
    AI_COMPLETED_STATUSES,
    AI_NOT_ENABLED_STATUSES,
)
from .normalization_core import (
    classify_ai_eligibility,
    ELIGIBILITY_ELIGIBLE,
    ELIGIBILITY_NOT_CONFIGURED,
    ELIGIBILITY_UNKNOWN,
)
from .fee_schedule import identify_fee_result
from .reason_normalization import normalize_reason, CATEGORY_LABELS
from .eligibility_snapshot import (
    STATUS_STALE as ELIGIBILITY_STATUS_STALE,
    resolve_participation,
)
from .cache import cached
from ai_adoption_service import get_ai_participation_map
from config import settings
from database import db_manager

logger = logging.getLogger(__name__)

MAX_DATE_SPAN_DAYS = 366


def _validate_date_span(start_date: Optional[str], end_date: Optional[str]) -> None:
    """Reject date ranges longer than MAX_DATE_SPAN_DAYS."""
    if not start_date or not end_date:
        return
    try:
        start = datetime.fromisoformat(start_date)
        end = datetime.fromisoformat(end_date)
        span = (end - start).days
        if span > MAX_DATE_SPAN_DAYS:
            raise ValueError(
                f"Date span exceeds maximum of {MAX_DATE_SPAN_DAYS} days (requested {span} days)."
            )
    except ValueError as e:
        if "Date span" in str(e):
            raise
        # If dates can't be parsed, let the SQL query handle it
        pass


_DATE_BASIS_TO_PROJECTION_FIELD = {
    # The AI record is created at the moment the run is submitted to us
    # (verified: ai_line_items.inserted_at == Claims.date_of_submitted
    # expressed in UTC), so all "business" date bases resolve to the same
    # projection timestamp. The AI Mongo data is the source of truth for
    # where a run is in the process; Recovery Hub SQL is only joined for
    # claims that reached the review grid (terminal released/cancelled).
    "business_status_date": "ai_inserted_at",
    "claim_created_date": "ai_inserted_at",
    "ai_record_inserted_date": "ai_inserted_at",
    "ai_record_updated_date": "ai_updated_at",
}


def _utc_window_for_local_dates(
    start_date: Optional[str], end_date: Optional[str]
) -> Tuple[Optional[Any], Optional[Any]]:
    """Convert an org-local date window to a UTC datetime window.

    Dashboard date ranges are local civil dates (the SQL date_of_submitted
    convention). The projection timestamps are UTC. A run submitted 8 PM
    local is already the next UTC day, so filtering the projection on raw
    UTC midnight boundaries would drop it or shift it a day. The local
    window is therefore shifted by the configured org offset
    (``settings.AI_LOCAL_UTC_OFFSET_HOURS``, -7 = US MST) into UTC.
    """
    offset = timedelta(hours=settings.AI_LOCAL_UTC_OFFSET_HOURS)

    def _to_utc(date_str: Optional[str], *, end: bool) -> Optional[Any]:
        if not date_str:
            return None
        d = datetime.fromisoformat(date_str)
        if end:
            d = d + timedelta(days=1)
        return d - offset  # local midnight → UTC instant

    return _to_utc(start_date, end=False), _to_utc(end_date, end=True)


def _local_date_str(dt: Optional[Any]) -> Optional[str]:
    """Render a UTC projection timestamp as the org-local date string.

    Mirrors ``_utc_window_for_local_dates``: local = UTC + offset.
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    local = dt + timedelta(hours=settings.AI_LOCAL_UTC_OFFSET_HOURS)
    return local.date().isoformat()


async def _load_normalized_cohort(
    ai_db,
    filters: AiAnalyticsFilters,
) -> Tuple[List[Dict[str, Any]], Dict[str, str], bool]:
    """Load and normalize the full cohort for the given filters.

    Returns (normalized_records, source_status, data_complete).

    Mongo-first cohort (the AI data IS the process data): the cohort is the
    set of ``ai_invoice_analytics`` projection docs in the date window —
    every run submitted to us that entered the AI process, in whatever
    state it currently is (in-flight, completed, failed). The SQL
    ``AIInvoiceProcessRHTemp`` table is deliberately NOT the cohort gate:
    it only receives a row when the AI completes a claim, which hid every
    in-flight run from the dashboard.

    Recovery Hub SQL is joined ONLY for claims that reached the review grid
    (``line_items_save_to_rh_status`` true) — the point where the business
    process actually hands over to Recovery Hub — to fetch the terminal
    outcome evidence (released/cancelled process logs, cancellation
    details). Everything before the grid is answered from the projection.
    """
    t0 = time.perf_counter()
    source_status: Dict[str, str] = {
        "recoveryhub_sql": "available",
        "recoveryhub_ai_mongo": "available",
    }
    data_complete = True

    date_field = _DATE_BASIS_TO_PROJECTION_FIELD.get(filters.date_basis)
    if date_field is None:
        raise ValueError(
            "date_basis must be one of: business_status_date, claim_created_date, "
            "ai_record_inserted_date, ai_record_updated_date"
        )

    # 1. Cohort from the projection (dashboard-owned Mongo), date-filtered.
    t_cohort = time.perf_counter()
    start_utc, end_utc = _utc_window_for_local_dates(filters.start_date, filters.end_date)
    try:
        projection_docs = await projection_repo.get_projection_cohort(
            db_manager.db,
            start_utc=start_utc,
            end_utc=end_utc,
            department_id=filters.department_id,
            date_field=date_field,
        )
    except Exception as e:
        logger.error(f"Projection cohort query failed: {e}")
        source_status["recoveryhub_ai_mongo"] = "unavailable"
        return [], source_status, False
    logger.info(f"AI analytics: projection cohort took {time.perf_counter() - t_cohort:.3f}s ({len(projection_docs)} docs)")

    if not projection_docs:
        return [], source_status, True

    # 2. Normalize from the projection alone — no SQL row required. The
    #    projection carries identity (department, run number), AI state,
    #    and step-1 output; build_normalized_record consumes a synthetic
    #    SQL-row shape so the shared join logic is reused unchanged (DRY).
    t_norm = time.perf_counter()
    normalized: List[Dict[str, Any]] = []
    grid_claim_ids: List[int] = []
    docs_by_claim: Dict[int, Dict[str, Any]] = {}
    for doc in projection_docs:
        claim_id = doc.get("_id")
        try:
            claim_id = int(claim_id)
        except (ValueError, TypeError):
            continue
        if doc.get("has_ai_line_item_record") is False:
            # Conversation-only trace — not an AI-processed run. The cohort
            # query filters these; guarded here too so direct callers of
            # the normalization loop stay correct.
            continue
        docs_by_claim[claim_id] = doc
        ai_record = projection_repo.projection_to_ai_record(doc)
        reached_grid = bool(ai_record.get("line_items_save_to_rh_status"))
        if reached_grid:
            grid_claim_ids.append(claim_id)
        sql_row = {
            "claim_id": claim_id,
            "invoice_number": None,
            "dept_id": doc.get("department_id"),
            "department_name": doc.get("department_name"),
            "department_state": None,
            "run_number": doc.get("run_number"),
            "claim_created_at": None,
            # "Submitted to us" date, rendered in org-local terms so the
            # trend/grouping matches the SQL-era semantics.
            "business_status_date": _local_date_str(doc.get("ai_inserted_at")),
            "ai_business_updated_at": _local_date_str(doc.get("ai_updated_at")),
            # RH-side process status is intentionally None pre-grid; the
            # outcome for those claims is derived below from the AI state.
            "AI_inv_process_status": None,
            "amount_invoiced": None,
        }
        record = build_normalized_record(sql_row, ai_record)
        if record["business_outcome"] == "unknown":
            # No RH terminal evidence (no released/cancelled logs, no
            # cancellation) — the claim is still in the process, whether
            # in flight pre-grid or sitting on the grid awaiting review.
            # That IS pending, not unknown.
            record["business_outcome"] = "pending"
        record["business_record_state"] = "ai_only"
        normalized.append(record)
    logger.info(f"AI analytics: Normalization took {time.perf_counter() - t_norm:.3f}s ({len(normalized)} records)")

    # 3. Run lifecycle (Claims) — the same source the Claims Breakdown
    #    buckets use, so "New runs / Active runs" here reconcile exactly
    #    with those widgets. One indexed PK batch for the whole cohort.
    t_life = time.perf_counter()
    lifecycle_by_claim: Dict[int, Dict[str, Any]] = {}
    try:
        lifecycle_by_claim = sql_repo.get_claim_lifecycle_for_claims(
            [r["claim_id"] for r in normalized]
        )
    except Exception as e:
        logger.error(f"SQL claim lifecycle query failed: {e}")
        source_status["recoveryhub_sql"] = "partial"
        data_complete = False
    for record in normalized:
        life = lifecycle_by_claim.get(record["claim_id"], {})
        record["run_type_id"] = life.get("type_id")
        record["rh_claim_status"] = life.get("status")
        record["rh_archived"] = life.get("archived")
    logger.info(f"AI analytics: claim lifecycle took {time.perf_counter() - t_life:.3f}s ({len(lifecycle_by_claim)} claims)")

    # 3. SQL enrichment — terminal outcome evidence for grid claims only.
    t_sql = time.perf_counter()
    if grid_claim_ids:
        try:
            cancellations_by_claim = sql_repo.get_cancellation_details_for_claims(grid_claim_ids)
        except Exception as e:
            logger.error(f"SQL cancellation details query failed: {e}")
            cancellations_by_claim = {}
            source_status["recoveryhub_sql"] = "partial"
            data_complete = False
        try:
            logs_by_claim = sql_repo.get_process_logs_for_claims(grid_claim_ids)
        except Exception as e:
            logger.error(f"SQL process logs query failed: {e}")
            logs_by_claim = {}
            source_status["recoveryhub_sql"] = "partial"
            data_complete = False
        for i, record in enumerate(normalized):
            cid = record["claim_id"]
            if cid not in cancellations_by_claim and cid not in logs_by_claim:
                continue
            # Re-run the shared join with the RH evidence included so
            # released / cancelled outcomes are classified from the
            # authoritative log text. The synthetic SQL row is unchanged.
            ai_record = projection_repo.projection_to_ai_record(docs_by_claim[cid])
            enriched = build_normalized_record(
                {
                    "claim_id": cid,
                    "invoice_number": None,
                    "dept_id": record.get("department_id"),
                    "department_name": record.get("department_name"),
                    "department_state": record.get("department_state"),
                    "run_number": record.get("run_number"),
                    "claim_created_at": record.get("claim_created_at"),
                    "business_status_date": record.get("business_status_date"),
                    "ai_business_updated_at": record.get("ai_business_updated_at"),
                    "AI_inv_process_status": None,
                    "amount_invoiced": None,
                },
                ai_record,
                cancellations_by_claim.get(cid),
                logs_by_claim.get(cid, []),
            )
            if enriched["business_outcome"] == "unknown":
                # Same rule as the first pass: no terminal evidence —
                # still in the process, pending.
                enriched["business_outcome"] = "pending"
            enriched["business_record_state"] = record.get("business_record_state")
            # Preserve the run-lifecycle fields attached after the first
            # pass — the enrichment rebuild must not drop them.
            for key in ("run_type_id", "rh_claim_status", "rh_archived"):
                enriched[key] = record.get(key)
            normalized[i] = enriched
    logger.info(f"AI analytics: SQL grid enrichment took {time.perf_counter() - t_sql:.3f}s ({len(grid_claim_ids)} grid claims)")

    # 4. Attach AI eligibility — is the claim's department actually set up
    # for AI billing? Done here, once, so every downstream endpoint
    # (funnel, summary, diagnostics, trend, departments) shares one
    # definition. In projection mode the department map is served from the
    # dashboard-owned snapshot so this step, like step 1, does not depend
    # on the operational AI Mongo being reachable.
    t_elig = time.perf_counter()
    participation, fee_config_status = await resolve_participation(
        ai_db,
        db_manager.db,
        fetch=get_ai_participation_map,
        prefer_snapshot=bool(settings.AI_ANALYTICS_USE_PROJECTION),
    )
    source_status["recoveryhub_ai_fee_config"] = fee_config_status
    if participation is None:
        logger.warning(
            "AI analytics: department AI configuration unavailable; "
            "eligibility will be reported as unknown."
        )
        data_complete = False
    elif fee_config_status == ELIGIBILITY_STATUS_STALE:
        # Eligibility came from an expired snapshot; surface it as
        # incomplete so the dashboards show their data warning.
        data_complete = False

    for record in normalized:
        dept_id = record.get("department_id")
        dept_uses_ai: Optional[bool] = None
        info: Optional[Dict[str, Any]] = None
        if participation is not None and dept_id is not None:
            info = participation.get(int(dept_id))
            # uses_ai=None means conflicting finalized schedules —
            # preserve it as unknown rather than bool-coercing to
            # not_configured. A missing department id stays unknown.
            dept_uses_ai = info.get("uses_ai") if info else False
        record["ai_eligibility"] = classify_ai_eligibility(
            dept_uses_ai=dept_uses_ai,
            writeback_status=record.get("writeback_status"),
            business_outcome=record.get("business_outcome"),
        )
        # Step-1 result described against this department's current fee
        # catalog (when the participation map carries one).
        record["fee_identification"] = identify_fee_result(
            record, info, fee_config_status
        )
    logger.info(
        f"AI analytics: Eligibility classification took "
        f"{time.perf_counter() - t_elig:.3f}s "
        f"({len(participation or {})} configured departments)"
    )
    logger.info(f"AI analytics: Total _load_normalized_cohort took {time.perf_counter() - t0:.3f}s")

    return normalized, source_status, data_complete


def qualified_records(
    records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Records whose department was (or may have been) set up for AI billing.

    Excludes only claims definitively known to be in a department with no
    AI-enabled fee tile. Eligibility-unknown claims are kept so a failed
    configuration read degrades the metrics rather than silently dropping
    most of the cohort.
    """
    return [
        r for r in records
        if r.get("ai_eligibility") != ELIGIBILITY_NOT_CONFIGURED
    ]


def qualified_ai_runs(
    records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Qualified records where the AI workflow actually ran."""
    return [
        r for r in qualified_records(records)
        if r.get("ai_record_state") == "present"
    ]


def count_not_configured(records: List[Dict[str, Any]]) -> int:
    """Number of claims that did not qualify for AI billing at all."""
    return sum(
        1 for r in records
        if r.get("ai_eligibility") == ELIGIBILITY_NOT_CONFIGURED
    )


def _apply_filters(
    records: List[Dict[str, Any]],
    filters: AiAnalyticsFilters,
) -> List[Dict[str, Any]]:
    """Apply post-normalization filters that can't be done in SQL."""
    result = []
    for r in records:
        if filters.business_outcome and r["business_outcome"] != filters.business_outcome:
            continue
        if filters.ai_processing_status and r.get("ai_processing_status") != filters.ai_processing_status:
            continue
        if filters.agent_execution_status and r.get("agent_execution_status") != filters.agent_execution_status:
            continue
        if filters.confidence_min is not None:
            if r.get("confidence") is None or r["confidence"] < filters.confidence_min:
                continue
        if filters.confidence_max is not None:
            if r.get("confidence") is None or r["confidence"] > filters.confidence_max:
                continue
        if filters.has_retry is not None:
            r_has_retry = r["retry_count"] > 0
            if r_has_retry != filters.has_retry:
                continue
        if filters.writeback_status and r.get("writeback_status") != filters.writeback_status:
            continue
        if filters.billing_category and r.get("billing_category") != filters.billing_category:
            continue
        if filters.reason_category and r.get("normalized_rejection_category") != filters.reason_category:
            continue
        result.append(r)
    return result


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

@cached(ttl=60, func_name="get_outcome_summary")
async def get_outcome_summary(
    ai_db,
    filters: AiAnalyticsFilters,
) -> AiOutcomeSummary:
    """Build the executive KPI summary."""
    _validate_date_span(filters.start_date, filters.end_date)

    records, source_status, data_complete = await _load_normalized_cohort(ai_db, filters)
    records = _apply_filters(records, filters)

    did_not_qualify = count_not_configured(records)
    records = qualified_records(records)

    total = len(records)
    released = sum(1 for r in records if r["business_outcome"] == "released")
    cancelled = sum(1 for r in records if r["business_outcome"] == "cancelled_rejected")
    pending = sum(1 for r in records if r["business_outcome"] == "pending")
    unknown = sum(1 for r in records if r["business_outcome"] == "unknown")
    terminal = released + cancelled

    # AI processing stats
    ai_completed = sum(1 for r in records if r.get("ai_processing_status") in AI_COMPLETED_STATUSES)
    ai_failed = sum(
        1 for r in records
        if classify_ai_execution_outcome(
            r.get("ai_processing_status"), r.get("agent_execution_status")
        ) == "failed"
    )
    ai_not_enabled = sum(
        1 for r in records
        if r.get("ai_processing_status") in AI_NOT_ENABLED_STATUSES
    )

    # Writeback stats
    writeback_success = sum(1 for r in records if r.get("writeback_status") == "success")
    writeback_failed = sum(
        1 for r in records if r.get("writeback_status") == "failed_or_not_saved"
    )

    # Confidence
    confidences = [r["confidence"] for r in records if r.get("confidence") is not None]
    avg_confidence = round(sum(confidences) / len(confidences), 2) if confidences else None

    return AiOutcomeSummary(
        total_ai_invoices=total,
        did_not_qualify=did_not_qualify,
        released=released,
        cancelled_rejected=cancelled,
        pending=pending,
        unknown=unknown,
        terminal_count=terminal,
        business_release_rate=calculate_release_rate(released, cancelled) or 0.0,
        rejection_rate=calculate_rejection_rate(released, cancelled) or 0.0,
        ai_completed=ai_completed,
        ai_failed=ai_failed,
        ai_not_enabled=ai_not_enabled,
        writeback_success=writeback_success,
        writeback_failed=writeback_failed,
        avg_confidence=avg_confidence,
        source_status=source_status,
        data_complete=data_complete,
    )


# ---------------------------------------------------------------------------
# Funnel
# ---------------------------------------------------------------------------

@cached(ttl=60, func_name="get_outcome_funnel")
async def get_outcome_funnel(
    ai_db,
    filters: AiAnalyticsFilters,
) -> List[AiPipelineStageStat]:
    """Build the pipeline funnel — six sections, all scoped to the period.

    The cohort is Mongo-first (the projection): every run the AI touched
    that was submitted in the date window, in its current real-time state
    — not gated on the SQL AIInvoiceProcessRHTemp table, which only fills
    in when a claim completes and would hide everything in-flight.

    1. Step 1 — fee card & value plugged in: the billing level/category
       the AI identified (the actual fee card: Motor Vehicle Incident
       Level 1, Standard Response Fee, ...), with the run count and the
       dollar value plugged into the fees & resources / fees & billables
       area. Not all categories have levels, so there is no separate
       "category no level" bucket — category-only runs appear under
       their category (the card family).
    2. AI runs submitted this period — the date-filtered cohort, split
       by where the runs are now (still in New Runs vs moved on).
    3. Left New Runs — claim status at handoff: runs that left New Runs
       (ClaimCurrentTypeId ≠ 1), grouped by the RH claim status.
    4. Human-in-the-loop grid vs elsewhere — over the runs whose AI
       processing has FINISHED: line items saved to the review grid vs
       handed off without it. Runs still being processed have not gone
       anywhere yet and are in neither row.
    5. In the HITL grid now — this period's runs currently pending
       human review, broken down by department; the invoice drill-down
       grid below lists them individually.
    6. Left the HITL grid — disposition: accepted/released (with the RH
       status) or rejected, with the normalized rejection-reason
       breakdown that has always been used.

    Stage counts are not monotonically decreasing — the sections answer
    different questions about the same period cohort. Each breakdown
    partitions its own stage.
    """
    _validate_date_span(filters.start_date, filters.end_date)

    records, source_status, data_complete = await _load_normalized_cohort(ai_db, filters)
    records = _apply_filters(records, filters)
    # The funnel covers ONLY the eligible cohort — the runs actually going
    # through the AI process. Runs from departments without a qualifying
    # AI tile never enter this pipeline (nobody processes them out of new
    # runs through AI); they are accounted for in the summary KPI
    # (did_not_qualify) instead of inflating the funnel stages.
    records = qualified_records(records)
    total = len(records)

    # Run lifecycle (Claims — the same source as the Claims Breakdown
    # buckets): ClaimCurrentTypeId 1 = New Run, 4 = Active Run.
    def _is_active_run(r) -> bool:
        if r.get("run_type_id") is not None:
            return r["run_type_id"] == 4
        # Lifecycle unavailable — fall back to the AI handoff signal.
        return r.get("writeback_status") == "success"

    active_runs = [r for r in records if _is_active_run(r)]
    new_runs = [r for r in records if not _is_active_run(r)]
    grid_runs = [r for r in records if r.get("writeback_status") == "success"]
    on_grid_now = [r for r in grid_runs if r["business_outcome"] == "pending"]
    left_grid_released = [r for r in grid_runs if r["business_outcome"] == "released"]
    left_grid_rejected = [r for r in grid_runs if r["business_outcome"] == "cancelled_rejected"]

    # ------------------------------------------------------------------
    # Section 1 — Step 1 results: the fee card each run plugged into,
    # with the run count AND the dollar value plugged into the fees &
    # resources / fees & billables area. The card is the billing_level
    # when the AI identified one, else the billing_category (the card
    # family — not all categories have levels, so there is no separate
    # "category no level" bucket).
    # ------------------------------------------------------------------
    card_counts: Counter = Counter()
    card_values: Dict[str, float] = defaultdict(float)
    for r in records:
        card = r.get("billing_level") or r.get("billing_category")
        if not card:
            continue
        card = str(card).strip()
        card_counts[card] += 1
        if r.get("invoice_total") is not None:
            card_values[card] += r["invoice_total"]
    step1_breakdown = [
        AiPipelineStageBreakdown(
            label=card,
            count=n,
            value=round(card_values.get(card, 0.0), 2) or None,
        )
        for card, n in card_counts.most_common()
    ]

    # ------------------------------------------------------------------
    # Section 2 — AI runs submitted THIS period (the cohort is
    # date-filtered on the AI record's created-at, so nothing from any
    # other period leaks in), split by where they are now.
    # ------------------------------------------------------------------
    submitted_breakdown = [
        AiPipelineStageBreakdown(
            label="Still in new runs — AI working or awaiting grid",
            count=len(new_runs),
        ),
        AiPipelineStageBreakdown(
            label="Moved on — active runs or beyond",
            count=len(active_runs),
        ),
    ]

    # ------------------------------------------------------------------
    # Section 3 — the claim status each run was given when it was
    # released from New Runs (grouped by RH claim status; runs still in
    # New Runs are not part of this section).
    # ------------------------------------------------------------------
    left_new = active_runs
    left_new_status: Counter = Counter()
    for r in left_new:
        status = r.get("rh_claim_status")
        if not status:
            status = {
                "released": "Released",
                "cancelled_rejected": "Cancelled / Rejected",
                "pending": "On review grid",
            }.get(r["business_outcome"], "Unknown")
        left_new_status[str(status).strip()] += 1
    left_new_breakdown = [
        AiPipelineStageBreakdown(label=label, count=n)
        for label, n in left_new_status.most_common()
    ]

    # ------------------------------------------------------------------
    # Section 4 — the two-way split over runs whose AI processing has
    # FINISHED: line items saved to the human-in-the-loop grid vs handed
    # off elsewhere (department takes the invoice directly / evaluated
    # only). Runs still being processed by the AI have not gone anywhere
    # yet and are counted in neither row.
    # ------------------------------------------------------------------
    finished = [r for r in records if r.get("ai_processing_status") in AI_COMPLETED_STATUSES]
    finished_to_grid = [r for r in finished if r.get("writeback_status") == "success"]
    hitl_breakdown = [
        AiPipelineStageBreakdown(
            label="Went to human-in-the-loop grid",
            count=len(finished_to_grid),
        ),
        AiPipelineStageBreakdown(
            label="Went elsewhere — finished without the review grid",
            count=len(finished) - len(finished_to_grid),
        ),
    ]

    # ------------------------------------------------------------------
    # Section 5 — runs currently sitting in the HITL grid from this
    # period's submissions (the invoice drill-down grid below lists
    # them individually).
    # ------------------------------------------------------------------
    on_grid_by_dept: Counter = Counter()
    for r in on_grid_now:
        on_grid_by_dept[r.get("department_name") or "Unknown department"] += 1
    on_grid_breakdown = [
        AiPipelineStageBreakdown(label=dept, count=n)
        for dept, n in on_grid_by_dept.most_common()
    ]

    # ------------------------------------------------------------------
    # Section 6 — the disposition of each run when it LEFT the HITL
    # grid: accepted/released, or rejected with the reason breakdown
    # that has always been used.
    # ------------------------------------------------------------------
    left_grid_status: Counter = Counter()
    for r in left_grid_released:
        status = r.get("rh_claim_status")
        left_grid_status[f"Accepted / Released — {status or 'status unknown'}"] += 1
    rejection_counts: Counter = Counter()
    for r in left_grid_rejected:
        rejection_counts[
            r.get("normalized_rejection_category") or "unknown"
        ] += 1
    left_grid_rows = [
        AiPipelineStageBreakdown(label=label, count=n)
        for label, n in left_grid_status.most_common()
    ]
    if left_grid_rejected:
        left_grid_rows.append(AiPipelineStageBreakdown(
            label="Rejected", count=len(left_grid_rejected),
        ))
    left_grid_rows.extend(
        AiPipelineStageBreakdown(
            label="Rejected — "
            + CATEGORY_LABELS.get(cat, cat.replace("_", " ").title()),
            count=n,
        )
        for cat, n in rejection_counts.most_common()
    )

    stages = [
        AiPipelineStageStat(
            stage="Step 1 — fee card & value plugged in",
            count=sum(card_counts.values()),
            description="Billing level/category the AI identified, with the invoice value plugged in",
            breakdown=step1_breakdown or None,
        ),
        AiPipelineStageStat(
            stage="AI runs submitted this period",
            count=total,
            description="Eligible runs the AI touched that were submitted in this period only",
            breakdown=submitted_breakdown,
        ),
        AiPipelineStageStat(
            stage="Left New Runs — claim status at handoff",
            count=len(left_new),
            description="ClaimCurrentTypeId ≠ 1, grouped by the RH claim status",
            breakdown=left_new_breakdown or None,
        ),
        AiPipelineStageStat(
            stage="Human-in-the-loop grid vs elsewhere",
            count=len(finished),
            description="Of the runs whose AI processing finished: review grid or handled without it",
            breakdown=hitl_breakdown,
        ),
        AiPipelineStageStat(
            stage="In the HITL grid now (pending review)",
            count=len(on_grid_now),
            description="This period's runs currently awaiting human review — listed in the invoice grid below",
            breakdown=on_grid_breakdown or None,
        ),
        AiPipelineStageStat(
            stage="Left the HITL grid — disposition",
            count=len(left_grid_released) + len(left_grid_rejected),
            description="Accepted/released or rejected (with reason breakdown)",
            breakdown=left_grid_rows or None,
        ),
    ]

    return stages


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------

async def get_outcome_trend(
    ai_db,
    filters: AiAnalyticsFilters,
    grain: str = "day",
) -> List[AiOutcomeTrendPoint]:
    """Build trend over time."""
    _validate_date_span(filters.start_date, filters.end_date)

    records, source_status, data_complete = await _load_normalized_cohort(ai_db, filters)
    records = qualified_records(_apply_filters(records, filters))

    # Group by time period
    period_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    date_field_by_basis = {
        # The cohort is Mongo-first: "submitted to us" is the AI record's
        # inserted_at rendered as an org-local date on the normalized
        # record's business_status_date field.
        "business_status_date": "business_status_date",
        "claim_created_date": "business_status_date",
        "ai_record_inserted_date": "business_status_date",
        "ai_record_updated_date": "ai_business_updated_at",
    }
    date_field = date_field_by_basis.get(filters.date_basis)
    if date_field is None:
        raise ValueError(
            "date_basis must be one of: business_status_date, claim_created_date, ai_record_updated_date"
        )

    for r in records:
        date_str = r.get(date_field)
        if not date_str:
            continue
        try:
            dt = datetime.fromisoformat(date_str.replace("T", " ").split(".")[0])
        except (ValueError, TypeError):
            continue

        if grain == "week":
            # ISO week
            iso_year, iso_week, _ = dt.isocalendar()
            key = f"{iso_year}-W{iso_week:02d}"
        elif grain == "month":
            key = f"{dt.year}-{dt.month:02d}"
        else:
            key = dt.strftime("%Y-%m-%d")

        period_groups[key].append(r)

    points: List[AiOutcomeTrendPoint] = []
    for period in sorted(period_groups.keys()):
        group = period_groups[period]
        released = sum(1 for r in group if r["business_outcome"] == "released")
        rejected = sum(1 for r in group if r["business_outcome"] == "cancelled_rejected")
        pending = sum(1 for r in group if r["business_outcome"] == "pending")
        release_rate = calculate_release_rate(released, rejected)
        points.append(AiOutcomeTrendPoint(
            period=period,
            total=len(group),
            released=released,
            rejected=rejected,
            pending=pending,
            release_rate=release_rate,
        ))

    return points


# ---------------------------------------------------------------------------
# Rejection reasons
# ---------------------------------------------------------------------------

@cached(ttl=60, func_name="get_rejection_reasons")
async def get_rejection_reasons(
    ai_db,
    filters: AiAnalyticsFilters,
) -> List[AiRejectionReasonStat]:
    """Build rejection reason analytics with drill-down."""
    _validate_date_span(filters.start_date, filters.end_date)

    records, source_status, data_complete = await _load_normalized_cohort(ai_db, filters)
    records = qualified_records(_apply_filters(records, filters))

    # Only look at cancelled/rejected records
    rejected = [r for r in records if r["business_outcome"] == "cancelled_rejected"]

    # Group by normalized category
    category_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rejected:
        category = r.get("normalized_rejection_category") or "unknown"
        category_groups[category].append(r)

    total_rejected = len(rejected)
    stats: List[AiRejectionReasonStat] = []
    for category, group in sorted(category_groups.items(), key=lambda x: -len(x[1])):
        # Build raw reason breakdown
        raw_reason_counts: Counter = Counter()
        for r in group:
            raw_reason = r.get("raw_rejection_reason") or "No reason recorded"
            raw_reason_counts[raw_reason] += 1

        breakdown = [
            AiRejectionReasonBreakdown(raw_reason=reason, count=count)
            for reason, count in raw_reason_counts.most_common()
        ]

        percent = round(len(group) / total_rejected * 100, 2) if total_rejected > 0 else 0.0

        stats.append(AiRejectionReasonStat(
            normalized_category=category,
            count=len(group),
            percent=percent,
            raw_reason_breakdown=breakdown,
        ))

    return stats


# ---------------------------------------------------------------------------
# Departments
# ---------------------------------------------------------------------------

@cached(ttl=60, func_name="get_department_outcomes")
async def get_department_outcomes(
    ai_db,
    filters: AiAnalyticsFilters,
) -> List[AiDepartmentOutcomeStat]:
    """Build department comparison table."""
    _validate_date_span(filters.start_date, filters.end_date)

    records, source_status, data_complete = await _load_normalized_cohort(ai_db, filters)
    records = qualified_records(_apply_filters(records, filters))

    # Group by department
    dept_groups: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        dept_id = r.get("department_id")
        if dept_id is not None:
            dept_groups[int(dept_id)].append(r)

    stats: List[AiDepartmentOutcomeStat] = []
    for dept_id, group in dept_groups.items():
        released = sum(1 for r in group if r["business_outcome"] == "released")
        rejected = sum(1 for r in group if r["business_outcome"] == "cancelled_rejected")
        pending = sum(1 for r in group if r["business_outcome"] == "pending")

        ai_completed = sum(1 for r in group if r.get("ai_processing_status") in AI_COMPLETED_STATUSES)
        writeback_failed = sum(1 for r in group if r.get("writeback_status") == "failed_or_not_saved")

        confidences = [r["confidence"] for r in group if r.get("confidence") is not None]
        avg_conf = round(sum(confidences) / len(confidences), 2) if confidences else None

        retry_count = sum(r.get("retry_count", 0) for r in group)
        human_intervention = sum(
            1 for r in group
            if r.get("business_user_id") is not None
        )

        first = group[0]
        stats.append(AiDepartmentOutcomeStat(
            department_id=dept_id,
            department_name=first.get("department_name"),
            state=first.get("department_state"),
            volume=len(group),
            released=released,
            rejected=rejected,
            pending=pending,
            release_rate=calculate_release_rate(released, rejected),
            ai_completion_rate=round(ai_completed / len(group) * 100, 2) if group else None,
            writeback_failure_rate=round(writeback_failed / len(group) * 100, 2) if group else None,
            avg_confidence=avg_conf,
            retry_count=retry_count,
            human_intervention_count=human_intervention,
        ))

    # Sort by volume descending
    stats.sort(key=lambda x: -x.volume)
    return stats


# ---------------------------------------------------------------------------
# Invoice cohort (drill-down)
# ---------------------------------------------------------------------------

async def get_invoice_cohort(
    ai_db,
    filters: AiAnalyticsFilters,
) -> AiInvoiceCohortResponse:
    """Return paged invoice cohort for drill-down."""
    _validate_date_span(filters.start_date, filters.end_date)

    records, source_status, data_complete = await _load_normalized_cohort(ai_db, filters)
    records = qualified_records(_apply_filters(records, filters))

    # Sort
    sort_by = filters.sort_by
    reverse = filters.sort_direction == "desc"
    if sort_by and records:
        records.sort(
            key=lambda r: (r.get(sort_by) is None, r.get(sort_by) or ""),
            reverse=reverse,
        )

    total = len(records)
    page = filters.page
    page_size = filters.page_size
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    page_records = records[start_idx:end_idx]

    invoices = [
        AiInvoiceListItem(
            claim_id=r["claim_id"],
            invoice_number=r.get("invoice_number"),
            department_id=r.get("department_id"),
            department_name=r.get("department_name"),
            run_number=r.get("run_number"),
            claim_created_at=r.get("claim_created_at"),
            ai_business_updated_at=r.get("ai_business_updated_at"),
            business_outcome=r["business_outcome"],
            raw_rejection_reason=r.get("raw_rejection_reason"),
            raw_rejection_description=r.get("raw_rejection_description"),
            normalized_rejection_category=r.get("normalized_rejection_category"),
            ai_processing_status=r.get("ai_processing_status"),
            agent_execution_status=r.get("agent_execution_status"),
            is_billable=r.get("is_billable"),
            billing_category=r.get("billing_category"),
            confidence=r.get("confidence"),
            writeback_status=r.get("writeback_status", "unknown"),
            retry_count=r.get("retry_count", 0),
            thread_id=r.get("thread_id"),
            ai_record_state=r.get("ai_record_state", "present"),
            business_record_state=r.get("business_record_state", "present"),
            invoice_total=r.get("invoice_total"),
            amount_invoiced=r.get("amount_invoiced"),
            processing_time_seconds=r.get("processing_time_seconds"),
        )
        for r in page_records
    ]

    return AiInvoiceCohortResponse(
        invoices=invoices,
        total_count=total,
        page=page,
        page_size=page_size,
        source_status=source_status,
        data_complete=data_complete,
    )


# ---------------------------------------------------------------------------
# Billability (Phase 4)
# ---------------------------------------------------------------------------

@cached(ttl=60, func_name="get_billability_stats")
async def get_billability_stats(
    ai_db,
    filters: AiAnalyticsFilters,
) -> AiBillabilityStat:
    """Build incident/billability evaluation metrics."""
    _validate_date_span(filters.start_date, filters.end_date)

    records, source_status, data_complete = await _load_normalized_cohort(ai_db, filters)

    # Only count qualified records with AI data
    ai_records = qualified_ai_runs(records)

    billability_determined = 0
    billability_undetermined = 0
    billable = 0
    not_billable = 0
    category_dist: Counter = Counter()

    for r in ai_records:
        billing_cat = r.get("billing_category")
        billab = classify_billability(
            billing_category=billing_cat,
            is_billable=r.get("is_billable"),
        )
        if billab["determined"]:
            billability_determined += 1
        if billab["undetermined"]:
            billability_undetermined += 1
        if billab["billable"]:
            billable += 1
        if billab["not_billable"]:
            not_billable += 1
        if billing_cat:
            category_dist[billing_cat] += 1

    return AiBillabilityStat(
        ai_records=len(ai_records),
        billability_determined=billability_determined,
        billability_undetermined=billability_undetermined,
        billable=billable,
        not_billable=not_billable,
        billing_category_distribution=dict(category_dist.most_common()),
    )
