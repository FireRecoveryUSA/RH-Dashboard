"""Read-only MongoDB repository for AI Analytics.

All methods are retrieval-only (find / aggregate). No
insert_one/insert_many/update_one/update_many/delete_one/delete_many/
replace_one.

Reads from the RecoveryHub_AI MongoDB database (configured via
``settings.RECOVERYHUB_AI_MONGODB_DB_NAME``).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from .fee_schedule import STEP1_SOURCE_FIELDS

logger = logging.getLogger(__name__)

# Collection names — verified in Phase 0
AI_LINE_ITEMS_COLLECTION = "ai_line_items"
AGENT_CONVERSATIONS_COLLECTION = "ai_agent_conversations"
DEPARTMENT_FEES_COLLECTION = "department_fees_resources"

# Summary projection — lightweight fields for aggregate pages.
# Does NOT retrieve incident_info, full line_items, or large nested payloads.
SUMMARY_PROJECTION = {
    "_id": 1,
    "claim_id": 1,
    "draft_claim_id": 1,
    "run_number": 1,
    "department_id": 1,
    "department_name": 1,
    "is_billable": 1,
    "is_billable_not_determined": 1,
    "dept_ai_fee_mvi_status": 1,
    "dept_ai_identify_billable_status": 1,
    "billing_category": 1,
    "incident_duration_in_minutes": 1,
    "confidence_level": 1,
    "review_msg": 1,
    "inserted_at": 1,
    "updated_at": 1,
    "run_date": 1,
    "invoice_total": 1,
    "claim_processing_status": 1,
    "line_items_save_to_rh_status": 1,
    "agent_exec_status": 1,
    "thread_id_is_billable": 1,
    "thread_id": 1,
    "retry_thread_id": 1,
    "imported_source": 1,
    # Fields discovered in Phase 0:
    "dept_send_auto_invoice_status": 1,
    "update_count": 1,
    "conversation_id": 1,
    "processing_time_seconds": 1,
    "retry_count": 1,
    "completed_at": 1,
    # Step-1 category/fee-item identification + intake evidence fields
    # (see ai_analytics.fee_schedule — single source of truth).
    **{field: 1 for field in STEP1_SOURCE_FIELDS},
    # Cancellation fields populated on newer ai_line_items docs
    "is_cancelled": 1,
    "cancellation_reason": 1,
}

# Full projection — includes everything for the forensic trace page
FULL_PROJECTION = None  # None means retrieve all fields


def _normalize_claim_id(claim_id: Any) -> List[Any]:
    """Return both int and string versions of a claim_id for $or queries."""
    try:
        cid_int = int(claim_id)
        return [cid_int, str(cid_int)]
    except (ValueError, TypeError):
        return [claim_id]


# ---------------------------------------------------------------------------
# ai_line_items
# ---------------------------------------------------------------------------

async def get_ai_line_items_for_claim_ids(
    ai_db,
    claim_ids: List[int],
) -> Dict[int, Dict[str, Any]]:
    """Fetch ai_line_items for a batch of claim IDs (summary projection).

    Returns a dict keyed by claim_id → ai_line_items document.
    Uses a single ``$in`` query — no N+1.
    """
    if not claim_ids:
        return {}

    # claim_id in production is stored as int, but be defensive and query
    # both int and string representations.
    int_ids = [int(cid) for cid in claim_ids]
    str_ids = [str(cid) for cid in claim_ids]

    query = {
        "claim_id": {"$in": int_ids + str_ids}
    }

    try:
        cursor = ai_db[AI_LINE_ITEMS_COLLECTION].find(query, SUMMARY_PROJECTION)
        # length=None returns all matches — a fixed cap here silently
        # truncated AI-side data for cohorts over 1,000 claims, which made
        # the funnel's "step 1 evaluated" stage undercount.
        docs = await cursor.to_list(length=None)
    except Exception as e:
        logger.error(f"Failed to fetch ai_line_items for {len(claim_ids)} claims: {e}")
        raise

    by_claim: Dict[int, Dict[str, Any]] = {}
    for doc in docs:
        claim_id = doc.get("claim_id")
        if claim_id is None:
            continue
        try:
            cid = int(claim_id)
        except (ValueError, TypeError):
            continue
        # No duplicates expected (Phase 0 verified), but keep most recent
        existing = by_claim.get(cid)
        if existing is None:
            by_claim[cid] = doc
        else:
            existing_updated = existing.get("updated_at")
            new_updated = doc.get("updated_at")
            if new_updated and (not existing_updated or new_updated > existing_updated):
                by_claim[cid] = doc
    return by_claim


async def get_ai_line_items_for_claim(
    ai_db,
    claim_id: int,
) -> Optional[Dict[str, Any]]:
    """Fetch a single ai_line_items document (full projection) for a claim."""
    int_ids = _normalize_claim_id(claim_id)
    query = {
        "$or": [{"claim_id": cid} for cid in int_ids]
    }
    try:
        # Full projection for forensic trace
        doc = await ai_db[AI_LINE_ITEMS_COLLECTION].find_one(query)
        return doc
    except Exception as e:
        logger.error(f"Failed to fetch ai_line_items for claim {claim_id}: {e}")
        raise


# ---------------------------------------------------------------------------
# ai_agent_conversations
# ---------------------------------------------------------------------------

CONVERSATION_PROJECTION = {
    "_id": 1,
    "agent": 1,
    "status": 1,
    "created_at": 1,
    "processing_stage": 1,
    "request_type": 1,
    "execution_time_seconds": 1,
    "incident_json": 1,
    "input_data": 1,
    "results": 1,
    "output_data": 1,
}


_CONVERSATION_CLAIM_ID_FIELDS = (
    "claim_id",
    "incident_json.claim_id",
    "input_data.claim_id",
)


def build_conversation_claim_query(claim_ids: List[Any]) -> Dict[str, Any]:
    """Filter matching ``ai_agent_conversations`` docs for any of ``claim_ids``.

    The query pattern confirmed in RecoveryHub_AI: ``$or`` across the
    top-level ``claim_id`` and the nested ``incident_json.claim_id`` /
    ``input_data.claim_id`` fields, with both int and string
    representations of every id. Shared by the per-claim lookup and the
    direct-read agent-stats aggregation so the two can never disagree on
    which conversations belong to a claim.
    """
    variants: List[Any] = []
    seen: Set[Tuple[type, Any]] = set()
    for cid in claim_ids:
        for v in _normalize_claim_id(cid):
            key = (type(v), v)
            if key not in seen:
                seen.add(key)
                variants.append(v)
    return {
        "$or": [
            {field: {"$in": variants}}
            for field in _CONVERSATION_CLAIM_ID_FIELDS
        ]
    }


async def get_agent_conversations_for_claim(
    ai_db,
    claim_id: int,
) -> List[Dict[str, Any]]:
    """Fetch all agent conversations for a claim, sorted chronologically.

    See ``build_conversation_claim_query`` for the field/type matching.
    """
    query = build_conversation_claim_query([claim_id])

    try:
        cursor = (
            ai_db[AGENT_CONVERSATIONS_COLLECTION]
            .find(query, CONVERSATION_PROJECTION)
            .sort("created_at", 1)
        )
        docs = await cursor.to_list(length=1000)
        return docs
    except Exception as e:
        logger.error(f"Failed to fetch agent conversations for claim {claim_id}: {e}")
        raise


async def get_conversation_by_id(
    ai_db,
    conversation_id: str,
) -> Optional[Dict[str, Any]]:
    """Fetch a single conversation by its _id (used when ai_line_items has
    a conversation_id link)."""
    from bson import ObjectId

    try:
        doc = await ai_db[AGENT_CONVERSATIONS_COLLECTION].find_one(
            {"_id": ObjectId(conversation_id)},
            CONVERSATION_PROJECTION,
        )
        return doc
    except Exception as e:
        logger.error(f"Failed to fetch conversation {conversation_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Department fees (AI participation context)
# ---------------------------------------------------------------------------

async def get_ai_fee_configuration_for_departments(
    ai_db,
    department_ids: List[int],
) -> Dict[int, Dict[str, Any]]:
    """Fetch AI fee configuration for a set of departments.

    Provides context for department comparison views (AI send mode, etc.).
    """
    if not department_ids:
        return {}

    query = {
        "department_id": {"$in": [int(d) for d in department_ids]}
    }
    projection = {
        "_id": 0,
        "department_id": 1,
        "department_name": 1,
        "fees_resources_final": 1,
    }

    try:
        cursor = ai_db[DEPARTMENT_FEES_COLLECTION].find(query, projection)
        docs = await cursor.to_list(length=1000)
    except Exception as e:
        logger.error(f"Failed to fetch AI fee config for {len(department_ids)} departments: {e}")
        raise

    by_dept: Dict[int, Dict[str, Any]] = {}
    for doc in docs:
        dept_id = doc.get("department_id")
        if dept_id is None:
            continue
        try:
            did = int(dept_id)
        except (ValueError, TypeError):
            continue
        by_dept[did] = doc
    return by_dept


# ---------------------------------------------------------------------------
# Collection availability check
# ---------------------------------------------------------------------------

async def check_ai_db_available(ai_db) -> bool:
    """Check if the AI MongoDB database is reachable and has the required
    collections."""
    try:
        collections = await ai_db.list_collection_names()
        return AI_LINE_ITEMS_COLLECTION in collections
    except Exception as e:
        logger.warning(f"AI MongoDB availability check failed: {e}")
        return False
