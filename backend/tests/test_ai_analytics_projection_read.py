"""Tests for ai_analytics.projection_read_repository (Phases 9-10).

Feature under test: the adapter that maps the worker's
``ai_invoice_analytics`` projection documents to the raw
``ai_line_items`` field shape that ``build_normalized_record`` expects,
and the batch fetch function that reads projections by claim_id.

Phase 10 additions:
-- ``projection_to_trace_data`` maps the projection to the full field
   shape that ``invoice_trace_service`` needs (line items with resources,
   review_msg, timestamps, conversation_id, thread_id_is_billable).
-- ``get_projection_for_trace`` fetches a single projection for the trace.
-- ``aggregate_agent_stats_from_projections`` aggregates conversation
   summaries from the projection for /diagnostics/agents.

Failure prevented:
-- A field name mismatch between the projection and the raw ai_record
   shape would silently produce ``None`` for a field that has a real
   value in the source data. For example, the projection stores
   ``ai_processing_status`` but ``build_normalized_record`` reads
   ``claim_processing_status`` — without the rename, every record would
   show ``ai_processing_status=None`` and the status distribution
   dashboard would collapse to "unknown".
-- A missing projection for a claim that HAS an ai_line_items record
   would make the claim look like it has no AI data (``ai_record=None``),
   undercounting AI metrics. The adapter must handle this the same way
   the direct-read path does — by omitting the claim from the result.

Test level: unit (mapping) + integration (fetch with mongomock).
"""

from __future__ import annotations

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from ai_analytics.projection_read_repository import (
    projection_to_ai_record,
    get_projection_records_for_claim_ids,
    projection_to_trace_data,
    get_projection_for_trace,
    aggregate_agent_stats_from_projections,
)


# ---------------------------------------------------------------------------
# projection_to_ai_record — field mapping
# ---------------------------------------------------------------------------


