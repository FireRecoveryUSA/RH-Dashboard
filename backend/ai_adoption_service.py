"""Service layer for the AI Adoption report.

Queries the RecoveryHub SQL Server for department submission counts and
joins them with AI configuration status to produce an adoption summary:
how many departments use AI, how many drafts flow through AI, and what
the coverage gap is. Results are returned as ``AiAdoptionResponse``.
"""
import json
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from models import DashboardFilters
from target_db import target_db
from ai_analytics.fee_schedule import FEE_CATALOG_VERSION, compact_fee_catalog
from ai_analytics.normalization_core import AI_SEND_OPTIONS, classify_fees

logger = logging.getLogger(__name__)

AI_FEES_COLLECTION = "department_fees_resources"

# Backward-compatible alias — the fee classifier lives in
# ai_analytics.normalization_core (single source of truth).
_classify_fees = classify_fees

# Rural Metro contract departments are excluded from this ranking.
EXCLUDED_DEPARTMENT_IDS = {1136, 2198, 2627, 2628, 2629}


class AiAdoptionResult(BaseModel):
    """Single department AI adoption + activity record."""
    rank_overall: int
    department_id: str
    department_name: Optional[str]
    state: Optional[str]
    submitted_drafts: int
    percent_of_total_volume: float
    ai_status: str
    ai_mode: str
    qualifying_fee_count: int = 0
    has_auto: bool = False
    has_queued: bool = False
    has_limited_auto: bool = False


class AiAdoptionSummary(BaseModel):
    active_departments: int
    departments_using_ai: int
    departments_not_using_ai: int
    departments_unknown: int
    total_drafts: int
    ai_department_drafts: int
    non_ai_department_drafts: int
    unknown_department_drafts: int
    ai_coverage_percent: float
    remaining_opportunity_percent: float


class AiAdoptionResponse(BaseModel):
    period: Dict[str, str]
    ai_status_basis: str
    summary: AiAdoptionSummary
    departments: List[AiAdoptionResult]


async def get_ai_participation_map(
    ai_db,
    department_ids: Optional[List[int]] = None,
) -> Optional[Dict[int, Dict[str, Any]]]:
    """Fetch current AI participation for the requested department IDs.

    Returns ``None`` when the AI Mongo source cannot be reached so that
    failures are not misclassified as ``not_using_ai``.

    Reads every document with a finalized ``fees_resources_final`` array —
    not only departments with a qualifying tile — so the result also carries
    each department's compact fee catalog (``fee_catalog``) for step-1
    result matching. Departments that legitimately have no qualifying tile
    appear with ``uses_ai=False``; departments whose finalized schedules
    conflict (multiple differing documents) get ``uses_ai=None`` /
    ``ai_mode='unknown'`` / ``fee_catalog=None`` rather than an arbitrary
    pick. Metadata-only documents (no ``fees_resources_final`` array) are
    excluded by the ``$type`` filter and can never overwrite a finalized
    schedule.
    """
    query: Dict[str, Any] = {"fees_resources_final": {"$type": "array"}}
    if department_ids is not None:
        ids = [int(d) for d in department_ids if d is not None]
        query["department_id"] = {"$in": ids + [str(d) for d in ids]}
    projection = {
        "_id": 0,
        "department_id": 1,
        "department_name": 1,
        "fees_resources_final.item": 1,
        "fees_resources_final.fee_category": 1,
        "fees_resources_final.fee_label_from_document": 1,
        "fees_resources_final.use_in_ai_process": 1,
        "fees_resources_final.fee_send_option": 1,
    }

    try:
        collections = await ai_db.list_collection_names()
        if AI_FEES_COLLECTION not in collections:
            logger.warning(
                f"AI participation collection '{AI_FEES_COLLECTION}' not found "
                f"in database. Treating AI status as unknown."
            )
            return None
        rows = await ai_db[AI_FEES_COLLECTION].find(query, projection).to_list(
            length=None
        )
    except Exception as e:
        logger.error(f"Failed to read AI participation from MongoDB: {e}")
        return None

    # Group finalized documents by department. Duplicate department IDs
    # exist in the source; whether the duplicates agree decides whether an
    # authoritative catalog can be selected at all.
    docs_by_dept: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        raw_id = row.get("department_id")
        if raw_id is None:
            continue
        try:
            dept_id = int(raw_id)
        except (ValueError, TypeError):
            continue
        docs_by_dept[dept_id].append(row)

    result: Dict[int, Dict[str, Any]] = {}
    for dept_id, docs in docs_by_dept.items():
        # A stable signature over ALL projected tile fields — including the
        # enablement flags, so conflicting controls are not hidden — decides
        # whether the finalized documents are equivalent.
        signatures = {
            json.dumps(
                sorted(
                    json.dumps(tile, sort_keys=True, default=str)
                    for tile in (doc.get("fees_resources_final") or [])
                )
            )
            for doc in docs
        }
        department_name = next(
            (d.get("department_name") for d in docs if d.get("department_name")),
            None,
        )
        if len(signatures) <= 1:
            # Single document, or identical duplicates — equivalent, take the
            # first; there is nothing to guess between.
            fees = docs[0].get("fees_resources_final") or []
            info = _classify_fees(fees)
            info["fee_catalog_version"] = FEE_CATALOG_VERSION
            info["fee_catalog"] = compact_fee_catalog(fees)
        else:
            # Differing finalized schedules — no authoritative pick. Report
            # unknown rather than guessing via recency or merging catalogs.
            logger.warning(
                "Department %s has %d differing finalized fee schedule "
                "documents; AI participation is ambiguous.",
                dept_id,
                len(docs),
            )
            info = _classify_fees([])
            info.update(
                uses_ai=None,
                ai_mode="unknown",
                fee_catalog=None,
                fee_catalog_version=FEE_CATALOG_VERSION,
            )
        info["department_name"] = department_name
        result[dept_id] = info
    return result


