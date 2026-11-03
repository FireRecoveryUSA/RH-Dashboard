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
import logging
import os
import time
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
from config import settings
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
    as_of: Optional[datetime] = None,
) -> Optional[Tuple[str, datetime]]:
    """Latest non-archive (bucket, row_start) from (status, bucket, row_start, is_current).

    ``as_of`` caps the state at the end of the comparison window: versions
    recorded after it are ignored, so a past window never includes future
    payment outcomes.

    When two statuses share the same latest row start, the live Claims row wins:
    it is the authoritative current state. Without this, the choice depends on
    SQL row order and changes between requests.
    """
    best: Optional[Tuple[str, datetime]] = None
    best_key: Optional[Tuple[datetime, bool]] = None
    for _status, bucket, row_start, is_current in versions:
        if bucket == BUCKET_ARCHIVE:
            continue
        if as_of is not None and row_start >= as_of:
            continue
        key = (row_start, is_current)
        if best_key is None or key > best_key:
            best_key = key
            best = (bucket, row_start)
    return best


def _fetch_status_versions(
    claim_ids: List[int],
    row_start_column: str,
    as_of: Optional[datetime] = None,
) -> Tuple[Dict[int, List[Tuple[str, str, datetime, bool]]], Counter]:
    """(status, bucket, row_start, is_current) versions per claim, plus unmapped status counts.

    ``row_start`` values are UTC (the temporal AS_ROW_START column). Unmapped
    raw statuses are only counted for versions before ``as_of`` so a past
    window never reports post-window values.
    """
    versions: Dict[int, List[Tuple[str, str, datetime, bool]]] = defaultdict(list)
    unmapped: Counter = Counter()
    chunks = list(_chunks(claim_ids, HISTORY_CHUNK_SIZE))
    for i, chunk in enumerate(chunks):
        for row in sql_repo.get_claim_status_versions(chunk, row_start_column, as_of):
            claim_id = row.get("id")
            last_start = row.get("last_start")
            if claim_id is None or not last_start:
                continue
            last_start_dt = _parse_ts(last_start)
            status = row.get("status")
            if not is_mapped_claim_status(status):
                if as_of is None or last_start_dt < as_of:
                    unmapped[status or "(null)"] += 1
            versions[int(claim_id)].append((
                status,
                classify_claim_status(status),
                last_start_dt,
                bool(row.get("is_current")),
            ))
        # Pace chunk reads so a full history scan can't starve the shared
        # Azure SQL (other dashboards' queries interleave between chunks).
        if i < len(chunks) - 1:
            time.sleep(_chunk_delay_seconds())
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

    # Cap everything at the end of the local window (inclusive dates).
    # Row starts are UTC, so the local end-of-window is converted to UTC the
    # same way _utc_window_for_local_dates does: UTC = local - offset.
    as_of_local = datetime(end.year, end.month, end.day) + timedelta(days=1)
    as_of = as_of_local - timedelta(hours=settings.AI_LOCAL_UTC_OFFSET_HOURS)

    try:
        versions, unmapped = await asyncio.to_thread(
            _fetch_status_versions,
            [t[0] for t in tracked],
            row_start_column,
            as_of,
        )
    except Exception as e:
        logger.error(f"Claim comparison status history query failed: {e}")
        source_status["recoveryhub_sql"] = "unavailable"
        return _empty_result(start, end, source_status)

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

    outcomes: List[_ClaimOutcome] = []
    for claim_id, cohort, subtype, record, row in tracked:
        claim_versions = versions.get(claim_id, [])
        effective = _effective_state(claim_versions, as_of)
        system_not_billable = record is not None and classify_billability(
            billing_category=None,
            is_billable=record.get("is_billable"),
        )["not_billable"]
        outcomes.append(_ClaimOutcome(
            cohort=cohort,
            subtype=subtype,
            bucket=effective[0] if effective else None,
            effective_date=effective[1] if effective else None,
            ever_not_billable=any(
                b == BUCKET_NOT_BILLABLE
                for _, b, row_start, _ in claim_versions
                if row_start < as_of
            ),
            incident=incident_type_label(row.get("type_id"), row.get("type_name")),
            billing_target=billing_target_from_statuses(
                (status, last_start, is_current)
                for status, _, last_start, is_current in claim_versions
                if last_start < as_of
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


async def _warm_once(ai_db) -> None:
    """Recompute the default window and overwrite its cache entry.

    Refreshes unconditionally: a plain call would hit the still-valid cached
    entry and never extend its expiry, leaving a cold-recompute window where
    the ~230 s history read exceeds the 180 s proxy timeout.
    """
    start, end = await asyncio.to_thread(resolve_comparison_window, None)
    filters = AiAnalyticsFilters(
        start_date=start,
        end_date=end,
        department_id=None,
    )
    value = await _compute_claim_payment_comparison(ai_db, filters)
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