class TestProjectionToAiRecord:
    """Verify the projection → ai_record field mapping is complete and correct."""

    def test_renames_ai_processing_status_to_claim_processing_status(self):
        """Projection's ``ai_processing_status`` → raw ``claim_processing_status``."""
        projection = {"ai_processing_status": "COMPLETED"}
        result = projection_to_ai_record(projection)
        assert result["claim_processing_status"] == "COMPLETED"

    def test_renames_agent_execution_status_to_agent_exec_status(self):
        """Projection's ``agent_execution_status`` → raw ``agent_exec_status``."""
        projection = {"agent_execution_status": "success"}
        result = projection_to_ai_record(projection)
        assert result["agent_exec_status"] == "success"

    def test_renames_ai_invoice_total_to_invoice_total(self):
        """Projection's ``ai_invoice_total`` → raw ``invoice_total``."""
        projection = {"ai_invoice_total": 1500.00}
        result = projection_to_ai_record(projection)
        assert result["invoice_total"] == 1500.00

    def test_renames_processing_duration_seconds_to_processing_time_seconds(self):
        """Projection's ``processing_duration_seconds`` → raw ``processing_time_seconds``."""
        projection = {"processing_duration_seconds": 42.5}
        result = projection_to_ai_record(projection)
        assert result["processing_time_seconds"] == 42.5

    def test_passthrough_fields_keep_same_name(self):
        """Fields with the same name in both shapes pass through unchanged."""
        projection = {
            "confidence_level": 85,
            "is_billable": True,
            "billing_category": "Fire Suppression",
            "line_items_save_to_rh_status": True,
            "retry_count": 2,
        }
        result = projection_to_ai_record(projection)
        assert result["confidence_level"] == 85
        assert result["is_billable"] is True
        assert result["billing_category"] == "Fire Suppression"
        assert result["line_items_save_to_rh_status"] is True
        assert result["retry_count"] == 2

    def test_retry_count_passthrough_not_silently_dropped(self):
        """``retry_count`` is 30% populated in production per the Phase 0
        audit and is stored in the projection (Section 9.6).

        Regression guard: a previous version of the adapter omitted
        ``retry_count`` from ``_PASSTHROUGH_FIELDS``, which silently
        dropped it and made every record report ``retry_count=0`` when
        the projection flag was on. ``calculate_retry_count`` reads
        ``ai_record.get("retry_count")`` directly — if the adapter
        doesn't carry it through, the has_retry filter and the
        retry_count column on the dashboard are wrong for 30% of records.
        """
        projection = {"retry_count": 3}
        result = projection_to_ai_record(projection)
        assert result["retry_count"] == 3

    def test_thread_id_and_retry_thread_id_are_none(self):
        """Fields not in the projection are explicitly None.

        Both are 0% populated in production per the Phase 0 audit, so
        None matches the direct-read path's behaviour.
        """
        projection = {}
        result = projection_to_ai_record(projection)
        assert result["thread_id"] is None
        assert result["retry_thread_id"] is None

    def test_empty_projection_produces_all_none_ai_record(self):
        """An empty projection dict produces a dict with all expected keys as None."""
        result = projection_to_ai_record({})
        expected_keys = {
            "confidence_level", "is_billable", "billing_category",
            "line_items_save_to_rh_status", "retry_count",
            "claim_processing_status", "agent_exec_status",
            "invoice_total", "processing_time_seconds",
            "thread_id", "retry_thread_id",
            "dept_send_auto_invoice_status",
            "is_cancelled", "cancellation_reason",
            "billing_level", "level_identification_confidence",
            "level_identification_low_confidence",
            "level_identification_reasoning",
            "intake_status", "intake_evaluated_at",
            "intake_evaluation_count", "level_label_matched",
            "_projection_schema_version",
        }
        assert set(result.keys()) == expected_keys
        assert all(
            v is None for k, v in result.items()
            if k != "_projection_schema_version"
        )
        assert result["_projection_schema_version"] == 0

    def test_full_projection_maps_all_fields(self):
        """A complete projection maps every field build_normalized_record reads."""
        projection = {
            "ai_processing_status": "COMPLETED",
            "agent_execution_status": "success",
            "confidence_level": 92,
            "is_billable": True,
            "billing_category": "Fire Suppression",
            "line_items_save_to_rh_status": True,
            "ai_invoice_total": 2500.00,
            "processing_duration_seconds": 18.3,
            "retry_count": 1,
        }
        result = projection_to_ai_record(projection)
        assert result == {
            "claim_processing_status": "COMPLETED",
            "agent_exec_status": "success",
            "confidence_level": 92,
            "is_billable": True,
            "billing_category": "Fire Suppression",
            "line_items_save_to_rh_status": True,
            "invoice_total": 2500.00,
            "processing_time_seconds": 18.3,
            "retry_count": 1,
            "thread_id": None,
            "retry_thread_id": None,
            "billing_level": None,
            "level_identification_confidence": None,
            "level_identification_low_confidence": None,
            "level_identification_reasoning": None,
            "intake_status": None,
            "intake_evaluated_at": None,
            "intake_evaluation_count": None,
            "level_label_matched": None,
            "dept_send_auto_invoice_status": None,
            "is_cancelled": None,
            "cancellation_reason": None,
            "_projection_schema_version": 0,
        }

    def test_internal_schema_version_passthrough(self):
        """The projection's schema version rides along so the cohort loader
        can detect outdated projections for source refresh."""
        result = projection_to_ai_record({"projection_schema_version": 4})
        assert result["_projection_schema_version"] == 4


# ---------------------------------------------------------------------------
# get_projection_records_for_claim_ids — batch fetch
# ---------------------------------------------------------------------------


class TestGetProjectionRecordsForClaimIds:
    """Verify the batch fetch returns adapted dicts keyed by claim_id."""

    @pytest.mark.asyncio
    async def test_empty_claim_ids_returns_empty_dict(self):
        """No claim_ids → no query, empty result."""
        result = await get_projection_records_for_claim_ids(object(), [])
        assert result == {}

    @pytest.mark.asyncio
    async def test_fetches_projections_by_id(self, mock_mongo_db):
        """Projections are fetched by ``_id`` (which is the integer claim_id)."""
        from ai_analytics_worker.config import worker_config

        collection = mock_mongo_db[worker_config.PROJECTIONS_COLLECTION]
        await collection.insert_one({
            "_id": 100,
            "ai_processing_status": "COMPLETED",
            "agent_execution_status": "success",
            "confidence_level": 90,
            "retry_count": 2,
        })
        await collection.insert_one({
            "_id": 200,
            "ai_processing_status": "INITIATED",
            "agent_execution_status": "pending",
            "confidence_level": None,
            "retry_count": 0,
        })

        result = await get_projection_records_for_claim_ids(
            mock_mongo_db, [100, 200, 999]
        )

        # 999 has no projection — absent from result, same as direct-read.
        assert set(result.keys()) == {100, 200}
        assert result[100]["claim_processing_status"] == "COMPLETED"
        assert result[100]["retry_count"] == 2
        assert result[200]["claim_processing_status"] == "INITIATED"
        assert result[200]["retry_count"] == 0

    @pytest.mark.asyncio
    async def test_missing_projection_is_absent_not_none(self, mock_mongo_db):
        """A claim with no projection is absent from the result dict.

        This matches the direct-read path where
        ``get_ai_line_items_for_claim_ids`` omits claims with no AI
        record. ``build_normalized_record`` then receives
        ``ai_record=None`` and handles it gracefully.
        """
        result = await get_projection_records_for_claim_ids(
            mock_mongo_db, [99999]
        )
        assert 99999 not in result
        assert result == {}

    @pytest.mark.asyncio
    async def test_propagates_database_errors(self):
        """A database failure propagates so _load_normalized_cohort can mark it."""
        from unittest.mock import MagicMock

        failing_db = MagicMock()
        failing_collection = MagicMock()
        failing_collection.find.side_effect = RuntimeError("connection lost")
        failing_db.__getitem__ = MagicMock(return_value=failing_collection)

        with pytest.raises(RuntimeError, match="connection lost"):
            await get_projection_records_for_claim_ids(failing_db, [100])