def get_department_draft_activity(
    start_date: str,
    end_date: str,
) -> List[Dict[str, Any]]:
    """Count drafts submitted during the period for every active department.

    Returns ALL departments with activity in the period (sorted by
    submitted_drafts desc) so that each AI-status tab can build its own
    independent top-N ranking instead of filtering a single top-N list.
    """
    sql = """
    SELECT
        CAST(c.dept_id AS VARCHAR(50)) AS department_id,
        MAX(d.Name) AS department_name,
        MAX(d.physical_state) AS state,
        COUNT(DISTINCT c.id) AS submitted_drafts
    FROM Claims c
    LEFT JOIN Departments d ON d.ID = c.dept_id
    WHERE c.submitted = 1
      AND c.original_run_id IS NULL
      AND c.date_of_submitted BETWEEN %(start_date)s AND %(end_date)s
      AND c.dept_id IS NOT NULL
      AND c.dept_id NOT IN (1136, 2198, 2627, 2628, 2629)
    GROUP BY CAST(c.dept_id AS VARCHAR(50))
    ORDER BY submitted_drafts DESC
    """

    filters = DashboardFilters(start_date=start_date, end_date=end_date)
    result = target_db.execute_read(sql, filters)
    return result.get("rows", [])


async def get_ai_adoption_report(
    ai_db,
    start_date: str,
    end_date: str,
    limit: int = 50,
    ai_status: str = "all",
) -> Dict[str, Any]:
    """Produce the AI Adoption dashboard payload for the selected period.

    Each AI-status tab (all / using_ai / not_using_ai / unknown) is its own
    independent top-N ranking.  We fetch every department with activity in the
    period, classify ALL of them by AI status, then filter to the requested
    status and take the top *limit* — rather than filtering a single top-N
    "all" list which would yield fewer than *limit* rows for a filtered tab.
    """
    activity = get_department_draft_activity(start_date, end_date)

    # Collect department IDs for a single targeted Mongo query.
    dept_ids: List[int] = []
    for row in activity:
        try:
            dept_id = int(row["department_id"])
        except (ValueError, TypeError):
            continue
        if dept_id not in EXCLUDED_DEPARTMENT_IDS:
            dept_ids.append(dept_id)

    participation = await get_ai_participation_map(ai_db, dept_ids)

    total_drafts = sum(int(row.get("submitted_drafts", 0) or 0) for row in activity)
    total_drafts = max(total_drafts, 1)  # avoid div/0; 0 still handled below

    # Classify every department with activity in the period.
    classified: List[Dict[str, Any]] = []
    for row in activity:
        try:
            dept_id = int(row["department_id"])
        except (ValueError, TypeError):
            continue

        ai_info: Dict[str, Any]
        if participation is None:
            ai_info = {
                "uses_ai": False,
                "ai_mode": "unknown",
                "qualifying_fee_count": 0,
                "has_auto": False,
                "has_queued": False,
                "has_limited_auto": False,
            }
            status = "unknown"
        else:
            ai_info = participation.get(
                dept_id,
                {
                    "uses_ai": False,
                    "ai_mode": "not_using_ai",
                    "qualifying_fee_count": 0,
                    "has_auto": False,
                    "has_queued": False,
                    "has_limited_auto": False,
                },
            )
            if ai_info.get("uses_ai") is None:
                status = "unknown"
            else:
                status = "using_ai" if ai_info["uses_ai"] else "not_using_ai"

        drafts = int(row.get("submitted_drafts", 0) or 0)
        pct = (drafts / total_drafts * 100) if total_drafts > 0 else 0.0
        classified.append(
            {
                "department_id": row.get("department_id"),
                "department_name": row.get("department_name"),
                "state": row.get("state"),
                "submitted_drafts": drafts,
                "percent_of_total_volume": round(pct, 2),
                "ai_status": status,
                "ai_mode": ai_info.get("ai_mode", status),
                "qualifying_fee_count": ai_info.get("qualifying_fee_count", 0),
                "has_auto": ai_info.get("has_auto", False),
                "has_queued": ai_info.get("has_queued", False),
                "has_limited_auto": ai_info.get("has_limited_auto", False),
            }
        )

    # Summary over the full activity set, not the filtered/sliced view.
    using_drafts = 0
    not_using_drafts = 0
    unknown_drafts = 0
    using = 0
    not_using = 0
    unknown = 0

    for item in classified:
        drafts = item["submitted_drafts"]
        status = item["ai_status"]
        if status == "using_ai":
            using += 1
            using_drafts += drafts
        elif status == "not_using_ai":
            not_using += 1
            not_using_drafts += drafts
        else:
            unknown += 1
            unknown_drafts += drafts

    active_departments = len(classified)
    total_drafts_for_pct = using_drafts + not_using_drafts + unknown_drafts

    ai_coverage = (
        (using_drafts / total_drafts_for_pct * 100) if total_drafts_for_pct else 0.0
    )
    remaining_opportunity = (
        (not_using_drafts / total_drafts_for_pct * 100)
        if total_drafts_for_pct
        else 0.0
    )

    summary = {
        "active_departments": active_departments,
        "departments_using_ai": using,
        "departments_not_using_ai": not_using,
        "departments_unknown": unknown,
        "total_drafts": total_drafts_for_pct,
        "ai_department_drafts": using_drafts,
        "non_ai_department_drafts": not_using_drafts,
        "unknown_department_drafts": unknown_drafts,
        "ai_coverage_percent": round(ai_coverage, 2),
        "remaining_opportunity_percent": round(remaining_opportunity, 2),
    }

    # Each tab is its own independent list: filter to the requested status,
    # then take the top *limit* and rank 1..N within that tab.
    if ai_status != "all":
        classified = [item for item in classified if item["ai_status"] == ai_status]

    classified = classified[:limit]

    departments: List[Dict[str, Any]] = []
    for rank, item in enumerate(classified, start=1):
        departments.append(
            {
                "rank_overall": rank,
                "department_id": item["department_id"],
                "department_name": item["department_name"],
                "state": item["state"],
                "submitted_drafts": item["submitted_drafts"],
                "percent_of_total_volume": item["percent_of_total_volume"],
                "ai_status": item["ai_status"],
                "ai_mode": item["ai_mode"],
                "qualifying_fee_count": item["qualifying_fee_count"],
                "has_auto": item["has_auto"],
                "has_queued": item["has_queued"],
                "has_limited_auto": item["has_limited_auto"],
            }
        )

    return {
        "period": {"start_date": start_date, "end_date": end_date},
        "ai_status_basis": "current_configuration",
        "summary": summary,
        "departments": departments,
    }
