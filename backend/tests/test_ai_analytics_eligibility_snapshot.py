"""Projection-mode AI eligibility must not depend on the operational AI
Mongo fee configuration being reachable (Devin Review, PR #39)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from ai_analytics import eligibility_snapshot as snap
from ai_analytics_worker.config import worker_config

PART = {1: {"uses_ai": True, "ai_mode": "auto"}}


async def _seed(db, age_seconds=0, schema_version=snap.SNAPSHOT_SCHEMA_VERSION):
    doc = {
        "_id": worker_config.AI_ELIGIBILITY_SNAPSHOT_ID,
        "participation": {"1": PART[1]},
        "refreshed_at": datetime.now(UTC) - timedelta(seconds=age_seconds),
    }
    if schema_version is not None:
        doc["schema_version"] = schema_version
    await db[worker_config.AI_ELIGIBILITY_SNAPSHOT_COLLECTION].insert_one(doc)


@pytest.mark.asyncio
async def test_projection_mode_serves_fresh_snapshot_without_source(mock_mongo_db):
    await _seed(mock_mongo_db)
    fetch = AsyncMock(side_effect=RuntimeError("ai mongo down"))
    result, status = await snap.resolve_participation(
        object(), mock_mongo_db, fetch=fetch, prefer_snapshot=True
    )
    assert result == PART
    assert status == snap.STATUS_AVAILABLE
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_snapshot_refreshes_from_source_and_rewrites(mock_mongo_db):
    await _seed(mock_mongo_db, age_seconds=worker_config.AI_ELIGIBILITY_SNAPSHOT_TTL_SECONDS + 5)
    fetch = AsyncMock(return_value={2: {"uses_ai": True}})
    result, status = await snap.resolve_participation(
        object(), mock_mongo_db, fetch=fetch, prefer_snapshot=True
    )
    assert result == {2: {"uses_ai": True}}
    assert status == snap.STATUS_AVAILABLE
    fetch.assert_awaited_once()
    stored, _ = await snap.read_snapshot(mock_mongo_db)
    assert stored == {2: {"uses_ai": True}}


@pytest.mark.asyncio
async def test_source_failure_falls_back_to_stale_snapshot(mock_mongo_db):
    await _seed(mock_mongo_db, age_seconds=worker_config.AI_ELIGIBILITY_SNAPSHOT_TTL_SECONDS + 5)
    fetch = AsyncMock(return_value=None)
    result, status = await snap.resolve_participation(
        object(), mock_mongo_db, fetch=fetch, prefer_snapshot=True
    )
    assert result == PART
    assert status == snap.STATUS_STALE


@pytest.mark.asyncio
async def test_direct_mode_reads_source_and_writes_snapshot(mock_mongo_db):
    await _seed(mock_mongo_db)
    fetch = AsyncMock(return_value={3: {"uses_ai": False}})
    result, status = await snap.resolve_participation(
        object(), mock_mongo_db, fetch=fetch, prefer_snapshot=False
    )
    assert result == {3: {"uses_ai": False}}
    fetch.assert_awaited_once()
    stored, _ = await snap.read_snapshot(mock_mongo_db)
    assert stored == {3: {"uses_ai": False}}


@pytest.mark.asyncio
async def test_no_snapshot_and_no_source_is_unavailable(mock_mongo_db):
    fetch = AsyncMock(return_value=None)
    result, status = await snap.resolve_participation(
        object(), mock_mongo_db, fetch=fetch, prefer_snapshot=True
    )
    assert result is None
    assert status == snap.STATUS_UNAVAILABLE


@pytest.mark.asyncio
async def test_fresh_legacy_snapshot_is_refreshed_immediately(mock_mongo_db):
    """A snapshot written before schema_version existed (v1, boolean-only
    participation) must not be served as fresh even when its TTL has not
    elapsed — it lacks the fee catalogs needed for step-1 matching."""
    await _seed(mock_mongo_db, schema_version=None)
    fetch = AsyncMock(return_value={2: {"uses_ai": True}})
    result, status = await snap.resolve_participation(
        object(), mock_mongo_db, fetch=fetch, prefer_snapshot=True
    )
    assert result == {2: {"uses_ai": True}}
    assert status == snap.STATUS_AVAILABLE
    fetch.assert_awaited_once()
    stored, _ = await snap.read_snapshot(mock_mongo_db)
    assert stored == {2: {"uses_ai": True}}


@pytest.mark.asyncio
async def test_legacy_snapshot_serves_as_stale_fallback_on_source_failure(
    mock_mongo_db,
):
    """If the source refresh of a legacy snapshot fails, its participation
    map is still served (stale) rather than reporting unavailable."""
    await _seed(mock_mongo_db, schema_version=None)
    fetch = AsyncMock(return_value=None)
    result, status = await snap.resolve_participation(
        object(), mock_mongo_db, fetch=fetch, prefer_snapshot=True
    )
    assert result == PART
    assert status == snap.STATUS_STALE


@pytest.mark.asyncio
async def test_stale_snapshot_marks_cohort_incomplete(mock_mongo_db, monkeypatch):
    """Serving eligibility from an expired snapshot must flag the cohort as
    incomplete so the dashboards show their data warning."""
    from unittest.mock import patch
    from ai_analytics.outcome_service import _load_normalized_cohort
    from ai_analytics.models import AiAnalyticsFilters
    from config import settings
    from database import db_manager

    monkeypatch.setattr(settings, "AI_ANALYTICS_USE_PROJECTION", True)
    monkeypatch.setattr(db_manager, "db", mock_mongo_db)
    await _seed(mock_mongo_db, age_seconds=worker_config.AI_ELIGIBILITY_SNAPSHOT_TTL_SECONDS + 5)

    with patch("ai_analytics.outcome_service.sql_repo.get_ai_invoice_cohort") as cohort, patch(
        "ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims",
        return_value={},
    ), patch(
        "ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims",
        return_value={},
    ), patch(
        "ai_analytics.outcome_service.get_ai_participation_map",
        new=AsyncMock(return_value=None),
    ):
        cohort.return_value = [
            {"claim_id": 100, "AI_inv_process_status": 4, "dept_id": 1,
             "department_name": "FD1", "department_state": "TX",
             "ai_business_updated_at": "2026-01-15T10:00:00"},
        ]
        records, source_status, data_complete = await _load_normalized_cohort(
            mock_mongo_db, AiAnalyticsFilters()
        )

    assert source_status["recoveryhub_ai_fee_config"] == snap.STATUS_STALE
    assert data_complete is False
    assert records[0]["ai_eligibility"] == "eligible"