# ---------------------------------------------------------------------------
# _load_normalized_cohort — flag-gated path selection
# ---------------------------------------------------------------------------


class TestLoadNormalizedCohortFlagGating:
    """Verify _load_normalized_cohort uses the projection when the flag is on."""

    @pytest.mark.asyncio
    async def test_flag_off_uses_direct_mongo_read(self, monkeypatch):
        """When AI_ANALYTICS_USE_PROJECTION is false, the direct Mongo path is used."""
        from ai_analytics.outcome_service import _load_normalized_cohort
        from ai_analytics.models import AiAnalyticsFilters
        from config import settings

        monkeypatch.setattr(settings, "AI_ANALYTICS_USE_PROJECTION", False)

        with patch(
            "ai_analytics.outcome_service.sql_repo.get_ai_invoice_cohort"
        ) as mock_cohort, patch(
            "ai_analytics.outcome_service.mongo_repo.get_ai_line_items_for_claim_ids",
            new_callable=AsyncMock,
        ) as mock_mongo, patch(
            "ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims"
        ) as mock_canc, patch(
            "ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims"
        ) as mock_logs:
            # Non-empty cohort so the function proceeds past the early
            # return to the AI-side query step.
            mock_cohort.return_value = [
                {"claim_id": 100, "AI_inv_process_status": 4, "dept_id": 1,
                 "department_name": "FD1", "department_state": "TX",
                 "ai_business_updated_at": "2026-01-15T10:00:00"},
            ]
            mock_mongo.return_value = {}
            mock_canc.return_value = {}
            mock_logs.return_value = {}

            filters = AiAnalyticsFilters()
            records, source_status, data_complete = (
                await _load_normalized_cohort(object(), filters)
            )

            assert mock_mongo.called
            assert len(records) == 1

    @pytest.mark.asyncio
    async def test_flag_on_uses_projection_read(self, monkeypatch, mock_mongo_db):
        """When AI_ANALYTICS_USE_PROJECTION is true, the projection path is used."""
        from ai_analytics.outcome_service import _load_normalized_cohort
        from ai_analytics.models import AiAnalyticsFilters
        from ai_analytics_worker.config import worker_config
        from config import settings
        from database import db_manager

        monkeypatch.setattr(settings, "AI_ANALYTICS_USE_PROJECTION", True)
        # The conftest sets db_manager.db to mock_mongo_db, but we need
        # to ensure it's the same instance the function will import.
        monkeypatch.setattr(db_manager, "db", mock_mongo_db)

        # Seed a projection into the dashboard-owned Mongo.
        collection = mock_mongo_db[worker_config.PROJECTIONS_COLLECTION]
        await collection.insert_one({
            "_id": 100,
            "projection_schema_version": worker_config.projection_schema_version,
            "ai_processing_status": "COMPLETED",
            "agent_execution_status": "success",
            "confidence_level": 90,
            "is_billable": True,
            "billing_category": "Fire Suppression",
            "line_items_save_to_rh_status": True,
            "ai_invoice_total": 1500.00,
            "processing_duration_seconds": 15.5,
            "retry_count": 2,
        })

        with patch(
            "ai_analytics.outcome_service.sql_repo.get_ai_invoice_cohort"
        ) as mock_cohort, patch(
            "ai_analytics.outcome_service.mongo_repo.get_ai_line_items_for_claim_ids",
            new_callable=AsyncMock,
        ) as mock_mongo, patch(
            "ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims"
        ) as mock_canc, patch(
            "ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims"
        ) as mock_logs:
            mock_cohort.return_value = [
                {"claim_id": 100, "AI_inv_process_status": 4, "dept_id": 1,
                 "department_name": "FD1", "department_state": "TX",
                 "ai_business_updated_at": "2026-01-15T10:00:00"},
            ]
            mock_canc.return_value = {}
            mock_logs.return_value = {
                100: [{"log_text": "Invoice to Insurance - Released",
                        "user_id": 7486, "user_type_id": 2}],
            }

            filters = AiAnalyticsFilters()
            records, source_status, data_complete = (
                await _load_normalized_cohort(mock_mongo_db, filters)
            )

            # Direct Mongo read must NOT have been called.
            assert not mock_mongo.called

            # The projection data flowed through build_normalized_record.
            assert len(records) == 1
            record = records[0]
            assert record["claim_id"] == 100
            assert record["ai_processing_status"] == "COMPLETED"
            assert record["agent_execution_status"] == "success"
            assert record["confidence"] == 90
            assert record["is_billable"] is True
            assert record["billing_category"] == "Fire Suppression"
            assert record["invoice_total"] == 1500.00
            assert record["processing_time_seconds"] == 15.5
            assert record["retry_count"] == 2
            # business_outcome comes from the SQL join, not the projection.
            assert record["business_outcome"] == "released"

    @pytest.mark.asyncio
    async def test_projection_missing_claim_treated_as_no_ai_record(
        self, monkeypatch, mock_mongo_db
    ):
        """A claim with a SQL row but no projection gets ai_record=None.

        This matches the direct-read path: if there's no ai_line_items
        document for a claim, build_normalized_record receives None and
        sets ai_record_state="missing".
        """
        from ai_analytics.outcome_service import _load_normalized_cohort
        from ai_analytics.models import AiAnalyticsFilters
        from config import settings
        from database import db_manager

        monkeypatch.setattr(settings, "AI_ANALYTICS_USE_PROJECTION", True)
        monkeypatch.setattr(db_manager, "db", mock_mongo_db)

        # No projections seeded — the projection collection is empty.

        with patch(
            "ai_analytics.outcome_service.sql_repo.get_ai_invoice_cohort"
        ) as mock_cohort, patch(
            "ai_analytics.outcome_service.mongo_repo.get_ai_line_items_for_claim_ids",
            new_callable=AsyncMock,
        ) as mock_mongo, patch(
            "ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims"
        ) as mock_canc, patch(
            "ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims"
        ) as mock_logs:
            mock_cohort.return_value = [
                {"claim_id": 100, "AI_inv_process_status": 4, "dept_id": 1,
                 "department_name": "FD1", "department_state": "TX",
                 "ai_business_updated_at": "2026-01-15T10:00:00"},
            ]
            # No projection → one batched source read; the source also has
            # no doc for this claim, so it stays "missing".
            mock_mongo.return_value = {}
            mock_canc.return_value = {}
            mock_logs.return_value = {}

            filters = AiAnalyticsFilters()
            records, _, _ = await _load_normalized_cohort(
                mock_mongo_db, filters
            )

            assert len(records) == 1
            assert records[0]["ai_record_state"] == "missing"
            assert records[0]["ai_processing_status"] is None

    @pytest.mark.asyncio
    async def test_projection_read_failure_marks_data_incomplete(
        self, monkeypatch
    ):
        """If the projection query fails, source_status reflects it."""
        from ai_analytics.outcome_service import _load_normalized_cohort
        from ai_analytics.models import AiAnalyticsFilters
        from config import settings
        from database import db_manager

        monkeypatch.setattr(settings, "AI_ANALYTICS_USE_PROJECTION", True)

        # Make db_manager.db raise on collection access.
        failing_db = MagicMock()
        failing_db.__getitem__ = MagicMock(
            side_effect=RuntimeError("db is down")
        )
        monkeypatch.setattr(db_manager, "db", failing_db)

        with patch(
            "ai_analytics.outcome_service.sql_repo.get_ai_invoice_cohort"
        ) as mock_cohort, patch(
            "ai_analytics.outcome_service.mongo_repo.get_ai_line_items_for_claim_ids",
            new_callable=AsyncMock,
        ) as mock_mongo, patch(
            "ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims"
        ) as mock_canc, patch(
            "ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims"
        ) as mock_logs:
            mock_cohort.return_value = [
                {"claim_id": 100, "AI_inv_process_status": 4, "dept_id": 1,
                 "department_name": "FD1", "department_state": "TX",
                 "ai_business_updated_at": "2026-01-15T10:00:00"},
            ]
            mock_canc.return_value = {}
            mock_logs.return_value = {}

            filters = AiAnalyticsFilters()
            records, source_status, data_complete = (
                await _load_normalized_cohort(object(), filters)
            )

            assert source_status["recoveryhub_ai_mongo"] == "unavailable"
            assert data_complete is False


