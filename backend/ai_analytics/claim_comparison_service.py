"""Claim Payment Comparison — final payment outcome of AI-released vs other runs.

Cohorts (per run in scope):
- ``ai_released``: AI line items were released from the AI grid.
- ``other`` / ``manual``: never went through the AI grid.
- ``other`` / ``ai_rejected``: went through the AI grid but was cancelled/rejected.
- ``ai_pending``: AI grid outcome still pending or unknown. Counted only;
  excluded from every rate.

Final state per run comes from SQL system versioning. Each claim's status
versions (current row plus history, filtered by claim id) are bucketed with
``claim_status_config``. Archive-bucket versions are ignored entirely, so the
effective status is the non-archive version with the latest row start. That
version's row start is the effective date used for weekly bucketing.

Billing target (direct / insurance / unknown) and incident type are per run,
taken from the same status versions and from ``Claims.type_id``. They do not
depend on the AI side, so both cohorts are compared on identical terms.

SQL reads are request-time. The AI side reuses ``_load_normalized_cohort``
from the outcome service, so it reads the same projection as the other AI
dashboards.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import os
import socket
import time
import uuid
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from .models import (
    AiAnalyticsFilters,
    AiClaimComparisonResponse,
    AiClaimComparisonSchemaCheck,
    ClaimComparisonClaim,
    ClaimComparisonNotBillable,
    ClaimComparisonNotBillableCohort,
    ClaimComparisonSegment,
    ClaimComparisonWeek,
    ClaimComparisonWeeklyCounts,
    ClaimStatusBreakdown,
    ClaimStatusValueCheck,
    RunTypeValueCheck,
)
from . import sql_repository as sql_repo
from .outcome_service import _load_normalized_cohort, iso_week_key
from config import settings
from .normalization_core import classify_billability
from ai_adoption_service import EXCLUDED_DEPARTMENT_IDS
from ai_analytics_worker.config import worker_config
from ai_analytics_worker.leader_election import release_lease, try_acquire_lease
from .claim_status_config import (
    BUCKET_ARCHIVE,
    BUCKET_DENIED,
    BUCKET_IN_PROGRESS,
    BUCKET_NOT_BILLABLE,
    BUCKET_PAID,
    BUCKET_READY_TO_BE_PAID,
    BUCKET_UNDER_REVIEW,
    FINAL_BUCKETS,
    billing_target_from_statuses,
    billing_target_label,
    classify_claim_status,
    incident_type_label,
    is_mapped_claim_status,
    INSURANCE_BILLING_STATUSES,
)
from .cache import cached, prime_cache, touch_cache

logger = logging.getLogger(__name__)

HISTORY_CHUNK_SIZE = 2000

# Cache TTL must exceed the warm interval plus a margin for the warm compute
# itself (~230s cold), or requests between warms would recompute against a
# cold SQL buffer cache (exceeding the proxy's 180s timeout).
CACHE_TTL_SECONDS = 3600

COHORT_AI_RELEASED = "ai_released"
COHORT_OTHER = "other"
COHORT_AI_PENDING = "ai_pending"
SUBTYPE_MANUAL = "manual"
SUBTYPE_AI_REJECTED = "ai_rejected"


def _pct(numerator: int, denominator: int) -> Optional[float]:
    if denominator == 0:
        return None
    return round(numerator / denominator * 100, 2)


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def resolve_comparison_window(end_date: Optional[str]) -> Tuple[str, str]:
    """Fixed window: most recent July 1 through ``end_date`` (default today).

    Uses the effective end date's year; if that July 1 is still in the future
    (Jan–Jun), the window starts on July 1 of the previous year so it never
    inverts.
    """
    today = date.fromisoformat(sql_repo.get_server_date())
    end = date.fromisoformat(end_date) if end_date else today
    start = date(end.year, 7, 1)
    if start > end:
        start = date(end.year - 1, 7, 1)
    return start.isoformat(), end.isoformat()


def _chunks(items: List[int], size: int) -> Iterator[List[int]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _dept_excluded(record: Dict[str, Any]) -> bool:
    """Whether the record's department is one the SQL scope excludes (Rural
    Metro). Projection department IDs can arrive as strings — normalize."""
    try:
        return int(record.get("department_id")) in EXCLUDED_DEPARTMENT_IDS
    except (TypeError, ValueError):
        return False


def _cohort_for(record: Optional[Dict[str, Any]]) -> Tuple[str, Optional[str]]:
    if record is None:
        return COHORT_OTHER, SUBTYPE_MANUAL
    outcome = record.get("business_outcome")
    if outcome == "released":
        return COHORT_AI_RELEASED, None
    if outcome == "cancelled_rejected":
        return COHORT_OTHER, SUBTYPE_AI_REJECTED
    return COHORT_AI_PENDING, None


def _effective_version(
    versions: Iterable[Tuple[str, str, datetime, bool, datetime]],
    as_of: Optional[datetime] = None,
) -> Optional[Tuple[str, str, datetime, bool, datetime]]:
    """Latest non-archive version under the same rules everywhere it is used.

    ``as_of`` caps the state at the end of the comparison window: versions
    recorded after it are ignored, so a past window never includes future
    payment outcomes.

    When two statuses share the same latest row start, the live Claims row wins:
    it is the authoritative current state. Without this, the choice depends on
    SQL row order and changes between requests.
    """
    best: Optional[Tuple[str, str, datetime, bool, datetime]] = None
    best_key: Optional[Tuple[datetime, bool]] = None
    for version in versions:
        _status, bucket, row_start, is_current, _first_start = version
        if bucket == BUCKET_ARCHIVE:
            continue
        if as_of is not None and row_start >= as_of:
            continue
        key = (row_start, is_current)
        if best_key is None or key > best_key:
            best_key = key
            best = version
    return best


def _effective_state(
    versions: Iterable[Tuple[str, str, datetime, bool]],
    as_of: Optional[datetime] = None,
) -> Optional[Tuple[str, datetime]]:
    """Latest non-archive (bucket, row_start) — see ``_effective_version``."""
    version = _effective_version(versions, as_of)
    if version is None:
        return None
    return (version[1], version[2])


def _effective_status_name(
    versions: Iterable[Tuple[str, str, datetime, bool, datetime]],
    as_of: Optional[datetime],
) -> Optional[str]:
    """Raw status of the effective version — see ``_effective_version``."""
    version = _effective_version(versions, as_of)
    return version[0] if version else None


def _fetch_status_versions(
    claim_ids: List[int],
    row_start_column: str,
    as_of: Optional[datetime] = None,
) -> Dict[int, List[Tuple[str, str, datetime, bool, datetime]]]:
    """(status, bucket, last_start, is_current, first_start) versions per claim.

    ``row_start`` values are UTC (the temporal AS_ROW_START column).
    ``first_start`` is the earliest occurrence of that status — used to date
    when a claim entered the process.
    """
    versions: Dict[int, List[Tuple[str, str, datetime, bool, datetime]]] = defaultdict(list)
    chunks = list(_chunks(claim_ids, HISTORY_CHUNK_SIZE))
    for i, chunk in enumerate(chunks):
        for row in sql_repo.get_claim_status_versions(chunk, row_start_column, as_of):
            claim_id = row.get("id")
            last_start = row.get("last_start")
            if claim_id is None or not last_start:
                continue
            last_start_dt = _parse_ts(last_start)
            first_start_dt = _parse_ts(row.get("first_start") or last_start)
            status = row.get("status")
            versions[int(claim_id)].append((
                status,
                classify_claim_status(status),
                last_start_dt,
                bool(row.get("is_current")),
                first_start_dt,
            ))
        # Pace chunk reads so a full history scan can't starve the shared
        # Azure SQL (other dashboards' queries interleave between chunks).
        if i < len(chunks) - 1:
            time.sleep(_chunk_delay_seconds())
    return versions


def _process_entry_local_date(
    record: Optional[Dict[str, Any]],
    claim_versions: List[Tuple[str, str, datetime, bool, datetime]],
    local_offset: timedelta,
) -> Optional[date]:
    """Local date the claim entered the line-item process.

    AI claims: ``business_status_date`` from the Mongo record (``ai_inserted_at``
    — when the run entered AI line-item processing). Everything else: the
    initial assignment — the earliest 'Assigned' status first_start, falling
    back to the earliest version start overall. Returns None when nothing
    marks an entry into the process.
    """
    if record and record.get("business_status_date"):
        try:
            return date.fromisoformat(str(record["business_status_date"])[:10])
        except ValueError:
            pass
    if not claim_versions:
        return None
    assigned = [
        fs for status, _b, _ls, _cur, fs in claim_versions
        if status and status.strip().lower() == "assigned"
    ]
    earliest = min(assigned) if assigned else min(
        fs for _s, _b, _ls, _cur, fs in claim_versions
    )
    return (earliest + local_offset).date()


def _status_before(
    claim_versions: List[Tuple[str, str, datetime, bool, datetime]],
    moment: Optional[datetime],
) -> Optional[str]:
    """Status the claim was in right before ``moment``.

    Versions are grouped per status (first/last start), so a status that
    recurs after ``moment`` still counts via its first start.
    """
    if moment is None:
        return None
    best: Optional[Tuple[datetime, str]] = None
    for status, _b, last_start, _cur, first_start in claim_versions:
        start = last_start if last_start < moment else (
            first_start if first_start < moment else None
        )
        if start is not None and (best is None or start > best[0]):
            best = (start, status)
    return best[1] if best else None


_INCIDENT_KEY_RE = re.compile(
    r'"?(?:incident[_ ]?type|incidentType|neris[_ ]?(?:incident[_ ]?)?type)"?\s*[:=]\s*"?([^",}\n]+)',
    re.IGNORECASE,
)


def _incident_from_debug(debug: Any) -> Optional[str]:
    """True incident (NERIS) type stored in the Claims.debug column.

    The debug blob may be JSON or embedded text; look for an incident-type
    key either way. Returns None when nothing parses — callers fall back to
    the run-type label.
    """
    if not debug:
        return None
    text = debug if isinstance(debug, str) else str(debug)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        for key in (
            "incident_type", "incidentType", "incident_type_name",
            "neris_incident_type", "neris_type", "incident",
        ):
            value = parsed.get(key)
            if value:
                return str(value).strip() or None
    match = _INCIDENT_KEY_RE.search(text)
    if match:
        value = match.group(1).strip().strip('"').strip()
        return value or None
    return None


def _fetch_claim_extras(
    claim_ids: List[int],
    row_start_column: str,
    as_of: Optional[datetime] = None,
) -> Tuple[Dict[int, float], Dict[int, Dict[str, Optional[str]]], Dict[int, Dict[str, Any]], bool]:
    """Per-claim extras for the drill-down rows: line-item totals, adjusters,
    and who/when marked not-billable, plus an all-queries-ok flag. Sync;
    called via asyncio.to_thread. Each query degrades independently, and the
    flag lets the caller mark the comparison partial when any extras read
    failed so the warmer never publishes missing details as complete."""
    totals: Dict[int, float] = {}
    adjusters: Dict[int, Dict[str, Optional[str]]] = {}
    markers: Dict[int, Dict[str, Any]] = {}
    ok = True
    try:
        totals = sql_repo.get_claim_services_totals(claim_ids)
    except Exception as e:
        ok = False
        logger.error(f"claim_services totals query failed: {e}")
    try:
        adjusters = sql_repo.get_claim_adjusters(claim_ids)
    except Exception as e:
        ok = False
        logger.error(f"adjusters query failed: {e}")
    try:
        markers = sql_repo.get_not_billable_markers(claim_ids, row_start_column, as_of)
    except Exception as e:
        ok = False
        logger.error(f"not-billable markers query failed: {e}")
    return totals, adjusters, markers, ok


def _first_insurance_start(
    versions: Iterable[Tuple[str, str, datetime, bool, datetime]],
    as_of: Optional[datetime] = None,
) -> Optional[datetime]:
    """First insurance-status start within the window; ``as_of`` keeps a past
    period from reporting send dates that happened after it."""
    starts = [
        fs for status, _b, _ls, _cur, fs in versions
        if status and status.strip().lower() in INSURANCE_BILLING_STATUSES
        and (as_of is None or fs < as_of)
    ]
    return min(starts) if starts else None


class _ClaimOutcome:
    __slots__ = (
        "cohort", "subtype", "bucket", "effective_date", "ever_not_billable",
        "incident", "billing_target", "system_not_billable",
    )

    def __init__(
        self,
        cohort: str,
        subtype: Optional[str],
        bucket: Optional[str],
        effective_date: Optional[datetime],
        ever_not_billable: bool,
        incident: str,
        billing_target: str,
        system_not_billable: bool,
    ) -> None:
        self.cohort = cohort
        self.subtype = subtype
        self.bucket = bucket
        self.effective_date = effective_date
        self.ever_not_billable = ever_not_billable
        self.incident = incident
        self.billing_target = billing_target
        self.system_not_billable = system_not_billable


def _breakdown(outcomes: List[_ClaimOutcome]) -> ClaimStatusBreakdown:
    resolved = [o for o in outcomes if o.bucket is not None]
    counts = Counter(o.bucket for o in resolved)
    paid = counts[BUCKET_PAID]
    ready = counts[BUCKET_READY_TO_BE_PAID]
    denied = counts[BUCKET_DENIED]
    # `total` counts only claims that finished the process (paid, ready to
    # be paid, denied) — the same claims the rates divide by. in_progress,
    # under_review and not_billable never make up a total.
    successes = paid + ready
    final = successes + denied
    return ClaimStatusBreakdown(
        total=final,
        paid=paid,
        ready_to_be_paid=ready,
        under_review=counts[BUCKET_UNDER_REVIEW],
        in_progress=counts[BUCKET_IN_PROGRESS],
        denied=denied,
        not_billable=counts[BUCKET_NOT_BILLABLE],
        paid_rate=_pct(successes, final),
        resolved_success_rate=_pct(successes, final),
        denied_rate=_pct(denied, final),
    )


def _segments(
    outcomes: List[_ClaimOutcome],
    key_fn,
) -> List[ClaimComparisonSegment]:
    groups: Dict[str, List[_ClaimOutcome]] = defaultdict(list)
    for o in outcomes:
        groups[key_fn(o)].append(o)
    segments = []
    for label, members in groups.items():
        segments.append(ClaimComparisonSegment(
            label=label,
            overall=_breakdown(members),
            ai_released=_breakdown([m for m in members if m.cohort == COHORT_AI_RELEASED]),
            other=_breakdown([m for m in members if m.cohort == COHORT_OTHER]),
        ))
    segments.sort(key=lambda s: -s.overall.total)
    return segments


def _weekly_counts(outcomes: List[_ClaimOutcome]) -> ClaimComparisonWeeklyCounts:
    counts = Counter(o.bucket for o in outcomes)
    paid = counts[BUCKET_PAID]
    ready = counts[BUCKET_READY_TO_BE_PAID]
    denied = counts[BUCKET_DENIED]
    return ClaimComparisonWeeklyCounts(
        paid=paid,
        ready_to_be_paid=ready,
        denied=denied,
        resolved_success_rate=_pct(paid + ready, paid + ready + denied),
    )


def _weekly_series(
    outcomes: List[_ClaimOutcome],
    start: date,
    end: date,
) -> List[ClaimComparisonWeek]:
    week_starts: Dict[str, date] = {}
    day = start
    while day <= end:
        key = iso_week_key(datetime(day.year, day.month, day.day))
        week_starts.setdefault(key, day - timedelta(days=day.weekday()))
        day += timedelta(days=1)

    by_week: Dict[str, List[_ClaimOutcome]] = defaultdict(list)
    # effective_date is a UTC row_start; bucket it by the org-local week so
    # week keys match the local-date week_starts built above.
    local_offset = timedelta(hours=settings.AI_LOCAL_UTC_OFFSET_HOURS)
    for o in outcomes:
        if o.bucket in FINAL_BUCKETS and o.effective_date is not None:
            key = iso_week_key(o.effective_date + local_offset)
            if key in week_starts:
                by_week[key].append(o)

    series = []
    for key, week_start in week_starts.items():
        members = by_week.get(key, [])
        series.append(ClaimComparisonWeek(
            week=key,
            week_start=week_start.isoformat(),
            ai_released=_weekly_counts([m for m in members if m.cohort == COHORT_AI_RELEASED]),
            other=_weekly_counts([m for m in members if m.cohort == COHORT_OTHER]),
            other_manual=_weekly_counts(
                [m for m in members if m.cohort == COHORT_OTHER and m.subtype == SUBTYPE_MANUAL]
            ),
            other_ai_rejected=_weekly_counts(
                [m for m in members if m.cohort == COHORT_OTHER and m.subtype == SUBTYPE_AI_REJECTED]
            ),
        ))
    return series


def _not_billable_cohort(outcomes: List[_ClaimOutcome]) -> ClaimComparisonNotBillableCohort:
    flagged = [o for o in outcomes if o.ever_not_billable]
    dist = Counter(o.bucket for o in flagged if o.bucket is not None)
    ready_or_paid = dist[BUCKET_READY_TO_BE_PAID] + dist[BUCKET_PAID]
    denied = dist[BUCKET_DENIED]
    return ClaimComparisonNotBillableCohort(
        ever_not_billable=len(flagged),
        effective_status=dict(dist),
        ready_or_paid=ready_or_paid,
        denied=denied,
        ready_vs_denied_rate=_pct(ready_or_paid, ready_or_paid + denied),
    )


def _assemble(
    start: date,
    end: date,
    outcomes: List[_ClaimOutcome],
    ai_pending: int,
    deleted_runs: int,
    unmapped: Counter,
    resolved_columns: Dict[str, Optional[str]],
    source_status: Dict[str, str],
    data_complete: bool,
    claims: Optional[List[ClaimComparisonClaim]] = None,
) -> AiClaimComparisonResponse:
    released = [o for o in outcomes if o.cohort == COHORT_AI_RELEASED]
    other = [o for o in outcomes if o.cohort == COHORT_OTHER]
    return AiClaimComparisonResponse(
        period_start=start.isoformat(),
        period_end=end.isoformat(),
        overall=_breakdown(outcomes),
        ai_released=_breakdown(released),
        other=_breakdown(other),
        other_manual=_breakdown([o for o in other if o.subtype == SUBTYPE_MANUAL]),
        other_ai_rejected=_breakdown([o for o in other if o.subtype == SUBTYPE_AI_REJECTED]),
        ai_pending=ai_pending,
        deleted_runs=deleted_runs,
        unresolved_claims=sum(1 for o in outcomes if o.bucket is None),
        weekly=_weekly_series(outcomes, start, end),
        by_incident_type=_segments(outcomes, lambda o: o.incident),
        by_billing_target=_segments(
            outcomes, lambda o: billing_target_label(o.billing_target),
        ),
        claims=claims or [],
        not_billable=ClaimComparisonNotBillable(
            ai_released=_not_billable_cohort(released),
            other=_not_billable_cohort(other),
            marked_by_processor=sum(1 for o in outcomes if o.ever_not_billable),
            identified_by_system=sum(1 for o in outcomes if o.system_not_billable),
        ),
        unmapped_statuses=dict(unmapped.most_common()),
        resolved_columns=resolved_columns,
        source_status=source_status,
        data_complete=data_complete,
    )


def _empty_result(
    start: date,
    end: date,
    source_status: Dict[str, str],
) -> AiClaimComparisonResponse:
    return _assemble(
        start, end, [], 0, 0, Counter(), {}, source_status, data_complete=False,
    )


async def _compute_claim_payment_comparison(
    ai_db,
    filters: AiAnalyticsFilters,
) -> AiClaimComparisonResponse:
    """Compare final payment outcomes of AI-released vs other runs."""
    if not filters.start_date or not filters.end_date:
        raise ValueError("start_date and end_date are required")
    start = date.fromisoformat(filters.start_date)
    end = date.fromisoformat(filters.end_date)
    if end < start:
        raise ValueError("end_date must be on or after start_date")

    source_status: Dict[str, str] = {"recoveryhub_sql": "available"}

    try:
        row_start_column = await asyncio.to_thread(sql_repo.get_claims_row_start_column)
        scope_rows = await asyncio.to_thread(
            sql_repo.get_claim_comparison_scope,
            filters.start_date,
            filters.end_date,
            filters.department_id,
            row_start_column,
            datetime(start.year, start.month, start.day)
            - timedelta(hours=settings.AI_LOCAL_UTC_OFFSET_HOURS),
            datetime(end.year, end.month, end.day)
            + timedelta(days=1)
            - timedelta(hours=settings.AI_LOCAL_UTC_OFFSET_HOURS),
        )
    except Exception as e:
        logger.error(f"Claim comparison SQL scope query failed: {e}")
        source_status["recoveryhub_sql"] = "unavailable"
        return _empty_result(start, end, source_status)

    records, ai_source_status, data_complete = await _load_normalized_cohort(
        ai_db,
        AiAnalyticsFilters(
            start_date=filters.start_date,
            end_date=filters.end_date,
            department_id=filters.department_id,
        ),
    )
    source_status.update(ai_source_status)
    if ai_source_status.get("recoveryhub_ai_mongo") == "unavailable":
        # Without the AI cohort every run would classify as manual, which
        # inflates the manual cohort with AI-released runs. Report the
        # comparison as incomplete instead of returning wrong rates.
        logger.error(
            "Claim comparison AI cohort unavailable; returning incomplete result"
        )
        return _empty_result(start, end, source_status)
    ai_by_claim = {
        int(r["claim_id"]): r for r in records
        if r.get("claim_id") and not _dept_excluded(r)
    }

    # Candidates = SQL scope rows ∪ AI-cohort claims. An AI-processed claim
    # qualifies on its ai_inserted_at alone — submission date is irrelevant.
    scope_by_claim: Dict[int, Dict[str, Any]] = {
        int(row["id"]): row for row in scope_rows if row.get("id") is not None
    }
    candidate_ids = set(scope_by_claim) | set(ai_by_claim)

    # Cap everything at the end of the local window (inclusive dates).
    # Row starts are UTC, so the local end-of-window is converted to UTC the
    # same way _utc_window_for_local_dates does: UTC = local - offset.
    as_of_local = datetime(end.year, end.month, end.day) + timedelta(days=1)
    local_offset = timedelta(hours=settings.AI_LOCAL_UTC_OFFSET_HOURS)
    as_of = as_of_local - local_offset

    try:
        versions = await asyncio.to_thread(
            _fetch_status_versions,
            sorted(candidate_ids),
            row_start_column,
            as_of,
        )
    except Exception as e:
        logger.error(f"Claim comparison status history query failed: {e}")
        source_status["recoveryhub_sql"] = "unavailable"
        return _empty_result(start, end, source_status)

    # Payload gate: a claim only counts if it entered the process inside the
    # window — AI claims by their ai_inserted_at (line-item creation date),
    # the rest by their initial assignment date (earliest 'Assigned' version,
    # falling back to the earliest version overall). date_of_submitted does
    # not gate anything.
    tracked: List[Tuple[int, str, Optional[str], Optional[Dict[str, Any]], Dict[str, Any]]] = []
    ai_pending = 0
    for claim_id in sorted(candidate_ids):
        row = scope_by_claim.get(claim_id, {"id": claim_id, "type_id": None, "type_name": None})
        record = ai_by_claim.get(claim_id)
        entry = _process_entry_local_date(record, versions.get(claim_id, []), local_offset)
        if entry is None or entry < start or entry > end:
            continue
        cohort, subtype = _cohort_for(record)
        if cohort == COHORT_AI_PENDING:
            ai_pending += 1
            continue
        tracked.append((claim_id, cohort, subtype, record, row))

    # Unmapped statuses are only reported for claims that survived the
    # entry-date gate — a claim outside the payload must not produce a
    # status warning.
    unmapped: Counter = Counter()
    for claim_id, _c, _st, _r, _row in tracked:
        for status, _b, last_start, _cur, _fs in versions.get(claim_id, []):
            if not is_mapped_claim_status(status) and last_start < as_of:
                unmapped[status or "(null)"] += 1

    deleted_runs = 0
    try:
        deleted_runs = await asyncio.to_thread(
            sql_repo.get_deleted_runs_count,
            filters.start_date,
            filters.end_date,
            filters.department_id,
        )
    except Exception as e:
        logger.error(f"Claim comparison deleted-runs query failed: {e}")
        source_status["recoveryhub_sql"] = "partial"
        data_complete = False

    tracked_ids = [t[0] for t in tracked]
    services_totals: Dict[int, float] = {}
    adjusters: Dict[int, Dict[str, Optional[str]]] = {}
    nb_markers: Dict[int, Dict[str, Any]] = {}
    try:
        services_totals, adjusters, nb_markers, extras_ok = await asyncio.to_thread(
            _fetch_claim_extras, tracked_ids, row_start_column, as_of,
        )
        if not extras_ok:
            source_status["recoveryhub_sql"] = "partial"
            data_complete = False
    except Exception as e:
        logger.error(f"Claim comparison claim-extras query failed: {e}")
        source_status["recoveryhub_sql"] = "partial"
        data_complete = False

    outcomes: List[_ClaimOutcome] = []
    claim_rows: List[ClaimComparisonClaim] = []
    for claim_id, cohort, subtype, record, row in tracked:
        claim_versions = versions.get(claim_id, [])
        effective = _effective_state(claim_versions, as_of)
        ever_nb = any(
            b == BUCKET_NOT_BILLABLE
            for _, b, row_start, _, _fs in claim_versions
            if row_start < as_of
        )
        system_not_billable = record is not None and classify_billability(
            billing_category=None,
            is_billable=record.get("is_billable"),
        )["not_billable"]
        incident = (
            _incident_from_debug(row.get("debug"))
            or incident_type_label(row.get("type_id"), row.get("type_name"))
        )
        billing_target = billing_target_from_statuses(
            (status, last_start, is_current)
            for status, _, last_start, is_current, _fs in claim_versions
            if last_start < as_of
        )
        outcomes.append(_ClaimOutcome(
            cohort=cohort,
            subtype=subtype,
            bucket=effective[0] if effective else None,
            effective_date=effective[1] if effective else None,
            ever_not_billable=ever_nb,
            incident=incident,
            billing_target=billing_target,
            system_not_billable=bool(system_not_billable),
        ))

        # Drill-down row
        adjuster = adjusters.get(claim_id, {})
        marker = nb_markers.get(claim_id, {})
        submitted_dt = _parse_ts(row["date_of_submitted"]) if row.get("date_of_submitted") else None
        sent_insurance = _first_insurance_start(claim_versions, as_of)
        days_to_insurance = (
            round((sent_insurance - submitted_dt).total_seconds() / 86400, 1)
            if sent_insurance and submitted_dt else None
        )
        dept_billing = row.get("dept_incidents_billing")
        try:
            dept_allows_direct = bool(int(dept_billing)) if dept_billing is not None else None
        except (TypeError, ValueError):
            dept_allows_direct = None
        marked_at_dt = _parse_ts(marker["marked_at"]) if marker.get("marked_at") else None
        prior_status = _status_before(claim_versions, marked_at_dt)
        claim_rows.append(ClaimComparisonClaim(
            claim_id=claim_id,
            run_number=row.get("run_number"),
            cohort=cohort,
            subtype=subtype,
            department_id=row.get("dept_id"),
            department_name=row.get("department_name"),
            department_allows_direct_billing=dept_allows_direct,
            incident=incident,
            status=_effective_status_name(claim_versions, as_of),
            bucket=effective[0] if effective else None,
            billing_target=billing_target,
            insurance_company=adjuster.get("insurance_company"),
            adjuster_name=adjuster.get("adjuster_name"),
            invoice_number=row.get("invoice_number"),
            amount_invoiced=row.get("amount_invoiced"),
            ai_invoice_total=(record or {}).get("invoice_total"),
            services_total=services_totals.get(claim_id),
            ever_not_billable=ever_nb,
            not_billable_by=marker.get("user_id"),
            not_billable_at=marked_at_dt.isoformat() if marked_at_dt else None,
            status_when_not_billable=prior_status,
            submitted_at=submitted_dt.isoformat() if submitted_dt else None,
            sent_to_insurance_at=sent_insurance.isoformat() if sent_insurance else None,
            days_submitted_to_insurance=days_to_insurance,
        ))

    resolved_columns: Dict[str, Optional[str]] = {
        "claims_row_start": row_start_column,
    }
    return _assemble(
        start, end, outcomes, ai_pending, deleted_runs, unmapped,
        resolved_columns, source_status, data_complete, claims=claim_rows,
    )


@cached(ttl=CACHE_TTL_SECONDS, func_name="get_claim_payment_comparison")
async def get_claim_payment_comparison(
    ai_db,
    filters: AiAnalyticsFilters,
) -> AiClaimComparisonResponse:
    """Cached wrapper over ``_compute_claim_payment_comparison``."""
    return await _compute_claim_payment_comparison(ai_db, filters)


def _chunk_delay_seconds() -> float:
    """Yield between chunked history reads; CLAIM_COMPARISON_CHUNK_DELAY_MS, default 100."""
    try:
        return max(0.0, float(os.environ.get("CLAIM_COMPARISON_CHUNK_DELAY_MS", "100")) / 1000)
    except ValueError:
        logger.warning("Invalid CLAIM_COMPARISON_CHUNK_DELAY_MS; using 100")
        return 0.1


def _warm_interval_seconds() -> int:
    try:
        return int(os.environ.get("CLAIM_COMPARISON_WARM_INTERVAL_SECONDS", "1800"))
    except ValueError:
        logger.warning(
            "Invalid CLAIM_COMPARISON_WARM_INTERVAL_SECONDS; using 1800"
        )
        return 1800


# Mongo lease serializing warm scans across uvicorn workers: each process
# runs its own warmer (its in-memory cache needs priming too), but the
# lease lets only one process run the heavy history scan at a time.
WARM_RUN_LOCK_ID = "comparison_warm_run"
# Long enough to cover the worst-case warm (~230 s); if the holder dies the
# lease lapses and a peer resumes scanning instead of stalling forever.
_WARM_RUN_LEASE_SECONDS = 900
_WARM_HOLDER_ID = (
    f"{socket.gethostname()}:{os.getpid()}:warm:{uuid.uuid4().hex[:8]}"
)

# True while the heavy history read is (or may still be) running on a
# thread. Set before the compute await and cleared only when it returns
# normally or raises — on cancellation the thread outlives the task, so it
# stays True and the warm lease is left to lapse on its own.
_WARM_SCAN_RUNNING = False


# Worker-state doc the lease holder publishes its completed result to, so
# non-holder processes can prime their own in-memory caches without
# running the history scan again.
WARM_RESULT_DOC_ID = "comparison_warm_result"


async def _warm_once(ai_db) -> None:
    """Recompute the default window and overwrite its cache entry.

    Refreshes unconditionally: a plain call would hit the still-valid cached
    entry and never extend its expiry, leaving a cold-recompute window where
    the ~230 s history read exceeds the 180 s proxy timeout.
    """
    if not await try_acquire_lease(
        ai_db, WARM_RUN_LOCK_ID, _WARM_HOLDER_ID, _WARM_RUN_LEASE_SECONDS
    ):
        # Another worker is scanning (or recently did): prime this process's
        # cache from the published result instead of a second scan.
        await _prime_from_shared_result(ai_db)
        return
    try:
        await _warm_once_uncached(ai_db)
    except asyncio.CancelledError:
        # Only keep the lease when the history scan may still be running on
        # its thread — releasing then would let a peer start a concurrent
        # scan. Cancelled before/after the scan (e.g. mid-publish) releases
        # normally so replacement workers aren't blocked by a dead holder.
        if not _WARM_SCAN_RUNNING:
            await release_lease(ai_db, WARM_RUN_LOCK_ID, _WARM_HOLDER_ID)
        raise
    except Exception:
        await release_lease(ai_db, WARM_RUN_LOCK_ID, _WARM_HOLDER_ID)
        raise
    else:
        await release_lease(ai_db, WARM_RUN_LOCK_ID, _WARM_HOLDER_ID)


async def _prime_from_shared_result(ai_db) -> None:
    """Prime this process's cache from the result the lease holder published."""
    try:
        doc = await ai_db[worker_config.WORKER_STATE_COLLECTION].find_one(
            {"_id": WARM_RESULT_DOC_ID}
        )
        if not doc or doc.get("value") is None:
            logger.info(
                "Claim comparison warm deferred; another worker is scanning "
                "and no shared result exists yet"
            )
            return
        computed_at = doc["computed_at"]
        if computed_at.tzinfo is None:
            computed_at = computed_at.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - computed_at).total_seconds()
        if age >= CACHE_TTL_SECONDS:
            logger.warning(
                "Shared claim comparison result is stale (%ds old); skipping warm",
                int(age),
            )
            return
        start, end = await asyncio.to_thread(resolve_comparison_window, None)
        payload = doc["value"]
        if (
            payload.get("period_start") != start
            or payload.get("period_end") != end
        ):
            # The shared result covers a different window (e.g. computed
            # yesterday, read after midnight) — never prime it under
            # today's cache key.
            logger.info(
                "Shared claim comparison result is for %s..%s, not %s..%s; skipping prime",
                payload.get("period_start"), payload.get("period_end"),
                start, end,
            )
            return
        filters = AiAnalyticsFilters(start_date=start, end_date=end,
                                     department_id=None)
        # Remaining lifetime only: the primed entry must die at the
        # original computed_at + TTL, not get a fresh full TTL.
        prime_cache(
            "get_claim_payment_comparison",
            (ai_db, filters),
            {},
            AiClaimComparisonResponse.model_validate(payload),
            CACHE_TTL_SECONDS - age,
            computed_at=computed_at.timestamp(),
        )
        logger.info("Claim comparison cache primed from shared result")
    except Exception as e:
        logger.warning(f"Shared claim comparison prime failed: {e}")


