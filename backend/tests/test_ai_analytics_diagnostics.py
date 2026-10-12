"""Tests for AI Analytics diagnostics service and routes."""

import pytest
from unittest.mock import patch, AsyncMock

from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer valid-mock-token"}


# ---------------------------------------------------------------------------
# Diagnostics service tests
# ---------------------------------------------------------------------------

class TestDiagnosticsService:
    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_diagnostics_summary_empty(self, mock_logs, mock_canc, mock_cohort):
        from ai_analytics.diagnostics_service import get_diagnostics_summary
        from ai_analytics.models import AiAnalyticsFilters

        mock_cohort.return_value = []
        filters = AiAnalyticsFilters()
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(get_diagnostics_summary(None, filters))
        assert result.ai_runs == 0
        assert result.completed == 0
        assert result.errors == 0
        assert result.avg_duration is None

    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_diagnostics_summary_with_data(self, mock_logs, mock_canc, mock_cohort):
        from ai_analytics.diagnostics_service import get_diagnostics_summary
        from ai_analytics.models import AiAnalyticsFilters

        from tests.conftest import make_projection_doc
        mock_cohort.return_value = [
            make_projection_doc(
                100,
                sql_row={"dept_id": 1, "department_name": "FD1",
                         "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED", "agent_exec_status": "success",
                           "confidence_level": 90, "line_items_save_to_rh_status": True,
                           "retry_count": 0, "processing_time_seconds": 15.5},
            ),
            make_projection_doc(
                200,
                sql_row={"dept_id": 1, "department_name": "FD1",
                         "ai_business_updated_at": "2026-01-16T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED", "agent_exec_status": "success",
                           "confidence_level": 30, "line_items_save_to_rh_status": False,
                           "retry_count": 2, "processing_time_seconds": 30.0},
            ),
        ]
        mock_canc.return_value = {}
        mock_logs.return_value = {
            100: [{"log_text": "Invoice to Insurance - Released", "user_id": 7486, "user_type_id": 2}],
            200: [{"log_text": "Invoice to Insurance - Cancelled", "user_id": 7486, "user_type_id": 2}],
        }

        filters = AiAnalyticsFilters()
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(get_diagnostics_summary(None, filters))
        assert result.ai_runs == 2
        assert result.completed == 2
        assert result.retries == 1  # claim 200 has retry_count=2
        assert result.low_confidence == 1  # claim 200 has confidence=30
        assert result.writeback_failures == 1  # claim 200 has writeback=False
        assert result.avg_duration == 22.75  # (15.5 + 30.0) / 2

    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_sql_detail_failure_marks_data_incomplete(
        self, mock_logs, mock_canc, mock_cohort,
    ):
        from ai_analytics.diagnostics_service import get_diagnostics_summary
        from ai_analytics.models import AiAnalyticsFilters
        import asyncio

        from tests.conftest import make_projection_doc
        # Claim must have reached the review grid (save_to_rh True) for the
        # SQL enrichment — the only SQL touchpoint — to be attempted.
        mock_cohort.return_value = [
            make_projection_doc(
                100,
                ai_record={"claim_processing_status": "COMPLETED",
                           "line_items_save_to_rh_status": True},
            ),
        ]
        mock_canc.side_effect = RuntimeError("cancellation query failed")
        mock_logs.return_value = {}

        result = asyncio.get_event_loop().run_until_complete(
            get_diagnostics_summary(None, AiAnalyticsFilters())
        )
        assert result.data_complete is False
        assert result.source_status["recoveryhub_sql"] == "partial"


# ---------------------------------------------------------------------------
# Diagnostics route tests
# ---------------------------------------------------------------------------