# ---------------------------------------------------------------------------
# _load_normalized_cohort — schema-version fallback to the source
# ---------------------------------------------------------------------------


def _cohort_row(claim_id):
    return {
        "claim_id": claim_id, "AI_inv_process_status": 4, "dept_id": 1,
        "department_name": "FD1", "department_state": "TX",
        "ai_business_updated_at": "2026-01-15T10:00:00",
    }


class TestProjectionFallback:
    """Missing or pre-v4 projections get one batched source read so newer
    step-1 fields are available; mature v4 projections trigger none."""

    def _patches(self):
        """Context manager patching the cohort sources; yields
        (mock_cohort, mock_mongo)."""
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            with patch(
                "ai_analytics.outcome_service.sql_repo.get_ai_invoice_cohort"
            ) as mock_cohort, patch(
                "ai_analytics.outcome_service.mongo_repo"
                ".get_ai_line_items_for_claim_ids",
                new_callable=AsyncMock,
            ) as mock_mongo, patch(
                "ai_analytics.outcome_service.sql_repo"
                ".get_cancellation_details_for_claims",
                return_value={},
            ), patch(
                "ai_analytics.outcome_service.sql_repo"
                ".get_process_logs_for_claims",
                return_value={},
            ):
                yield mock_cohort, mock_mongo

        return _ctx()

    @pytest.mark.asyncio
    async def test_missing_and_old_schema_refresh_from_source(
        self, monkeypatch, mock_mongo_db
    ):
        from ai_analytics.outcome_service import _load_normalized_cohort
        from ai_analytics.models import AiAnalyticsFilters
        from ai_analytics_worker.config import worker_config
        from config import settings
        from database import db_manager

        monkeypatch.setattr(settings, "AI_ANALYTICS_USE_PROJECTION", True)
        monkeypatch.setattr(db_manager, "db", mock_mongo_db)

        collection = mock_mongo_db[worker_config.PROJECTIONS_COLLECTION]
        await collection.insert_one({
            "_id": 100,
            "projection_schema_version": 3,  # outdated — no intake fields
            "ai_processing_status": "COMPLETED",
            "billing_category": "Old Category",
        })
        # Claim 200 has no projection at all.

        with self._patches() as (mock_cohort, mock_mongo):
            mock_cohort.return_value = [_cohort_row(100), _cohort_row(200)]
            mock_mongo.return_value = {
                100: {
                    "claim_id": 100,
                    "claim_processing_status": "COMPLETED",
                    "billing_category": "Fresh Category",
                    "intake_status": "IDENTIFIED",
                },
                200: {
                    "claim_id": 200,
                    "claim_processing_status": "COMPLETED",
                    "billing_category": "Recovered",
                },
            }

            records, source_status, data_complete = (
                await _load_normalized_cohort(mock_mongo_db, AiAnalyticsFilters())
            )

        mock_mongo.assert_awaited_once()
        refreshed_ids = set(mock_mongo.await_args.args[1])
        assert refreshed_ids == {100, 200}
        assert source_status["recoveryhub_ai_projection"] == "refreshing"
        by_id = {r["claim_id"]: r for r in records}
        # The old projection's stale value is replaced by the source doc.
        assert by_id[100]["billing_category"] == "Fresh Category"
        assert by_id[100]["intake_status"] == "IDENTIFIED"
        assert by_id[200]["billing_category"] == "Recovered"

    @pytest.mark.asyncio
    async def test_source_failure_retains_old_projection_and_marks_partial(
        self, monkeypatch, mock_mongo_db
    ):
        from ai_analytics.outcome_service import _load_normalized_cohort
        from ai_analytics.models import AiAnalyticsFilters
        from ai_analytics_worker.config import worker_config
        from config import settings
        from database import db_manager

        monkeypatch.setattr(settings, "AI_ANALYTICS_USE_PROJECTION", True)
        monkeypatch.setattr(db_manager, "db", mock_mongo_db)

        collection = mock_mongo_db[worker_config.PROJECTIONS_COLLECTION]
        await collection.insert_one({
            "_id": 100,
            "projection_schema_version": 3,
            "ai_processing_status": "COMPLETED",
            "billing_category": "Old Category",
        })

        with self._patches() as (mock_cohort, mock_mongo):
            mock_cohort.return_value = [_cohort_row(100)]
            mock_mongo.side_effect = RuntimeError("ai mongo down")

            records, source_status, data_complete = (
                await _load_normalized_cohort(mock_mongo_db, AiAnalyticsFilters())
            )

        assert source_status["recoveryhub_ai_projection"] == "partial"
        assert data_complete is False
        # The old projection data is retained, not dropped.
        assert records[0]["billing_category"] == "Old Category"
        assert records[0]["ai_processing_status"] == "COMPLETED"

    @pytest.mark.asyncio
    async def test_current_schema_projection_needs_no_source_read(
        self, monkeypatch, mock_mongo_db
    ):
        from ai_analytics.outcome_service import _load_normalized_cohort
        from ai_analytics.models import AiAnalyticsFilters
        from ai_analytics_worker.config import worker_config
        from config import settings
        from database import db_manager

        monkeypatch.setattr(settings, "AI_ANALYTICS_USE_PROJECTION", True)
        monkeypatch.setattr(db_manager, "db", mock_mongo_db)

        collection = mock_mongo_db[worker_config.PROJECTIONS_COLLECTION]
        await collection.insert_one({
            "_id": 100,
            "projection_schema_version": worker_config.projection_schema_version,
            "ai_processing_status": "COMPLETED",
            "billing_category": "Current Category",
            "intake_status": "IDENTIFIED",
        })

        with self._patches() as (mock_cohort, mock_mongo):
            mock_cohort.return_value = [_cohort_row(100)]

            records, source_status, data_complete = (
                await _load_normalized_cohort(mock_mongo_db, AiAnalyticsFilters())
            )

        mock_mongo.assert_not_awaited()
        assert "recoveryhub_ai_projection" not in source_status
        assert records[0]["billing_category"] == "Current Category"
        assert records[0]["intake_status"] == "IDENTIFIED"


