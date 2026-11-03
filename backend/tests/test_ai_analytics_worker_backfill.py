"""Unit tests for ai_analytics_worker.backfill (Phase 4).

Feature under test: the historical backfill orchestrator that scans
``ai_line_items`` for all claim_ids, fetches source data for each, builds
projections, and persists them to the ``ai_invoice_analytics`` collection.

Failure prevented:
- A backfill that stops the entire FastAPI process because one claim's
  source data is malformed — the backfill must dead-letter and continue.
- A backfill that monopolizes the event loop during a large historical
  scan — the max_claims_per_cycle yield prevents this.
- A backfill that can't be cancelled gracefully — the stop_event check
  between batches must allow prompt cancellation.

Test level: unit. Uses the in-memory mongomock backend from conftest for
the destination writes, and mocks the source_repository calls.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId
from pymongo.errors import ConnectionFailure

from ai_analytics_worker.backfill import BackfillResult, run_backfill
from ai_analytics_worker.config import worker_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_source_doc(claim_id: int = 12345, **overrides):
    """Build a minimal ai_line_items source doc for the backfill cursor."""
    base = {
        "_id": ObjectId(),
        "claim_id": claim_id,
    }
    base.update(overrides)
    return base


def make_full_doc(claim_id: int = 12345, **overrides):
    """Build a full ai_line_items doc that build_projection can consume."""
    base = {
        "_id": ObjectId(),
        "claim_id": claim_id,
        "department_id": 42,
        "department_name": "Fire Department",
        "updated_at": datetime(2026, 7, 2, 10, 30, 0),
        "claim_processing_status": "COMPLETED",
        "agent_exec_status": "success",
        "confidence_level": 85,
        "billing_category": "Fire Suppression",
        "line_items_save_to_rh_status": True,
        "retry_count": 0,
        "line_items": [],
    }
    base.update(overrides)
    return base


def seed_ai_line_items(ai_db, docs):
    """Insert source docs into the mock AI database's ai_line_items collection."""
    import asyncio as _asyncio

    async def _seed():
        for doc in docs:
            await ai_db["ai_line_items"].insert_one(doc)

    _asyncio.run(_seed())


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestBackfillHappyPath:
    """Tests that a normal backfill processes all claims and writes projections."""

    @pytest.mark.asyncio
    async def test_processes_all_claims_and_writes_projections(
        self, mock_mongo_db
    ):
        """A backfill with 3 claims produces 3 projections."""
        ai_db = mock_mongo_db  # reuse the same mock for source and dest
        # Seed source data
        for cid in [100, 200, 300]:
            await ai_db["ai_line_items"].insert_one(
                make_source_doc(claim_id=cid)
            )

        # Mock source_repository fetches to return full docs
        async def mock_get_line_items(db, claim_id):
            return make_full_doc(claim_id=claim_id)

        async def mock_get_conversations(db, claim_id):
            return []

        with patch(
            "ai_analytics_worker.claim_refresh.get_ai_line_items_for_claim_with_retry",
            new=mock_get_line_items,
        ), patch(
            "ai_analytics_worker.claim_refresh.get_agent_conversations_for_claim_with_retry",
            new=mock_get_conversations,
        ):
            result = await run_backfill(ai_db, mock_mongo_db, batch_size=10)

        assert result.cancelled is False
        assert result.total_claim_ids == 3
        assert result.claims_processed == 3
        assert result.claims_failed == 0
        assert result.projections_inserted == 3
        assert result.projections_updated == 0
        assert result.completed_at is not None

        # Verify projections were written
        count = await mock_mongo_db[
            worker_config.PROJECTIONS_COLLECTION
        ].count_documents({})
        assert count == 3

    @pytest.mark.asyncio
    async def test_backfill_records_run_in_audit_log(self, mock_mongo_db):
        ai_db = mock_mongo_db
        await ai_db["ai_line_items"].insert_one(make_source_doc(claim_id=100))

        async def mock_get_line_items(db, claim_id):
            return make_full_doc(claim_id=claim_id)

        with patch(
            "ai_analytics_worker.claim_refresh.get_ai_line_items_for_claim_with_retry",
            new=mock_get_line_items,
        ), patch(
            "ai_analytics_worker.claim_refresh.get_agent_conversations_for_claim_with_retry",
            new=AsyncMock(return_value=[]),
        ):
            await run_backfill(ai_db, mock_mongo_db, batch_size=10)

        runs = await mock_mongo_db[
            worker_config.WORKER_RUNS_COLLECTION
        ].find({}).to_list(length=10)
        assert len(runs) == 1
        assert runs[0]["run_type"] == "backfill"
        assert runs[0]["status"] == "completed"
        assert runs[0]["claims_processed"] == 1
        assert runs[0]["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_upsert_updates_existing_projection(self, mock_mongo_db):
        """Re-running a backfill updates existing projections, not duplicates."""
        ai_db = mock_mongo_db
        await ai_db["ai_line_items"].insert_one(make_source_doc(claim_id=100))

        call_count = {"n": 0}

        async def mock_get_line_items(db, claim_id):
            call_count["n"] += 1
            return make_full_doc(claim_id=claim_id, department_name=f"Dept v{call_count['n']}")

        with patch(
            "ai_analytics_worker.claim_refresh.get_ai_line_items_for_claim_with_retry",
            new=mock_get_line_items,
        ), patch(
            "ai_analytics_worker.claim_refresh.get_agent_conversations_for_claim_with_retry",
            new=AsyncMock(return_value=[]),
        ):
            result1 = await run_backfill(ai_db, mock_mongo_db, batch_size=10)
            result2 = await run_backfill(ai_db, mock_mongo_db, batch_size=10)

        assert result1.projections_inserted == 1
        assert result2.projections_updated == 1
        assert result2.projections_inserted == 0

        # Only one document, with the latest data
        count = await mock_mongo_db[
            worker_config.PROJECTIONS_COLLECTION
        ].count_documents({"_id": 100})
        assert count == 1
        doc = await mock_mongo_db[worker_config.PROJECTIONS_COLLECTION].find_one(
            {"_id": 100}
        )
        assert doc["department_name"] == "Dept v2"


# ---------------------------------------------------------------------------
# Error handling — one bad claim doesn't stop the backfill
# ---------------------------------------------------------------------------


class TestBackfillErrorHandling:
    """Tests that a failing claim is dead-lettered and the backfill continues."""

    @pytest.mark.asyncio
    async def test_failing_claim_is_dead_lettered(self, mock_mongo_db):
        ai_db = mock_mongo_db
        await ai_db["ai_line_items"].insert_one(make_source_doc(claim_id=100))
        await ai_db["ai_line_items"].insert_one(make_source_doc(claim_id=200))

        async def mock_get_line_items(db, claim_id):
            if claim_id == 100:
                raise ConnectionFailure("connection dropped")
            return make_full_doc(claim_id=claim_id)

        with patch(
            "ai_analytics_worker.claim_refresh.get_ai_line_items_for_claim_with_retry",
            new=mock_get_line_items,
        ), patch(
            "ai_analytics_worker.claim_refresh.get_agent_conversations_for_claim_with_retry",
            new=AsyncMock(return_value=[]),
        ):
            result = await run_backfill(ai_db, mock_mongo_db, batch_size=10)

        assert result.claims_processed == 1  # claim 200 succeeded
        assert result.claims_failed == 1  # claim 100 failed
        assert result.dead_lettered == 1

        # Dead-letter record exists for claim 100
        dl = await mock_mongo_db[
            worker_config.DEAD_LETTERS_COLLECTION
        ].find_one({"claim_id": 100})
        assert dl is not None
        assert dl["error_type"] == "ConnectionFailure"
        assert dl["source_event_type"] == "backfill"

    @pytest.mark.asyncio
    async def test_all_claims_fail_still_completes(self, mock_mongo_db):
        ai_db = mock_mongo_db
        for cid in [100, 200, 300]:
            await ai_db["ai_line_items"].insert_one(make_source_doc(claim_id=cid))

        async def mock_get_line_items(db, claim_id):
            raise Exception("everything fails")

        with patch(
            "ai_analytics_worker.claim_refresh.get_ai_line_items_for_claim_with_retry",
            new=mock_get_line_items,
        ), patch(
            "ai_analytics_worker.claim_refresh.get_agent_conversations_for_claim_with_retry",
            new=AsyncMock(return_value=[]),
        ):
            result = await run_backfill(ai_db, mock_mongo_db, batch_size=10)

        assert result.claims_processed == 0
        assert result.claims_failed == 3
        assert result.dead_lettered == 3
        assert result.cancelled is False
        assert result.completed_at is not None


# ---------------------------------------------------------------------------
# Skipped claims — invalid claim_id
# ---------------------------------------------------------------------------


class TestBackfillSkips:
    """Tests that claims with invalid claim_ids are skipped."""

    @pytest.mark.asyncio
    async def test_skips_claim_with_none_claim_id(self, mock_mongo_db):
        ai_db = mock_mongo_db
        await ai_db["ai_line_items"].insert_one(
            make_source_doc(claim_id=None)
        )
        await ai_db["ai_line_items"].insert_one(make_source_doc(claim_id=100))

        async def mock_get_line_items(db, claim_id):
            return make_full_doc(claim_id=claim_id)

        with patch(
            "ai_analytics_worker.claim_refresh.get_ai_line_items_for_claim_with_retry",
            new=mock_get_line_items,
        ), patch(
            "ai_analytics_worker.claim_refresh.get_agent_conversations_for_claim_with_retry",
            new=AsyncMock(return_value=[]),
        ):
            result = await run_backfill(ai_db, mock_mongo_db, batch_size=10)

        assert result.claims_skipped == 1
        assert result.total_claim_ids == 1
        assert result.claims_processed == 1

    @pytest.mark.asyncio
    async def test_skips_claim_with_non_numeric_claim_id(self, mock_mongo_db):
        ai_db = mock_mongo_db
        await ai_db["ai_line_items"].insert_one(
            make_source_doc(claim_id="not-a-number")
        )

        with patch(
            "ai_analytics_worker.claim_refresh.get_ai_line_items_for_claim_with_retry",
            new=AsyncMock(return_value=make_full_doc()),
        ), patch(
            "ai_analytics_worker.claim_refresh.get_agent_conversations_for_claim_with_retry",
            new=AsyncMock(return_value=[]),
        ):
            result = await run_backfill(ai_db, mock_mongo_db, batch_size=10)

        assert result.claims_skipped == 1
        assert result.total_claim_ids == 0


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestBackfillCancellation:
    """Tests that the backfill respects the stop_event for graceful shutdown."""

    @pytest.mark.asyncio
    async def test_stop_event_cancels_before_start(self, mock_mongo_db):
        """If stop_event is already set, backfill cancels immediately."""
        ai_db = mock_mongo_db
        await ai_db["ai_line_items"].insert_one(make_source_doc(claim_id=100))

        stop_event = asyncio.Event()
        stop_event.set()

        with patch(
            "ai_analytics_worker.claim_refresh.get_ai_line_items_for_claim_with_retry",
            new=AsyncMock(return_value=make_full_doc()),
        ), patch(
            "ai_analytics_worker.claim_refresh.get_agent_conversations_for_claim_with_retry",
            new=AsyncMock(return_value=[]),
        ):
            result = await run_backfill(
                ai_db, mock_mongo_db, stop_event=stop_event, batch_size=10
            )

        assert result.cancelled is True
        assert result.claims_processed == 0

    @pytest.mark.asyncio
    async def test_stop_event_cancels_between_batches(self, mock_mongo_db):
        """Stop event set during processing is observed between batches."""
        ai_db = mock_mongo_db
        # Insert enough claims to span 2 batches
        for cid in range(100, 105):
            await ai_db["ai_line_items"].insert_one(make_source_doc(claim_id=cid))

        stop_event = asyncio.Event()
        call_count = {"n": 0}

        async def mock_get_line_items(db, claim_id):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                stop_event.set()
            return make_full_doc(claim_id=claim_id)

        with patch(
            "ai_analytics_worker.claim_refresh.get_ai_line_items_for_claim_with_retry",
            new=mock_get_line_items,
        ), patch(
            "ai_analytics_worker.claim_refresh.get_agent_conversations_for_claim_with_retry",
            new=AsyncMock(return_value=[]),
        ):
            result = await run_backfill(
                ai_db, mock_mongo_db, stop_event=stop_event, batch_size=3
            )

        assert result.cancelled is True
        # At least one claim was processed before cancellation
        assert result.claims_processed >= 1

        # Run audit log shows cancelled status
        run_doc = await mock_mongo_db[
            worker_config.WORKER_RUNS_COLLECTION
        ].find_one({})
        assert run_doc["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Empty source collection
# ---------------------------------------------------------------------------


class TestBackfillEmptySource:
    """Tests that an empty source collection completes with zero claims."""

    @pytest.mark.asyncio
    async def test_empty_source_completes_with_zero_claims(self, mock_mongo_db):
        ai_db = mock_mongo_db
        # No documents in ai_line_items

        with patch(
            "ai_analytics_worker.claim_refresh.get_ai_line_items_for_claim_with_retry",
            new=AsyncMock(return_value=make_full_doc()),
        ), patch(
            "ai_analytics_worker.claim_refresh.get_agent_conversations_for_claim_with_retry",
            new=AsyncMock(return_value=[]),
        ):
            result = await run_backfill(ai_db, mock_mongo_db, batch_size=10)

        assert result.total_claim_ids == 0
        assert result.claims_processed == 0
        assert result.cancelled is False
        assert result.completed_at is not None

        # Run is still recorded
        run_doc = await mock_mongo_db[
            worker_config.WORKER_RUNS_COLLECTION
        ].find_one({})
        assert run_doc["status"] == "completed"


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------


class TestBackfillBatching:
    """Tests that the backfill processes claims in batches via cursor pagination."""

    @pytest.mark.asyncio
    async def test_processes_more_than_one_batch(self, mock_mongo_db):
        """Claims beyond a single batch_size are all processed."""
        ai_db = mock_mongo_db
        # Insert 5 claims, batch_size=2 → 3 batches
        for cid in range(100, 105):
            await ai_db["ai_line_items"].insert_one(make_source_doc(claim_id=cid))

        async def mock_get_line_items(db, claim_id):
            return make_full_doc(claim_id=claim_id)

        with patch(
            "ai_analytics_worker.claim_refresh.get_ai_line_items_for_claim_with_retry",
            new=mock_get_line_items,
        ), patch(
            "ai_analytics_worker.claim_refresh.get_agent_conversations_for_claim_with_retry",
            new=AsyncMock(return_value=[]),
        ):
            result = await run_backfill(ai_db, mock_mongo_db, batch_size=2)

        assert result.total_claim_ids == 5
        assert result.claims_processed == 5
        assert result.projections_inserted == 5

    @pytest.mark.asyncio
    async def test_max_claims_per_cycle_caps_in_flight_claims(self, mock_mongo_db):
        """A batch larger than ``max_claims_per_cycle`` is scheduled in
        chunks: never more than the cap is in flight at once, even with
        a wide concurrency budget."""
        ai_db = mock_mongo_db
        for cid in range(100, 110):
            await ai_db["ai_line_items"].insert_one(make_source_doc(claim_id=cid))

        in_flight = 0
        peak = 0

        async def mock_get_line_items(db, claim_id):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return make_full_doc(claim_id=claim_id)

        async def mock_get_conversations(db, claim_id):
            return []

        with patch(
            "ai_analytics_worker.claim_refresh.get_ai_line_items_for_claim_with_retry",
            new=mock_get_line_items,
        ), patch(
            "ai_analytics_worker.claim_refresh.get_agent_conversations_for_claim_with_retry",
            new=mock_get_conversations,
        ):
            result = await run_backfill(
                ai_db, mock_mongo_db,
                batch_size=10, max_claims_per_cycle=3, concurrency=10,
            )

        assert result.claims_processed == 10
        assert peak <= 3


class TestStartupBackfillCheck:
    """``main._run_worker_backfill_if_needed`` — the leader-only startup hook."""

    @pytest.fixture
    def startup_env(self, mock_mongo_db):
        import main as app_main

        with patch.object(app_main.db_manager, "db", mock_mongo_db), patch.object(
            app_main.worker_runtime, "start_backfill", new_callable=AsyncMock
        ) as start:
            yield app_main, mock_mongo_db, start

    @pytest.mark.asyncio
    async def test_empty_projection_starts_backfill(self, startup_env):
        app_main, _, start = startup_env
        await app_main._run_worker_backfill_if_needed()
        start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_current_schema_skips_backfill(self, startup_env):
        app_main, db, start = startup_env
        await db[worker_config.PROJECTIONS_COLLECTION].insert_one({
            "_id": 1,
            "projection_schema_version": worker_config.projection_schema_version,
        })
        await app_main._run_worker_backfill_if_needed()
        start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_schema_starts_backfill(self, startup_env):
        app_main, db, start = startup_env
        await db[worker_config.PROJECTIONS_COLLECTION].insert_many([
            {"_id": 1,
             "projection_schema_version": worker_config.projection_schema_version},
            {"_id": 2,
             "projection_schema_version": worker_config.projection_schema_version - 1},
        ])
        await app_main._run_worker_backfill_if_needed()
        start.assert_awaited_once()