class TestDiagnosticsRoutes:
    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_diagnostics_summary_route(self, mock_logs, mock_canc, mock_cohort, test_client):
        mock_cohort.return_value = []
        response = test_client.get("/api/ai-analytics/diagnostics/summary", headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        assert data["ai_runs"] == 0
        assert data["completed"] == 0

    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_diagnostics_confidence_route(self, mock_logs, mock_canc, mock_cohort, test_client):
        from tests.conftest import make_projection_doc
        mock_cohort.return_value = [
            make_projection_doc(
                100,
                sql_row={"dept_id": 1, "department_name": "FD1",
                         "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED", "confidence_level": 85,
                           "line_items_save_to_rh_status": True},
            ),
        ]
        mock_canc.return_value = {}
        mock_logs.return_value = {
            100: [{"log_text": "Invoice to Insurance - Released", "user_id": 7486, "user_type_id": 2}],
        }

        response = test_client.get("/api/ai-analytics/diagnostics/confidence", headers=AUTH)
        assert response.status_code == 200
        buckets = response.json()
        assert len(buckets) == 1
        assert buckets[0]["bucket"] == "80-89"
        assert buckets[0]["count"] == 1
        assert buckets[0]["released"] == 1

    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_diagnostics_retries_route(self, mock_logs, mock_canc, mock_cohort, test_client):
        mock_cohort.return_value = []
        response = test_client.get("/api/ai-analytics/diagnostics/retries", headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        assert data["total_records"] == 0
        assert data["records_with_retries"] == 0


# ---------------------------------------------------------------------------
# Phase 10 projection service path
# ---------------------------------------------------------------------------


def _qualified_cohort():
    """One eligible claim with an ai_line_items record."""
    return (
        [{"claim_id": 100, "ai_record_state": "present",
          "ai_eligibility": "eligible"}],
        {},
        True,
    )


@pytest.mark.asyncio
async def test_agent_stats_uses_projection_when_flag_enabled(monkeypatch):
    """The service-level flag branch must call projection aggregation, not raw Mongo."""
    from ai_analytics.diagnostics_service import get_agent_stats
    from ai_analytics.models import AiAnalyticsFilters
    from config import settings

    monkeypatch.setattr(settings, "AI_ANALYTICS_USE_PROJECTION", True)
    projection_results = [{
        "agent": "agent-a",
        "status": "completed",
        "processing_stage": "stage-1",
        "request_type": "incident_analysis",
        "count": 3,
    }]

    with patch(
        "ai_analytics.diagnostics_service.projection_repo.aggregate_agent_stats_from_projections",
        new_callable=AsyncMock,
        return_value=projection_results,
    ) as aggregate, patch(
        "ai_analytics.diagnostics_service.mongo_repo.AGENT_CONVERSATIONS_COLLECTION",
        "should-not-be-used",
    ), patch(
        "ai_analytics.diagnostics_service._load_normalized_cohort",
        new_callable=AsyncMock,
        return_value=_qualified_cohort(),
    ):
        stats = await get_agent_stats(object(), AiAnalyticsFilters())

    aggregate.assert_awaited_once()
    # Qualified AI-run claim IDs are forwarded to the projection aggregation
    assert aggregate.await_args.kwargs["claim_ids"] == [100]
    assert stats[0].agent == "agent-a"
    assert stats[0].count == 3


@pytest.mark.asyncio
async def test_agent_stats_projection_failure_returns_empty(monkeypatch):
    """Projection aggregation errors are contained at the diagnostics boundary."""
    from ai_analytics.diagnostics_service import get_agent_stats
    from ai_analytics.models import AiAnalyticsFilters
    from config import settings

    monkeypatch.setattr(settings, "AI_ANALYTICS_USE_PROJECTION", True)
    with patch(
        "ai_analytics.diagnostics_service.projection_repo.aggregate_agent_stats_from_projections",
        new_callable=AsyncMock,
        side_effect=RuntimeError("projection unavailable"),
    ), patch(
        "ai_analytics.diagnostics_service._load_normalized_cohort",
        new_callable=AsyncMock,
        return_value=_qualified_cohort(),
    ):
        assert await get_agent_stats(object(), AiAnalyticsFilters()) == []


@pytest.mark.asyncio
async def test_agent_stats_empty_when_no_qualified_ai_runs():
    """No qualified AI runs → no aggregation call at all."""
    from ai_analytics.diagnostics_service import get_agent_stats
    from ai_analytics.models import AiAnalyticsFilters

    empty_cohort = (
        [{"claim_id": 200, "ai_record_state": "present",
          "ai_eligibility": "not_configured"}],
        {},
        True,
    )
    with patch(
        "ai_analytics.diagnostics_service._load_normalized_cohort",
        new_callable=AsyncMock,
        return_value=empty_cohort,
    ), patch(
        "ai_analytics.diagnostics_service.projection_repo.aggregate_agent_stats_from_projections",
        new_callable=AsyncMock,
    ) as aggregate:
        assert await get_agent_stats(object(), AiAnalyticsFilters()) == []
    aggregate.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_stats_direct_path_filters_by_claim_id(mock_mongo_db):
    """Direct-read aggregation only counts conversations of qualified claims."""
    from ai_analytics.diagnostics_service import get_agent_stats
    from ai_analytics.models import AiAnalyticsFilters
    from ai_analytics import mongo_repository as mongo_repo

    conversations = mock_mongo_db[mongo_repo.AGENT_CONVERSATIONS_COLLECTION]
    await conversations.insert_one({
        "claim_id": 100, "agent": "agent-a", "status": "completed",
        "processing_stage": "s1", "request_type": "r1",
        "created_at": "2026-07-01T09:00:00",
    })
    await conversations.insert_one({
        "claim_id": 200, "agent": "agent-b", "status": "completed",
        "processing_stage": "s1", "request_type": "r1",
        "created_at": "2026-07-01T09:00:00",
    })

    with patch(
        "ai_analytics.diagnostics_service._load_normalized_cohort",
        new_callable=AsyncMock,
        return_value=_qualified_cohort(),
    ):
        stats = await get_agent_stats(mock_mongo_db, AiAnalyticsFilters())

    assert len(stats) == 1
    assert stats[0].agent == "agent-a"

    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_diagnostics_writeback_route(self, mock_logs, mock_canc, mock_cohort, test_client):
        mock_cohort.return_value = []
        response = test_client.get("/api/ai-analytics/diagnostics/writeback", headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        assert data["total_records"] == 0
        assert data["failure_count"] == 0

    def test_diagnostics_agents_route(self, test_client):
        """Agent stats route — uses mock mongo from conftest."""
        response = test_client.get("/api/ai-analytics/diagnostics/agents", headers=AUTH)
        assert response.status_code == 200
        # With empty mock mongo, should return empty list
        data = response.json()
        assert isinstance(data, list)


# ---------------------------------------------------------------------------
# AI eligibility gating
# ---------------------------------------------------------------------------

class TestDiagnosticsEligibility:
    @patch("ai_analytics.outcome_service.get_ai_participation_map", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_summary_excludes_nonqualifying_records(
        self, mock_logs, mock_canc, mock_cohort, mock_part,
    ):
        from ai_analytics.diagnostics_service import get_diagnostics_summary
        from ai_analytics.models import AiAnalyticsFilters
        import asyncio

        mock_part.return_value = {
            1: {"uses_ai": True, "ai_mode": "auto"},
            2: {"uses_ai": False, "ai_mode": "not_using_ai"},
        }
        from tests.conftest import make_projection_doc
        mock_cohort.return_value = [
            make_projection_doc(
                100,
                sql_row={"dept_id": 1, "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED",
                           "agent_exec_status": "success",
                           "line_items_save_to_rh_status": False,
                           "processing_time_seconds": 10.0},
            ),
            make_projection_doc(
                200,
                sql_row={"dept_id": 2, "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED",
                           "agent_exec_status": "success",
                           "line_items_save_to_rh_status": False,
                           "processing_time_seconds": 99.0},
            ),
            # dept 2 claim with no ai_line_items doc — conversation-only
            # trace, excluded from the Mongo-first cohort entirely.
            make_projection_doc(
                300,
                sql_row={"dept_id": 2, "ai_business_updated_at": "2026-01-15T10:00:00"},
                has_ai_line_item_record=False,
            ),
        ]
        mock_canc.return_value = {}
        mock_logs.return_value = {}

        result = asyncio.get_event_loop().run_until_complete(
            get_diagnostics_summary(None, AiAnalyticsFilters())
        )
        assert result.ai_runs == 1          # only claim 100 qualifies
        assert result.did_not_qualify == 1  # claim 200 (dept 2); claim 300 has no AI record → not in cohort
        assert result.completed == 1
        assert result.avg_duration == 10.0  # claim 200's 99s excluded


@pytest.mark.asyncio
async def test_agent_stats_direct_path_matches_nested_claim_ids(mock_mongo_db):
    """Direct-read aggregation must find conversations that key the claim
    under ``incident_json.claim_id`` / ``input_data.claim_id`` or as a
    string — the same locations the per-claim lookup matches."""
    from ai_analytics.diagnostics_service import get_agent_stats
    from ai_analytics.models import AiAnalyticsFilters
    from ai_analytics import mongo_repository as mongo_repo

    common = {
        "status": "completed", "processing_stage": "s1",
        "request_type": "r1", "created_at": "2026-07-01T09:00:00",
    }
    conversations = mock_mongo_db[mongo_repo.AGENT_CONVERSATIONS_COLLECTION]
    await conversations.insert_many([
        {"claim_id": "100", "agent": "top-string", **common},
        {"incident_json": {"claim_id": 100}, "agent": "incident", **common},
        {"input_data": {"claim_id": "100"}, "agent": "input", **common},
        {"input_data": {"claim_id": 200}, "agent": "other-claim", **common},
    ])

    with patch(
        "ai_analytics.diagnostics_service._load_normalized_cohort",
        new_callable=AsyncMock,
        return_value=_qualified_cohort(),
    ):
        stats = await get_agent_stats(mock_mongo_db, AiAnalyticsFilters())

    assert sorted(s.agent for s in stats) == ["incident", "input", "top-string"]