async def _warm_once_uncached(ai_db) -> None:
    global _WARM_SCAN_RUNNING
    start, end = await asyncio.to_thread(resolve_comparison_window, None)
    filters = AiAnalyticsFilters(
        start_date=start,
        end_date=end,
        department_id=None,
    )
    _WARM_SCAN_RUNNING = True
    try:
        value = await _compute_claim_payment_comparison(ai_db, filters)
    except asyncio.CancelledError:
        raise  # keep _WARM_SCAN_RUNNING: the to_thread read outlives the task
    except Exception:
        _WARM_SCAN_RUNNING = False
        raise
    else:
        _WARM_SCAN_RUNNING = False
    if not value.data_complete:
        # Don't overwrite the last good entry with an empty result from a
        # transient outage — and extend its expiry so requests keep being
        # served from cache instead of triggering a cold history read that
        # would exceed the proxy timeout before the next warm runs.
        if touch_cache(
            "get_claim_payment_comparison",
            (ai_db, filters),
            {},
            CACHE_TTL_SECONDS,
        ):
            logger.warning(
                "Claim comparison warm produced an incomplete result; extended previous cache entry"
            )
        else:
            logger.warning(
                "Claim comparison warm produced an incomplete result; no cache entry to keep"
            )
        return
    prime_cache(
        "get_claim_payment_comparison",
        (ai_db, filters),
        {},
        value,
        CACHE_TTL_SECONDS,
    )
    # Publish so the other uvicorn workers can prime their own caches
    # without rerunning the history scan.
    try:
        await ai_db[worker_config.WORKER_STATE_COLLECTION].replace_one(
            {"_id": WARM_RESULT_DOC_ID},
            {
                "_id": WARM_RESULT_DOC_ID,
                "computed_at": datetime.now(UTC),
                "holder_id": _WARM_HOLDER_ID,
                "value": value.model_dump(),
            },
            upsert=True,
        )
    except Exception as e:
        logger.warning(f"Publishing shared claim comparison result failed: {e}")