# ---------------------------------------------------------------------------
# Phase 10: projection_to_trace_data — trace field mapping
# ---------------------------------------------------------------------------


class TestProjectionToTraceData:
    """Verify the projection → trace data field mapping (Phase 10)."""

    def test_renames_review_message_to_review_msg(self):
        """Projection's ``review_message`` → raw ``review_msg``."""
        projection = {"review_message": "Auto-approved"}
        result = projection_to_trace_data(projection)
        assert result["review_msg"] == "Auto-approved"

    def test_renames_ai_inserted_at_to_inserted_at(self):
        """Projection's ``ai_inserted_at`` → raw ``inserted_at``."""
        from datetime import datetime
        ts = datetime(2026, 7, 1, 9, 0, 0)
        projection = {"ai_inserted_at": ts}
        result = projection_to_trace_data(projection)
        assert result["inserted_at"] == ts

    def test_renames_ai_updated_at_to_updated_at(self):
        """Projection's ``ai_updated_at`` → raw ``updated_at``."""
        from datetime import datetime
        ts = datetime(2026, 7, 2, 10, 30, 0)
        projection = {"ai_updated_at": ts}
        result = projection_to_trace_data(projection)
        assert result["updated_at"] == ts

    def test_renames_ai_completed_at_to_completed_at(self):
        """Projection's ``ai_completed_at`` → raw ``completed_at``."""
        from datetime import datetime
        ts = datetime(2026, 7, 2, 10, 25, 0)
        projection = {"ai_completed_at": ts}
        result = projection_to_trace_data(projection)
        assert result["completed_at"] == ts

    def test_renames_ai_line_items_to_line_items(self):
        """Projection's ``ai_line_items`` → raw ``line_items`` (with resources)."""
        items = [
            {"item": "Equipment", "quantity": 2, "rate": 500, "resources": []},
        ]
        projection = {"ai_line_items": items}
        result = projection_to_trace_data(projection)
        assert result["line_items"] == items

    def test_conversation_id_passthrough(self):
        """``conversation_id`` passes through unchanged (v2)."""
        projection = {"conversation_id": "conv-abc-123"}
        result = projection_to_trace_data(projection)
        assert result["conversation_id"] == "conv-abc-123"

    def test_thread_id_is_billable_passthrough(self):
        """``thread_id_is_billable`` passes through unchanged (v2)."""
        projection = {"thread_id_is_billable": "yes"}
        result = projection_to_trace_data(projection)
        assert result["thread_id_is_billable"] == "yes"

    def test_incident_duration_passthrough(self):
        """``incident_duration_in_minutes`` passes through unchanged."""
        projection = {"incident_duration_in_minutes": 45}
        result = projection_to_trace_data(projection)
        assert result["incident_duration_in_minutes"] == 45

    def test_conversation_summaries_extracted(self):
        """``conversation_summaries`` is extracted from the projection (v2)."""
        summaries = [
            {"agent": "agent_a", "status": "completed", "conversation_id": "1"},
        ]
        projection = {"conversation_summaries": summaries}
        result = projection_to_trace_data(projection)
        assert result["conversation_summaries"] == summaries

    def test_conversation_summaries_empty_for_v1_projection(self):
        """v1 projections (no conversation_summaries) → empty list."""
        result = projection_to_trace_data({})
        assert result["conversation_summaries"] == []
        assert result["has_ai_line_item_record"] is True

    def test_explicit_missing_ai_record_is_preserved(self):
        """A tombstone-like projection must remain an AI-record-missing trace."""
        result = projection_to_trace_data({"has_ai_line_item_record": False})
        assert result["has_ai_line_item_record"] is False

    def test_includes_all_phase9_fields(self):
        """The trace adapter also maps all Phase 9 fields (superset)."""
        projection = {
            "ai_processing_status": "COMPLETED",
            "agent_execution_status": "success",
            "ai_invoice_total": 1500.00,
            "processing_duration_seconds": 12.5,
            "confidence_level": 85,
            "is_billable": True,
            "billing_category": "Fire",
            "line_items_save_to_rh_status": True,
            "retry_count": 1,
        }
        result = projection_to_trace_data(projection)
        assert result["claim_processing_status"] == "COMPLETED"
        assert result["agent_exec_status"] == "success"
        assert result["invoice_total"] == 1500.00
        assert result["processing_time_seconds"] == 12.5
        assert result["confidence_level"] == 85
        assert result["retry_count"] == 1


