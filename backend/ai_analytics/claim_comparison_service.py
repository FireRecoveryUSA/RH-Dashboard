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

import logging
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from .models import (
    AiAnalyticsFilters,
    AiClaimComparisonResponse,
    AiClaimComparisonSchemaCheck,
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
from .normalization_core import classify_billability
from .claim_status_config import (
    BUCKET_ARCHIVE,
    BUCKET_DENIED,
    BUCKET_IN_PROGRESS,
    BUCKET_NOT_BILLABLE,
    BUCKET_PAID,
    BUCKET_READY_TO_BE_PAID,
    BUCKET_UNDER_REVIEW,
    RATED_BUCKETS,
    billing_target_from_statuses,
    billing_target_label,
    classify_claim_status,
    incident_type_label,
    is_mapped_claim_status,
)
from .cache import cached

logger = logging.getLogger(__name__)

HISTORY_CHUNK_SIZE = 2000

COHORT_AI_RELEASED = "ai_released"
COHORT_OTHER = "other"
COHORT_AI_PENDING = "ai_pending"
SUBTYPE_MANUAL = "manual"
SUBTYPE_AI_REJECTED = "ai_rejected"

FINAL_BUCKETS = (BUCKET_PAID, BUCKET_READY_TO_BE_PAID, BUCKET_DENIED)


def _pct(numerator: int, denominator: int) -> Optional[float]:
    if denominator == 0:
        return None
    return round(numerator / denominator * 100, 2)


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def resolve_comparison_window(end_date: Optional[str]) -> Tuple[str, str]:
    """Fixed window: Jan 1 of the SQL server's year through ``end_date`` (default today)."""
    today = date.fromisoformat(sql_repo.get_server_date())
    start = date(today.year, 1, 1)
    return start.isoformat(), end_date or today.isoformat()


def _chunks(items: List[int], size: int) -> Iterator[List[int]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _cohort_for(record: Optional[Dict[str, Any]]) -> Tuple[str, Optional[str]]:
    if record is None:
        return COHORT_OTHER, SUBTYPE_MANUAL
    outcome = record.get("business_outcome")
    if outcome == "released":
        return COHORT_AI_RELEASED, None
    if outcome == "cancelled_rejected":
        return COHORT_OTHER, SUBTYPE_AI_REJECTED
    return COHORT_AI_PENDING, None


def _effective_state(
    versions: Iterable[Tuple[str, str, datetime, bool]],
) -> Optional[Tuple[str, datetime]]:
    """Latest non-archive (bucket, row_start) from (status, bucket, row_start, is_current).

    When two statuses share the same latest row start, the live Claims row wins:
    it is the authoritative current state. Without this, the choice depends on
    SQL row order and changes between requests.
    """
    best: Optional[Tuple[str, datetime]] = None
    best_key: Optional[Tuple[datetime, bool]] = None
    for _status, bucket, row_start, is_current in versions:
        if bucket == BUCKET_ARCHIVE:
            continue
        key = (row_start, is_current)
        if best_key is None or key > best_key:
            best_key = key
            best = (bucket, row_start)
    return best


def _fetch_status_versions(
    claim_ids: List[int],
    row_start_column: str,
) -> Tuple[Dict[int, List[Tuple[str, str, datetime, bool]]], Counter]:
    """(status, bucket, row_start, is_current) versions per claim, plus unmapped status counts."""
    versions: Dict[int, List[Tuple[str, str, datetime, bool]]] = defaultdict(list)
    unmapped: Counter = Counter()
    for chunk in _chunks(claim_ids, HISTORY_CHUNK_SIZE):
        for row in sql_repo.get_claim_status_versions(chunk, row_start_column):
            claim_id = row.get("id")
            last_start = row.get("last_start")
            if claim_id is None or not last_start:
                continue
            status = row.get("status")
            if not is_mapped_claim_status(status):
                unmapped[status or "(null)"] += 1
            versions[int(claim_id)].append((
                status,
                classify_claim_status(status),
                _parse_ts(last_start),
                bool(row.get("is_current")),
            ))
    return versions, unmapped


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
    rated = sum(counts[b] for b in RATED_BUCKETS)
    successes = paid + ready
    final = successes + denied
    return ClaimStatusBreakdown(
        total=len(resolved),
        paid=paid,
        ready_to_be_paid=ready,
        under_review=counts[BUCKET_UNDER_REVIEW],
        in_progress=counts[BUCKET_IN_PROGRESS],
        denied=denied,
        not_billable=counts[BUCKET_NOT_BILLABLE],
        paid_rate=_pct(paid, rated),
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
    for o in outcomes:
        if o.bucket in FINAL_BUCKETS and o.effective_date is not None:
            key = iso_week_key(o.effective_date)
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


@cached(ttl=300, func_name="get_claim_payment_comparison")
async def get_claim_payment_comparison(
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
        row_start_column = sql_repo.get_claims_row_start_column()
        scope_rows = sql_repo.get_claim_comparison_scope(
            filters.start_date,
            filters.end_date,
            filters.department_id,
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
    ai_by_claim = {int(r["claim_id"]): r for r in records if r.get("claim_id")}

    tracked: List[Tuple[int, str, Optional[str], Optional[Dict[str, Any]], Dict[str, Any]]] = []
    ai_pending = 0
    for row in scope_rows:
        if row.get("id") is None:
            continue
        claim_id = int(row["id"])
        record = ai_by_claim.get(claim_id)
        cohort, subtype = _cohort_for(record)
        if cohort == COHORT_AI_PENDING:
            ai_pending += 1
            continue
        tracked.append((claim_id, cohort, subtype, record, row))

    try:
        versions, unmapped = _fetch_status_versions(
            [t[0] for t in tracked], row_start_column,
        )
    except Exception as e:
        logger.error(f"Claim comparison status history query failed: {e}")
        source_status["recoveryhub_sql"] = "unavailable"
        return _empty_result(start, end, source_status)

    deleted_runs = 0
    try:
        deleted_runs = sql_repo.get_deleted_runs_count(
            filters.start_date, filters.end_date, filters.department_id,
        )
    except Exception as e:
        logger.error(f"Claim comparison deleted-runs query failed: {e}")
        source_status["recoveryhub_sql"] = "partial"
        data_complete = False

    outcomes: List[_ClaimOutcome] = []
    for claim_id, cohort, subtype, record, row in tracked:
        claim_versions = versions.get(claim_id, [])
        effective = _effective_state(claim_versions)
        system_not_billable = record is not None and classify_billability(
            billing_category=None,
            is_billable=record.get("is_billable"),
        )["not_billable"]
        outcomes.append(_ClaimOutcome(
            cohort=cohort,
            subtype=subtype,
            bucket=effective[0] if effective else None,
            effective_date=effective[1] if effective else None,
            ever_not_billable=any(b == BUCKET_NOT_BILLABLE for _, b, _, _ in claim_versions),
            incident=incident_type_label(row.get("type_id"), row.get("type_name")),
            billing_target=billing_target_from_statuses(
                (status, last_start) for status, _, last_start, _ in claim_versions
            ),
            system_not_billable=bool(system_not_billable),
        ))

    resolved_columns: Dict[str, Optional[str]] = {
        "claims_row_start": row_start_column,
    }
    return _assemble(
        start, end, outcomes, ai_pending, deleted_runs, unmapped,
        resolved_columns, source_status, data_complete,
    )


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
    row_start_column = sql_repo.get_claims_row_start_column()

    status_rows = sql_repo.get_scope_status_counts(start_date, end_date)
    run_type_rows = sql_repo.get_run_type_values(start_date, end_date)
    deleted_columns = sql_repo.get_table_column_names("claims_deleted")

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