async def warm_claim_comparison_cache(
    ai_db,
    interval_seconds: Optional[int] = None,
) -> None:
    """Keep the default-window comparison cached so requests never recompute.

    A cold history read takes about 230 s, longer than the proxy's 180 s
    timeout, so the first request after an idle period would return a 504.
    Refreshing the cache entry on a schedule keeps both the app cache and the
    SQL buffer cache warm. ``interval_seconds`` defaults to
    CLAIM_COMPARISON_WARM_INTERVAL_SECONDS (600); 0 disables warming.
    """
    if interval_seconds is None:
        interval_seconds = _warm_interval_seconds()
    if interval_seconds <= 0:
        logger.info("Claim comparison cache warming disabled")
        return
    while True:
        try:
            await _warm_once(ai_db)
            logger.info("Claim comparison cache warmed")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Claim comparison cache warm failed: {e}")
        await asyncio.sleep(interval_seconds)


async def get_claim_comparison_schema_check(
    start_date: str,
    end_date: str,
) -> AiClaimComparisonSchemaCheck:
    """Show the raw values the editable config is matched against.

    Used to verify the status buckets and run-type labels before trusting the
    comparison numbers.
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    row_start_column = await asyncio.to_thread(sql_repo.get_claims_row_start_column)

    status_rows = await asyncio.to_thread(
        sql_repo.get_scope_status_counts, start_date, end_date,
    )
    run_type_rows = await asyncio.to_thread(
        sql_repo.get_run_type_values, start_date, end_date,
    )
    deleted_columns = await asyncio.to_thread(
        sql_repo.get_table_column_names, "claims_deleted",
    )

    return AiClaimComparisonSchemaCheck(
        period_start=start.isoformat(),
        period_end=end.isoformat(),
        status_values=[
            ClaimStatusValueCheck(
                status=row.get("status"),
                claim_count=int(row.get("claim_count") or 0),
                bucket=classify_claim_status(row.get("status")),
                mapped=is_mapped_claim_status(row.get("status")),
            )
            for row in status_rows
        ],
        run_type_values=[
            RunTypeValueCheck(
                type_id=row.get("type_id"),
                type_name=row.get("type_name"),
                label=incident_type_label(row.get("type_id"), row.get("type_name")),
                claim_count=int(row.get("claim_count") or 0),
            )
            for row in run_type_rows
        ],
        resolved_columns={"claims_row_start": row_start_column},
        claims_deleted_columns=deleted_columns,
        source_status={"recoveryhub_sql": "available"},
        data_complete=True,
    )