# ---------------------------------------------------------------------------
# Phase 10: get_projection_for_trace — single claim fetch
# ---------------------------------------------------------------------------


class TestGetProjectionForTrace:
    """Verify the single-claim trace projection fetch."""

    @pytest.mark.asyncio
    async def test_returns_trace_data_when_projection_exists(self, mock_mongo_db):
        """A v2 projection is fetched and adapted to trace data shape."""
        from ai_analytics_worker.config import worker_config

        collection = mock_mongo_db[worker_config.PROJECTIONS_COLLECTION]
        await collection.insert_one({
            "_id": 100,
            "ai_processing_status": "COMPLETED",
            "review_message": "Auto-approved",
            "ai_line_items": [{"item": "Equipment", "resources": []}],
            "conversation_id": "conv-123",
            "conversation_summaries": [
                {"agent": "agent_a", "status": "completed"},
            ],
        })

        result = await get_projection_for_trace(mock_mongo_db, 100)

        assert result is not None
        assert result["claim_processing_status"] == "COMPLETED"
        assert result["review_msg"] == "Auto-approved"
        assert result["line_items"] == [{"item": "Equipment", "resources": []}]
        assert result["conversation_id"] == "conv-123"
        assert len(result["conversation_summaries"]) == 1

    @pytest.mark.asyncio
    async def test_returns_none_when_no_projection(self, mock_mongo_db):
        """No projection for the claim → None (caller falls back to direct read)."""
        result = await get_projection_for_trace(mock_mongo_db, 99999)
        assert result is None


