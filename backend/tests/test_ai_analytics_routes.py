"""Integration tests for AI Analytics routes.

Uses the test_client fixture and mocks the SQL + Mongo data layers.
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer valid-mock-token"}


# ---------------------------------------------------------------------------
# Summary endpoint
# ---------------------------------------------------------------------------

class TestOutcomesSummaryRoute:
    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_summary_empty_cohort(
        self, mock_logs, mock_canc, mock_cohort, test_client,
    ):
        mock_cohort.return_value = []
        response = test_client.get("/api/ai-analytics/outcomes/summary", headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        assert data["total_ai_invoices"] == 0
        assert data["released"] == 0
        assert data["cancelled_rejected"] == 0
        assert data["business_release_rate"] == 0.0

    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_summary_with_data(
        self, mock_logs, mock_canc, mock_cohort, test_client,
    ):
        # 3 claims: 1 released, 1 cancelled, 1 pending
        from tests.conftest import make_projection_doc
        mock_cohort.return_value = [
            make_projection_doc(
                100,
                sql_row={"dept_id": 1, "department_name": "FD1", "run_number": "R1",
                         "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED", "agent_exec_status": "success",
                           "confidence_level": 90, "line_items_save_to_rh_status": True,
                           "billing_category": "Motor Vehicle Accident", "retry_count": 0},
            ),
            make_projection_doc(
                200,
                sql_row={"dept_id": 1, "department_name": "FD1", "run_number": "R2",
                         "ai_business_updated_at": "2026-01-16T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED", "agent_exec_status": "success",
                           "confidence_level": 50, "line_items_save_to_rh_status": True,
                           "billing_category": "Motor Vehicle Accident", "retry_count": 0},
            ),
            make_projection_doc(
                300,
                sql_row={"dept_id": 1, "department_name": "FD1", "run_number": "R3",
                         "ai_business_updated_at": "2026-01-17T10:00:00"},
                ai_record={"claim_processing_status": "INITIATED", "agent_exec_status": "in_progress",
                           "confidence_level": None, "line_items_save_to_rh_status": False,
                           "billing_category": None, "retry_count": 0},
            ),
        ]
        mock_canc.return_value = {
            200: {"reason_id": 7, "raw_reason": "Miscalculated Nested Line Items",
                  "reason_descr": "Wrong qty"},
        }
        mock_logs.return_value = {
            100: [{"log_text": "Invoice to Insurance - Released", "user_id": 7486, "user_type_id": 2}],
            200: [{"log_text": "Invoice to Insurance - Cancelled", "user_id": 7486, "user_type_id": 2}],
            300: [{"log_text": "Line Item Created", "user_id": 10499, "user_type_id": 1}],
        }

        response = test_client.get("/api/ai-analytics/outcomes/summary", headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        assert data["total_ai_invoices"] == 3
        assert data["released"] == 1
        assert data["cancelled_rejected"] == 1
        assert data["pending"] == 1
        assert data["terminal_count"] == 2
        assert data["business_release_rate"] == 50.0
        assert data["rejection_rate"] == 50.0
        assert data["ai_completed"] == 2
        assert data["writeback_success"] == 2


# ---------------------------------------------------------------------------
# Funnel endpoint
# ---------------------------------------------------------------------------

class TestOutcomesFunnelRoute:
    @patch("ai_analytics.outcome_service.sql_repo.get_claim_lifecycle_for_claims")
    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_funnel_empty(self, mock_logs, mock_canc, mock_cohort, mock_life, test_client):
        mock_cohort.return_value = []
        response = test_client.get("/api/ai-analytics/outcomes/funnel", headers=AUTH)
        assert response.status_code == 200
        stages = response.json()
        assert len(stages) == 6
        assert all(s["count"] == 0 for s in stages)

    @patch("ai_analytics.outcome_service.sql_repo.get_claim_lifecycle_for_claims")
    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_funnel_with_data(self, mock_logs, mock_canc, mock_cohort, mock_life, test_client):
        from tests.conftest import make_projection_doc
        mock_cohort.return_value = [
            make_projection_doc(
                100,
                sql_row={"dept_id": 1, "department_name": "FD1",
                         "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED", "agent_exec_status": "success",
                           "confidence_level": 90, "line_items_save_to_rh_status": True,
                           "billing_category": "Motor Vehicle Accident",
                           "billing_level": "Motor Vehicle Incident Level 1",
                           "invoice_total": 500.0, "retry_count": 0,
                           "dept_send_auto_invoice_status": 2},
            ),
        ]
        mock_life.return_value = {100: {"type_id": 4, "status": "Payment Received", "archived": 0}}
        mock_canc.return_value = {}
        mock_logs.return_value = {
            100: [{"log_text": "Invoice to Insurance - Released", "user_id": 7486, "user_type_id": 2}],
        }

        response = test_client.get("/api/ai-analytics/outcomes/funnel", headers=AUTH)
        assert response.status_code == 200
        stages = response.json()
        assert len(stages) == 6

        # Section 1: fee card + value plugged in
        assert stages[0]["count"] == 1
        cards = {b["label"]: b for b in stages[0]["breakdown"]}
        assert cards["Motor Vehicle Incident Level 1"]["count"] == 1
        assert cards["Motor Vehicle Incident Level 1"]["value"] == 500.0

        # Section 2: AI runs submitted this period, moved on
        assert stages[1]["count"] == 1
        submitted = {b["label"]: b["count"] for b in stages[1]["breakdown"]}
        assert submitted["Moved on — active runs or beyond"] == 1
        assert submitted["Still in new runs — AI working or awaiting grid"] == 0

        # Section 3: left New Runs — claim status at handoff
        assert stages[2]["count"] == 1
        left_new = {b["label"]: b["count"] for b in stages[2]["breakdown"]}
        assert left_new["Payment Received"] == 1

        # Section 4: HITL grid vs elsewhere
        assert stages[3]["count"] == 1
        hitl = {b["label"]: b["count"] for b in stages[3]["breakdown"]}
        assert hitl["Went to human-in-the-loop grid"] == 1
        assert hitl.get("Went elsewhere — finished without the review grid", 0) == 0

        # Section 5: nothing pending on the grid (it was released)
        assert stages[4]["count"] == 0

        # Section 6: left the grid — accepted/released with the RH status
        assert stages[5]["count"] == 1
        left_grid = {b["label"]: b["count"] for b in stages[5]["breakdown"]}
        assert left_grid["Accepted / Released — Payment Received"] == 1

    @patch("ai_analytics.outcome_service.sql_repo.get_claim_lifecycle_for_claims")
    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_funnel_step1_breakdown(
        self, mock_logs, mock_canc, mock_cohort, mock_life, test_client,
    ):
        """Section 1 breaks down by the actual fee card (billing level,
        else category) — no separate 'category no level' bucket — and
        section 2 splits the period cohort by where runs are now."""
        from tests.conftest import make_projection_doc
        mock_cohort.return_value = [
            make_projection_doc(
                100,
                sql_row={"dept_id": 1, "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED",
                           "billing_level": "Motor Vehicle Incident Level 1",
                           "line_items_save_to_rh_status": True,
                           "dept_send_auto_invoice_status": 0},
            ),
            make_projection_doc(
                200,
                sql_row={"dept_id": 1, "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED",
                           "billing_level": "Motor Vehicle Incident Level 1",
                           "line_items_save_to_rh_status": True,
                           "dept_send_auto_invoice_status": 2},
            ),
            make_projection_doc(
                300,
                sql_row={"dept_id": 1, "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "BILLING_LEVEL_NOT_ENABLED",
                           "billing_category": "Motor Vehicle Accident",
                           "line_items_save_to_rh_status": False},
            ),
        ]
        # 100/200 became Active Runs (typeId 4); 300 is still a New Run.
        mock_life.return_value = {
            100: {"type_id": 4, "status": "Ready to Invoice Insurance", "archived": 0},
            200: {"type_id": 4, "status": "Ready to Invoice Insurance", "archived": 0},
            300: {"type_id": 1, "status": "Unassigned", "archived": 0},
        }
        mock_canc.return_value = {}
        mock_logs.return_value = {}

        response = test_client.get("/api/ai-analytics/outcomes/funnel", headers=AUTH)
        assert response.status_code == 200
        stages = response.json()
        assert len(stages) == 6

        # Section 1: fee cards — the level appears twice, the category-only
        # run appears under its category (the card family).
        assert stages[0]["count"] == 3
        cards = {b["label"]: b["count"] for b in stages[0]["breakdown"]}
        assert cards["Motor Vehicle Incident Level 1"] == 2
        assert cards["Motor Vehicle Accident"] == 1
        assert "Category identified (no level)" not in cards
        assert "Not identified" not in cards

        # Section 2: period cohort split by where runs are now.
        assert stages[1]["count"] == 3
        submitted = {b["label"]: b["count"] for b in stages[1]["breakdown"]}
        assert submitted["Still in new runs — AI working or awaiting grid"] == 1
        assert submitted["Moved on — active runs or beyond"] == 2

        # Section 3: left New Runs — 100/200, grouped by claim status.
        assert stages[2]["count"] == 2
        left_new = {b["label"]: b["count"] for b in stages[2]["breakdown"]}
        assert left_new["Ready to Invoice Insurance"] == 2

        # Section 4: of the FINISHED runs (100/200), both went to the
        # grid; 300 never finished (not enabled) so it is in neither row.
        assert stages[3]["count"] == 2
        hitl = {b["label"]: b["count"] for b in stages[3]["breakdown"]}
        assert hitl["Went to human-in-the-loop grid"] == 2
        assert hitl["Went elsewhere — finished without the review grid"] == 0

        # Section 5: 100/200 are pending on the grid now.
        assert stages[4]["count"] == 2

        # Section 6: nothing has left the grid yet.
        assert stages[5]["count"] == 0

    @patch("ai_analytics.outcome_service.sql_repo.get_claim_lifecycle_for_claims")
    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_funnel_ai_side_cancellation(
        self, mock_logs, mock_canc, mock_cohort, mock_life, test_client,
    ):
        """is_cancelled on the ai_line_items doc counts as rejected even
        without a SQL cancellation record or cancelled log — section 6
        shows the rejection with its reason."""
        from tests.conftest import make_projection_doc
        mock_cohort.return_value = [
            make_projection_doc(
                100,
                sql_row={"dept_id": 1, "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED",
                           "line_items_save_to_rh_status": True,
                           "is_cancelled": True,
                           "cancellation_reason": "Wrong Level Selected"},
            ),
        ]
        mock_life.return_value = {100: {"type_id": 4, "status": "Unassigned", "archived": 0}}
        mock_canc.return_value = {}
        mock_logs.return_value = {}

        response = test_client.get("/api/ai-analytics/outcomes/funnel", headers=AUTH)
        assert response.status_code == 200
        stages = response.json()

        # Section 4: it went to the grid.
        hitl = {b["label"]: b["count"] for b in stages[3]["breakdown"]}
        assert hitl["Went to human-in-the-loop grid"] == 1

        # Section 5: it is not pending on the grid (it was rejected).
        assert stages[4]["count"] == 0

        # Section 6: left the grid as rejected, with the reason.
        assert stages[5]["count"] == 1
        left_grid = {b["label"]: b["count"] for b in stages[5]["breakdown"]}
        assert left_grid["Rejected"] == 1
        assert left_grid["Rejected — Wrong Level Selected"] == 1


# ---------------------------------------------------------------------------
# Rejection reasons endpoint
# ---------------------------------------------------------------------------

class TestRejectionReasonsRoute:
    @patch("ai_analytics.outcome_service.sql_repo.get_claim_lifecycle_for_claims")
    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_rejection_reasons(self, mock_logs, mock_canc, mock_cohort, mock_life, test_client):
        from tests.conftest import make_projection_doc
        # Grid claims (save_to_rh True) — the only claims whose terminal
        # outcome evidence is fetched from SQL.
        mock_cohort.return_value = [
            make_projection_doc(
                100,
                sql_row={"dept_id": 1, "department_name": "FD1",
                         "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED",
                           "line_items_save_to_rh_status": True},
            ),
            make_projection_doc(
                200,
                sql_row={"dept_id": 1, "department_name": "FD1",
                         "ai_business_updated_at": "2026-01-16T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED",
                           "line_items_save_to_rh_status": True},
            ),
        ]
        mock_life.return_value = {
            100: {"type_id": 4, "status": "Unassigned", "archived": 0},
            200: {"type_id": 4, "status": "Unassigned", "archived": 0},
        }
        mock_canc.return_value = {
            100: {"reason_id": 7, "raw_reason": "Miscalculated Nested Line Items",
                  "reason_descr": "Wrong qty"},
            200: {"reason_id": 1, "raw_reason": "Incorrect line item description",
                  "reason_descr": "Typo"},
        }
        mock_logs.return_value = {
            100: [{"log_text": "Invoice to Insurance - Cancelled", "user_id": 7486, "user_type_id": 2}],
            200: [{"log_text": "Invoice to Insurance - Cancelled", "user_id": 7486, "user_type_id": 2}],
        }

        response = test_client.get("/api/ai-analytics/outcomes/rejection-reasons", headers=AUTH)
        assert response.status_code == 200
        stats = response.json()
        assert len(stats) == 2
        # Sorted by count descending — both have count 1, so order may vary
        categories = {s["normalized_category"] for s in stats}
        assert "fee_calculation" in categories
        assert "line_item_accuracy" in categories
        for s in stats:
            assert s["percent"] == 50.0
            assert len(s["raw_reason_breakdown"]) == 1

    @patch("ai_analytics.outcome_service.sql_repo.get_claim_lifecycle_for_claims")
    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_rejection_reason_filter(
        self, mock_logs, mock_canc, mock_cohort, mock_life, test_client,
    ):
        from tests.conftest import make_projection_doc
        mock_cohort.return_value = [
            make_projection_doc(
                100, sql_row={"dept_id": 1},
                ai_record={"claim_processing_status": "COMPLETED",
                           "line_items_save_to_rh_status": True},
            ),
            make_projection_doc(
                200, sql_row={"dept_id": 1},
                ai_record={"claim_processing_status": "COMPLETED",
                           "line_items_save_to_rh_status": True},
            ),
        ]
        mock_life.return_value = {
            100: {"type_id": 4, "status": "Unassigned", "archived": 0},
            200: {"type_id": 4, "status": "Unassigned", "archived": 0},
        }
        mock_canc.return_value = {
            100: {"reason_id": 7, "raw_reason": "Miscalculated Nested Line Items"},
            200: {"reason_id": 1, "raw_reason": "Incorrect line item description"},
        }
        mock_logs.return_value = {
            100: [{"log_text": "Invoice to Insurance - Cancelled"}],
            200: [{"log_text": "Invoice to Insurance - Cancelled"}],
        }

        response = test_client.get(
            "/api/ai-analytics/outcomes/rejection-reasons?reason_category=fee_calculation",
            headers=AUTH,
        )
        assert response.status_code == 200
        stats = response.json()
        assert [s["normalized_category"] for s in stats] == ["fee_calculation"]
        assert stats[0]["count"] == 1


# ---------------------------------------------------------------------------
# Invoice cohort endpoint
# ---------------------------------------------------------------------------

class TestInvoiceCohortRoute:
    @patch("ai_analytics.outcome_service.sql_repo.get_claim_lifecycle_for_claims")
    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_invoice_cohort_pagination(self, mock_logs, mock_canc, mock_cohort, mock_life, test_client):
        # Create 5 claims — minimal in-flight AI records so they enter the
        # Mongo-first cohort.
        from tests.conftest import make_projection_doc
        mock_cohort.return_value = [
            make_projection_doc(
                i,
                sql_row={"dept_id": 1, "department_name": "FD1",
                         "run_number": f"R{i}",
                         "ai_business_updated_at": f"2026-01-{i:02d}T10:00:00"},
                ai_record={"claim_processing_status": "INITIATED",
                           "agent_exec_status": "in_progress",
                           "line_items_save_to_rh_status": False},
            )
            for i in range(1, 6)
        ]
        mock_life.return_value = {
            i: {"type_id": 1, "status": "Unassigned", "archived": 0}
            for i in range(1, 6)
        }
        mock_canc.return_value = {}
        mock_logs.return_value = {}

        # Page 1 with page_size 2
        response = test_client.get("/api/ai-analytics/outcomes/invoices?page=1&page_size=2", headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        assert data["total_count"] == 5
        assert data["page"] == 1
        assert data["page_size"] == 2
        assert len(data["invoices"]) == 2

        # Page 3 with page_size 2 → only 1 item
        response = test_client.get("/api/ai-analytics/outcomes/invoices?page=3&page_size=2", headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        assert len(data["invoices"]) == 1


# ---------------------------------------------------------------------------
# Billability endpoint
# ---------------------------------------------------------------------------

class TestBillabilityRoute:
    @patch("ai_analytics.outcome_service.sql_repo.get_claim_lifecycle_for_claims")
    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_billability_stats(self, mock_logs, mock_canc, mock_cohort, mock_life, test_client):
        from tests.conftest import make_projection_doc
        mock_cohort.return_value = [
            make_projection_doc(
                100,
                sql_row={"dept_id": 1, "department_name": "FD1",
                         "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED",
                           "billing_category": "Motor Vehicle Accident"},
            ),
            make_projection_doc(
                200,
                sql_row={"dept_id": 1, "department_name": "FD1",
                         "ai_business_updated_at": "2026-01-16T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED",
                           "billing_category": None},
            ),
        ]
        mock_life.return_value = {
            100: {"type_id": 1, "status": "Unassigned", "archived": 0},
            200: {"type_id": 1, "status": "Unassigned", "archived": 0},
        }
        mock_canc.return_value = {}
        mock_logs.return_value = {}

        response = test_client.get("/api/ai-analytics/billability/stats", headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        assert data["ai_records"] == 2
        assert data["billability_determined"] == 1
        assert data["billability_undetermined"] == 1
        assert data["billable"] == 1
        assert "Motor Vehicle Accident" in data["billing_category_distribution"]


# ---------------------------------------------------------------------------
# Date span validation
# ---------------------------------------------------------------------------

class TestDateSpanValidation:
    def test_date_span_exceeds_max(self, test_client):
        # 400 days apart
        response = test_client.get(
            "/api/ai-analytics/outcomes/summary?start_date=2025-01-01&end_date=2026-02-05"
        , headers=AUTH)
        assert response.status_code == 400
        assert "Date span" in response.json()["detail"]

    def test_valid_date_span(self, test_client):
        # Mock the cohort to return empty so we don't hit the DB
        with patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock, return_value=[]):
            response = test_client.get(
                "/api/ai-analytics/outcomes/summary?start_date=2026-01-01&end_date=2026-01-31"
            , headers=AUTH)
            assert response.status_code == 200


# ---------------------------------------------------------------------------
# Shared route error handling (_handle_route_errors decorator)
# ---------------------------------------------------------------------------

class TestRouteErrorHandling:
    """The decorator must preserve a handler's own HTTPException status codes
    while converting ValueError to 400 and anything else to a generic 500."""

    def test_handler_raised_http_exception_keeps_its_status(self, test_client):
        # The trend handler validates `grain` itself and raises HTTPException(400)
        # from inside the decorated function body. A decorator that caught this
        # as a generic error would report 500 instead.
        response = test_client.get(
            "/api/ai-analytics/outcomes/trend?grain=bogus", headers=AUTH
        )
        assert response.status_code == 400
        assert "grain must be one of" in response.json()["detail"]

    def test_value_error_becomes_400_with_message(self, test_client):
        # Patch the service function in the routes module namespace — that's
        # the symbol the decorated handler actually calls, so the ValueError
        # propagates straight up to _handle_route_errors (the underlying
        # outcome_service swallows repo errors, so patching the repo wouldn't
        # exercise the decorator's ValueError branch).
        with patch(
            "ai_analytics_routes.get_outcome_summary",
            side_effect=ValueError("bad filter value"),
        ):
            response = test_client.get(
                "/api/ai-analytics/outcomes/summary", headers=AUTH
            )
        assert response.status_code == 400
        assert response.json()["detail"] == "bad filter value"

    def test_unexpected_error_becomes_generic_500(self, test_client):
        with patch(
            "ai_analytics_routes.get_outcome_summary",
            side_effect=RuntimeError("connection reset by peer"),
        ):
            response = test_client.get(
                "/api/ai-analytics/outcomes/summary", headers=AUTH
            )
        assert response.status_code == 500
        # Internal details must not leak to the client
        assert response.json()["detail"] == "Internal server error"
        assert "connection reset" not in response.text

    def test_decorated_handlers_keep_their_query_params(self, test_client):
        """functools.wraps preserves __wrapped__, so FastAPI still resolves the
        original signature — query params and their validation stay intact."""
        # page_size has le=250; exceeding it must still be a 422 from FastAPI,
        # which proves the decorator did not erase the parameter metadata.
        response = test_client.get(
            "/api/ai-analytics/outcomes/invoices?page_size=9999", headers=AUTH
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# AI eligibility gating (department fee-tile configuration)
# ---------------------------------------------------------------------------

def _participation():
    """Dept 1 has a qualifying AI fee tile; dept 2 does not."""
    return {
        1: {"uses_ai": True, "ai_mode": "auto", "qualifying_fee_count": 1,
            "has_auto": True, "has_queued": False, "has_limited_auto": False},
        2: {"uses_ai": False, "ai_mode": "not_using_ai",
            "qualifying_fee_count": 0, "has_auto": False,
            "has_queued": False, "has_limited_auto": False},
    }


class TestAiEligibilityGating:
    @patch("ai_analytics.outcome_service.sql_repo.get_claim_lifecycle_for_claims")
    @patch("ai_analytics.outcome_service.get_ai_participation_map", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_summary_counts_qualified_and_did_not_qualify(
        self, mock_logs, mock_canc, mock_cohort, mock_part, mock_life, test_client,
    ):
        mock_part.return_value = _participation()
        from tests.conftest import make_projection_doc
        mock_cohort.return_value = [
            make_projection_doc(
                100, sql_row={"dept_id": 1,
                              "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "INITIATED",
                           "agent_exec_status": "in_progress",
                           "line_items_save_to_rh_status": False},
            ),
            make_projection_doc(
                200, sql_row={"dept_id": 2,
                              "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "INITIATED",
                           "agent_exec_status": "in_progress",
                           "line_items_save_to_rh_status": False},
            ),
        ]
        mock_life.return_value = {
            100: {"type_id": 1, "status": "Unassigned", "archived": 0},
            200: {"type_id": 1, "status": "Unassigned", "archived": 0},
        }

        response = test_client.get("/api/ai-analytics/outcomes/summary", headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        assert data["total_ai_invoices"] == 1
        assert data["did_not_qualify"] == 1
        assert data["source_status"]["recoveryhub_ai_fee_config"] == "available"
        assert data["data_complete"] is True

    @patch("ai_analytics.outcome_service.sql_repo.get_claim_lifecycle_for_claims")
    @patch("ai_analytics.outcome_service.get_ai_participation_map", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_funnel_intake_partition_and_nonqualifying_exclusion(
        self, mock_logs, mock_canc, mock_cohort, mock_part, mock_life, test_client,
    ):
        """The legacy dept_send_auto_invoice_status=2 flag alone does not
        qualify a claim — dept 2 has no qualifying tile, so its run is
        counted under 'did not qualify' in the section-2 split and its
        card still shows in section 1 (the AI did evaluate it)."""
        mock_part.return_value = _participation()
        from tests.conftest import make_projection_doc
        mock_cohort.return_value = [
            make_projection_doc(
                100, sql_row={"dept_id": 1,
                              "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED",
                           "billing_level": "Motor Vehicle Incident Level 1",
                           "line_items_save_to_rh_status": True},
            ),
            make_projection_doc(
                200, sql_row={"dept_id": 2,
                              "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED",
                           "billing_level": "Nonqualifying Level",
                           "dept_send_auto_invoice_status": 2,
                           "line_items_save_to_rh_status": False},
            ),
        ]
        mock_life.return_value = {
            100: {"type_id": 4, "status": "Payment Received", "archived": 0},
            200: {"type_id": 1, "status": "Unassigned", "archived": 0},
        }
        mock_canc.return_value = {}
        mock_logs.return_value = {
            100: [{"log_text": "Invoice to Insurance - Released",
                   "user_id": 7486, "user_type_id": 2}],
        }

        response = test_client.get("/api/ai-analytics/outcomes/funnel", headers=AUTH)
        assert response.status_code == 200
        stages = response.json()
        assert len(stages) == 6

        # The funnel covers the ELIGIBLE cohort only — claim 200 (dept 2,
        # no qualifying tile) is accounted for in the summary KPI, not in
        # the funnel stages.
        # Section 1: only claim 100's fee card.
        assert stages[0]["count"] == 1
        cards = {b["label"]: b["count"] for b in stages[0]["breakdown"]}
        assert cards["Motor Vehicle Incident Level 1"] == 1
        assert "Nonqualifying Level" not in cards

        # Section 2: one eligible run submitted, and it moved on.
        assert stages[1]["count"] == 1
        submitted = {b["label"]: b["count"] for b in stages[1]["breakdown"]}
        assert submitted["Still in new runs — AI working or awaiting grid"] == 0
        assert submitted["Moved on — active runs or beyond"] == 1

        # Section 3: claim 100 left New Runs → Payment Received.
        assert stages[2]["count"] == 1
        left_new = {b["label"]: b["count"] for b in stages[2]["breakdown"]}
        assert left_new["Payment Received"] == 1

        # Section 4: the one finished run went to the grid.
        assert stages[3]["count"] == 1
        hitl = {b["label"]: b["count"] for b in stages[3]["breakdown"]}
        assert hitl["Went to human-in-the-loop grid"] == 1
        assert hitl["Went elsewhere — finished without the review grid"] == 0

        # Section 5: 100 was released, nothing pending on the grid.
        assert stages[4]["count"] == 0

        # Section 6: 100 left the grid as accepted/released.
        assert stages[5]["count"] == 1
        left_grid = {b["label"]: b["count"] for b in stages[5]["breakdown"]}
        assert left_grid["Accepted / Released — Payment Received"] == 1

    @patch("ai_analytics.outcome_service.sql_repo.get_claim_lifecycle_for_claims")
    @patch("ai_analytics.outcome_service.get_ai_participation_map", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_historical_activity_rescues_disabled_department(
        self, mock_logs, mock_canc, mock_cohort, mock_part, mock_life, test_client,
    ):
        """A successful writeback proves the claim was eligible when it ran
        even though the department currently has no qualifying tile."""
        mock_part.return_value = _participation()
        from tests.conftest import make_projection_doc
        mock_cohort.return_value = [
            make_projection_doc(
                200, sql_row={"dept_id": 2,
                              "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "COMPLETED",
                           "line_items_save_to_rh_status": True},
            ),
        ]
        mock_life.return_value = {200: {"type_id": 4, "status": "Payment Received", "archived": 0}}
        mock_canc.return_value = {}
        mock_logs.return_value = {
            200: [{"log_text": "Invoice to Insurance - Released",
                   "user_id": 7486, "user_type_id": 2}],
        }

        response = test_client.get("/api/ai-analytics/outcomes/funnel", headers=AUTH)
        stages = response.json()
        # The run's successful writeback rescued it into the eligible
        # cohort: it moved on (section 2) and was released (section 6).
        submitted = {b["label"]: b["count"] for b in stages[1]["breakdown"]}
        assert submitted["Moved on — active runs or beyond"] == 1
        assert stages[5]["count"] == 1
        left_grid = {b["label"]: b["count"] for b in stages[5]["breakdown"]}
        assert left_grid["Accepted / Released — Payment Received"] == 1

    @patch("ai_analytics.outcome_service.sql_repo.get_claim_lifecycle_for_claims")
    @patch("ai_analytics.outcome_service.get_ai_participation_map", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_funnel_step1_matches_against_fee_catalog(
        self, mock_logs, mock_canc, mock_mongo, mock_cohort, mock_part, test_client,
    ):
        """With a real fee catalog on the participation map, a
        category-only result resolves to the canonical category label and
        a fee-item value matches its tile."""
        participation = _participation()
        participation[1]["fee_catalog_version"] = 1
        participation[1]["fee_catalog"] = [
            {"item": "Vehicle Fire", "fee_category": "Structure Fires",
             "fee_label_from_document": "Vehicle fire response"},
            {"item": "", "fee_category": "Structure Fires",
             "fee_label_from_document": ""},
        ]
        # Dept 2's catalog shares no categories/items with dept 1 — the
        # same source output must not cross-match across departments.
        participation[2] = {
            "uses_ai": True, "ai_mode": "queued", "qualifying_fee_count": 1,
            "has_auto": False, "has_queued": True, "has_limited_auto": False,
            "fee_catalog_version": 1,
            "fee_catalog": [
                {"item": "Rescue Response", "fee_category": "Rescue",
                 "fee_label_from_document": ""},
            ],
        }
        mock_part.return_value = participation
        cohort = [
            {"claim_id": 100, "AI_inv_process_status": 2, "dept_id": 1,
             "ai_business_updated_at": "2026-01-15T10:00:00"},
            {"claim_id": 200, "AI_inv_process_status": 2, "dept_id": 1,
             "ai_business_updated_at": "2026-01-15T10:00:00"},
            {"claim_id": 300, "AI_inv_process_status": 2, "dept_id": 2,
             "ai_business_updated_at": "2026-01-15T10:00:00"},
        ]
        mock_cohort.return_value = cohort
        mock_mongo.return_value = {
            # Category-only result — a valid category selection.
            100: {"claim_processing_status": "COMPLETED",
                  "billing_category": "Structure Fires",
                  "intake_status": "CATEGORY_ONLY"},
            # Item result, documented under its source-schedule label —
            # resolves to the canonical fee item via the alias.
            200: {"claim_processing_status": "COMPLETED",
                  "billing_category": "Structure Fires",
                  "billing_level": "Vehicle fire response",
                  "intake_status": "IDENTIFIED"},
            # Identical dept-1 labels under dept 2's catalog → unmatched.
            300: {"claim_processing_status": "COMPLETED",
                  "billing_category": "Structure Fires",
                  "billing_level": "Vehicle Fire"},
        }
        mock_canc.return_value = {}
        mock_logs.return_value = {}

        response = test_client.get("/api/ai-analytics/outcomes/funnel", headers=AUTH)
        assert response.status_code == 200
        stages = response.json()
        assert stages[2]["count"] == 3
        breakdown = {
            (b["label"], b["match_status"]): b
            for b in stages[2]["breakdown"]
        }
        cat = breakdown[("Structure Fires", "category_matched")]
        assert cat["count"] == 1
        assert "Intake: Category identified" in cat["description"]
        item = breakdown[("Structure Fires / Vehicle Fire", "matched")]
        assert item["count"] == 1
        # Dept 2's record keeps its raw label, explicitly unmatched.
        other = breakdown[("Structure Fires / Vehicle Fire", "unmatched")]
        assert other["count"] == 1
        assert "Category identified (no level)" not in {
            b["label"] for b in stages[2]["breakdown"]
        }
        assert sum(b["count"] for b in stages[2]["breakdown"]) == 3
        counts = [s["count"] for s in stages]
        assert counts == sorted(counts, reverse=True)

        # A department filter is forwarded to the SQL cohort and scopes the
        # breakdown to that department's catalog results.
        mock_cohort.return_value = cohort[:2]
        response = test_client.get(
            "/api/ai-analytics/outcomes/funnel?department_id=1", headers=AUTH
        )
        assert response.status_code == 200
        assert mock_cohort.call_args.kwargs["department_id"] == 1
        stages = response.json()
        assert stages[2]["count"] == 2
        assert {b["match_status"] for b in stages[2]["breakdown"]} == {
            "category_matched", "matched"
        }

    @patch("ai_analytics.outcome_service.get_ai_participation_map", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_ai_invoice_cohort")
    @patch("ai_analytics.outcome_service.mongo_repo.get_ai_line_items_for_claim_ids", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_participation_unavailable_marks_unknown_not_disqualified(
        self, mock_logs, mock_canc, mock_cohort, mock_part, mock_life, test_client,
    ):
        mock_part.return_value = None
        mock_life.return_value = {100: {"type_id": 1, "status": "Unassigned", "archived": 0}}
        from tests.conftest import make_projection_doc
        mock_cohort.return_value = [
            make_projection_doc(
                100, sql_row={"dept_id": 1,
                              "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "INITIATED",
                           "agent_exec_status": "in_progress",
                           "line_items_save_to_rh_status": False},
            ),
        ]
        mock_canc.return_value = {}
        mock_logs.return_value = {}

        response = test_client.get("/api/ai-analytics/outcomes/summary", headers=AUTH)
        data = response.json()
        # Unknown claims stay in the qualified cohort — nothing collapses
        assert data["total_ai_invoices"] == 1
        assert data["did_not_qualify"] == 0
        assert data["source_status"]["recoveryhub_ai_fee_config"] == "unavailable"
        assert data["data_complete"] is False

    @patch("ai_analytics.outcome_service.sql_repo.get_claim_lifecycle_for_claims")
    @patch("ai_analytics.outcome_service.get_ai_participation_map", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.projection_repo.get_projection_cohort", new_callable=AsyncMock)
    @patch("ai_analytics.outcome_service.sql_repo.get_cancellation_details_for_claims")
    @patch("ai_analytics.outcome_service.sql_repo.get_process_logs_for_claims")
    def test_participation_failure_treated_as_unavailable(
        self, mock_logs, mock_canc, mock_cohort, mock_part, mock_life, test_client,
    ):
        """An unexpected exception from the participation lookup degrades to
        the same unknown/unavailable state as a clean None."""
        mock_part.side_effect = RuntimeError("mongo unreachable")
        mock_life.return_value = {100: {"type_id": 1, "status": "Unassigned", "archived": 0}}
        from tests.conftest import make_projection_doc
        mock_cohort.return_value = [
            make_projection_doc(
                100, sql_row={"dept_id": 1,
                              "ai_business_updated_at": "2026-01-15T10:00:00"},
                ai_record={"claim_processing_status": "INITIATED",
                           "agent_exec_status": "in_progress",
                           "line_items_save_to_rh_status": False},
            ),
        ]
        mock_canc.return_value = {}
        mock_logs.return_value = {}

        response = test_client.get("/api/ai-analytics/outcomes/summary", headers=AUTH)
        data = response.json()
        assert data["total_ai_invoices"] == 1
        assert data["did_not_qualify"] == 0
        assert data["source_status"]["recoveryhub_ai_fee_config"] == "unavailable"
        assert data["data_complete"] is False