# ---------------------------------------------------------------------------
# Phase 10: aggregate_agent_stats_from_projections — /diagnostics/agents
# ---------------------------------------------------------------------------


class TestAggregateAgentStatsFromProjections:
    """Verify the projection-based agent stats aggregation."""

    @pytest.mark.asyncio
    async def test_aggregates_by_agent_status_stage_request_type(self, mock_mongo_db):
        """Conversation summaries are grouped by (agent, status, stage, request_type)."""
        from ai_analytics_worker.config import worker_config

        collection = mock_mongo_db[worker_config.PROJECTIONS_COLLECTION]
        await collection.insert_one({
            "_id": 100,
            "conversation_summaries": [
                {"agent": "agent_a", "status": "completed",
                 "processing_stage": "stage_1", "request_type": "incident_analysis",
                 "created_at": "2026-07-01T09:00:00"},
                {"agent": "agent_a", "status": "completed",
                 "processing_stage": "stage_1", "request_type": "incident_analysis",
                 "created_at": "2026-07-01T10:00:00"},
                {"agent": "agent_b", "status": "failed",
                 "processing_stage": "stage_2", "request_type": "billability_check",
                 "created_at": "2026-07-01T11:00:00"},
            ],
        })

        results = await aggregate_agent_stats_from_projections(mock_mongo_db)

        assert len(results) == 2
        # agent_a, completed, stage_1, incident_analysis → count 2
        agent_a = next(
            r for r in results if r["agent"] == "agent_a"
        )
        assert agent_a["count"] == 2
        assert agent_a["status"] == "completed"
        # agent_b, failed, stage_2, billability_check → count 1
        agent_b = next(
            r for r in results if r["agent"] == "agent_b"
        )
        assert agent_b["count"] == 1
        assert agent_b["status"] == "failed"

    @pytest.mark.asyncio
    async def test_v1_projections_contribute_nothing(self, mock_mongo_db):
        """Projections without conversation_summaries produce no results."""
        from ai_analytics_worker.config import worker_config

        collection = mock_mongo_db[worker_config.PROJECTIONS_COLLECTION]
        await collection.insert_one({
            "_id": 100,
            # v1 projection — no conversation_summaries field
            "ai_processing_status": "COMPLETED",
        })

        results = await aggregate_agent_stats_from_projections(mock_mongo_db)
        assert results == []

    @pytest.mark.asyncio
    async def test_date_filter_on_created_at(self, mock_mongo_db):
        """Date range filters on conversation_summaries.created_at."""
        from ai_analytics_worker.config import worker_config

        collection = mock_mongo_db[worker_config.PROJECTIONS_COLLECTION]
        await collection.insert_one({
            "_id": 100,
            "conversation_summaries": [
                {"agent": "agent_a", "status": "completed",
                 "processing_stage": "s1", "request_type": "r1",
                 "created_at": "2026-07-01T09:00:00"},
                {"agent": "agent_b", "status": "completed",
                 "processing_stage": "s2", "request_type": "r2",
                 "created_at": "2026-08-01T09:00:00"},
            ],
        })

        # Filter to only July. end_date is exclusive (caller adds 1 day),
        # so end_date="2026-07-31" → end_exclusive="2026-08-01" which
        # excludes August 1st.
        results = await aggregate_agent_stats_from_projections(
            mock_mongo_db,
            start_date="2026-07-01",
            end_date="2026-07-31",
        )

        # Only the July conversation should be counted
        assert len(results) == 1
        assert results[0]["agent"] == "agent_a"

    @pytest.mark.asyncio
    async def test_claim_ids_restrict_to_matching_projections(self, mock_mongo_db):
        """``claim_ids`` filters on the projection ``_id`` (the claim ID)."""
        from ai_analytics_worker.config import worker_config

        collection = mock_mongo_db[worker_config.PROJECTIONS_COLLECTION]
        await collection.insert_one({
            "_id": 100,
            "conversation_summaries": [
                {"agent": "agent_a", "status": "completed",
                 "processing_stage": "s1", "request_type": "r1",
                 "created_at": "2026-07-01T09:00:00"},
            ],
        })
        await collection.insert_one({
            "_id": 200,
            "conversation_summaries": [
                {"agent": "agent_b", "status": "completed",
                 "processing_stage": "s1", "request_type": "r1",
                 "created_at": "2026-07-01T09:00:00"},
            ],
        })

        results = await aggregate_agent_stats_from_projections(
            mock_mongo_db, claim_ids=[100],
        )

        assert len(results) == 1
        assert results[0]["agent"] == "agent_a"

        assert await aggregate_agent_stats_from_projections(
            mock_mongo_db, claim_ids=[999],
        ) == []


@pytest.mark.asyncio
async def test_projection_without_ai_line_item_record_is_treated_as_missing(mock_mongo_db):
    """A projection written for a claim that has conversations but no
    ai_line_items document must not be surfaced as a present AI record."""
    from ai_analytics.projection_read_repository import (
        get_projection_records_for_claim_ids,
    )
    from ai_analytics_worker.config import worker_config as wc

    await mock_mongo_db[wc.PROJECTIONS_COLLECTION].insert_many([
        {"_id": 1, "has_ai_line_item_record": True, "ai_processing_status": "COMPLETED"},
        {"_id": 2, "has_ai_line_item_record": False},
        {"_id": 3, "ai_processing_status": "COMPLETED"},
    ])
    by_claim = await get_projection_records_for_claim_ids(mock_mongo_db, [1, 2, 3])
    assert set(by_claim) == {1, 3}
