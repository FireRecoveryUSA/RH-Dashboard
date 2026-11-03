"""Tests for the Claim Payment Comparison analytics (classifier, service, routes).

SQL and Mongo are mocked. Production Azure SQL is not reachable from dev machines.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_analytics import claim_comparison_service as svc
from ai_analytics import sql_repository as sql_repo
from ai_analytics.claim_status_config import (
    BILLING_TARGET_DIRECT,
    BILLING_TARGET_INSURANCE,
    BILLING_TARGET_UNKNOWN,
    BUCKET_ARCHIVE,
    BUCKET_DENIED,
    BUCKET_IN_PROGRESS,
    BUCKET_NOT_BILLABLE,
    BUCKET_PAID,
    BUCKET_READY_TO_BE_PAID,
    BUCKET_UNDER_REVIEW,
    billing_target_from_statuses,
    billing_target_label,
    classify_claim_status,
    incident_type_label,
    is_mapped_claim_status,
)
from ai_analytics.models import (
    AiAnalyticsFilters,
    AiClaimComparisonResponse,
    ClaimComparisonNotBillable,
    ClaimComparisonNotBillableCohort,
    ClaimStatusBreakdown,
)

AUTH = {"Authorization": "Bearer valid-mock-token"}
ROW_START = "SysStartTime"
SERVICE = "ai_analytics.claim_comparison_service"
SQL = "ai_analytics.sql_repository"
RELEASED = {"business_outcome": "released"}
REJECTED = {"business_outcome": "cancelled_rejected"}
PENDING = {"business_outcome": "pending"}


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

class TestClassifyClaimStatus:
    @pytest.mark.parametrize("status", [
        "Review For Archives", "Archived per FD", "Archived Payment Plan",
    ])
    def test_archive_bucket(self, status):
        assert classify_claim_status(status) == BUCKET_ARCHIVE
        assert is_mapped_claim_status(status)

    def test_review_for_archives_is_not_under_review(self):
        assert classify_claim_status("Review For Archives") != BUCKET_UNDER_REVIEW

    def test_case_insensitive_exact_match(self):
        assert classify_claim_status("  payment received ") == BUCKET_PAID
        assert classify_claim_status("UNDER REVIEW") == BUCKET_UNDER_REVIEW

    def test_paid(self):
        assert classify_claim_status("Payment Received") == BUCKET_PAID

    @pytest.mark.parametrize("status", ["Payment Approved", "Confirm the Receipt of Invoice"])
    def test_ready_to_be_paid(self, status):
        assert classify_claim_status(status) == BUCKET_READY_TO_BE_PAID

    @pytest.mark.parametrize("status", ["Denied", "Denial Letter Review"])
    def test_denied(self, status):
        assert classify_claim_status(status) == BUCKET_DENIED

    def test_under_review(self):
        assert classify_claim_status("Under Review") == BUCKET_UNDER_REVIEW

    @pytest.mark.parametrize("status", [
        "Unbillable", "Non-Billable (Insufficient Information)",
    ])
    def test_not_billable(self, status):
        assert classify_claim_status(status) == BUCKET_NOT_BILLABLE

    @pytest.mark.parametrize("status", [
        "Sent To Collections", "On Hold", "Invoiced Direct", "Invoiced Insurance",
        "Follow Up", "Resident", "Requesting Information", "Unassigned", "Assigned",
        "Payment Plan", "Ready to Invoice Insurance", "Ready to Invoice Direct",
        "False Alarms", "False Alarm Below Threshold", "Invalid Address", "Law Office",
        "Uncollectable by Law Office", "Ready for Collections", "RM Pending Action",
    ])
    def test_known_in_progress_statuses_are_mapped(self, status):
        assert classify_claim_status(status) == BUCKET_IN_PROGRESS
        assert is_mapped_claim_status(status)

    def test_unknown_status_falls_back_to_in_progress_and_is_unmapped(self):
        assert classify_claim_status("Something Unexpected") == BUCKET_IN_PROGRESS
        assert not is_mapped_claim_status("Something Unexpected")
        assert classify_claim_status(None) == BUCKET_IN_PROGRESS
        assert not is_mapped_claim_status(None)


class TestBillingTarget:
    def test_direct_only(self):
        assert billing_target_from_statuses([("Invoiced Direct", 1, True)]) == BILLING_TARGET_DIRECT

    def test_insurance_only(self):
        assert billing_target_from_statuses([("Ready to Invoice Insurance", 1, True)]) == BILLING_TARGET_INSURANCE

    def test_neither_is_unknown(self):
        assert billing_target_from_statuses([("Denied", 1, True)]) == BILLING_TARGET_UNKNOWN
        assert billing_target_from_statuses([]) == BILLING_TARGET_UNKNOWN

    def test_both_uses_latest(self):
        assert billing_target_from_statuses(
            [("Invoiced Insurance", 1, True), ("Invoiced Direct", 2, False)]
        ) == BILLING_TARGET_DIRECT
        assert billing_target_from_statuses(
            [("Invoiced Direct", 1, True), ("Invoiced Insurance", 2, False)]
        ) == BILLING_TARGET_INSURANCE

    def test_tie_prefers_current_row(self):
        # Timestamp tie: the live Claims row's status wins, whichever side it is
        assert billing_target_from_statuses(
            [("Invoiced Direct", 2, True), ("Invoiced Insurance", 2, False)]
        ) == BILLING_TARGET_DIRECT
        assert billing_target_from_statuses(
            [("Invoiced Direct", 2, False), ("Invoiced Insurance", 2, True)]
        ) == BILLING_TARGET_INSURANCE
        # Neither current -> deterministic insurance fallback
        assert billing_target_from_statuses(
            [("Invoiced Direct", 2, False), ("Invoiced Insurance", 2, False)]
        ) == BILLING_TARGET_INSURANCE

    def test_labels(self):
        assert billing_target_label(BILLING_TARGET_DIRECT) == "Direct"
        assert billing_target_label(BILLING_TARGET_INSURANCE) == "Insurance"
        assert billing_target_label(BILLING_TARGET_UNKNOWN) == "Unknown"


class TestIncidentTypeLabel:
    def test_named_type(self):
        assert incident_type_label(1, "Motor Vehicle Incident") == "Motor Vehicle Incident"

    def test_type_without_run_types_row(self):
        assert incident_type_label(10, None) == "Type 10"

    def test_null_type(self):
        assert incident_type_label(None, None) == "Unspecified"


# ---------------------------------------------------------------------------
# Service helpers
# ---------------------------------------------------------------------------

def _scope_row(claim_id, dept=1, type_id=None, type_name=None):
    return {
        "id": claim_id,
        "dept_id": dept,
        "run_number": f"R{claim_id}",
        "date_of_submitted": "2026-02-10T00:00:00",
        "department_name": f"Dept {dept}",
        "type_id": type_id,
        "type_name": type_name,
    }


def _versions(claim_id, *versions):
    """versions: (status, last_start_iso)."""
    return [
        {"id": claim_id, "status": status, "first_start": ts, "last_start": ts}
        for status, ts in versions
    ]


def _filters():
    return AiAnalyticsFilters(start_date="2026-01-01", end_date="2026-06-30")


def _ai_result(records=None, complete=True):
    return (
        records or [],
        {"recoveryhub_sql": "available", "recoveryhub_ai_mongo": "available"},
        complete,
    )


def _run(scope_rows, versions_rows, records=None, deleted=0, ai_complete=True):
    """Run the service with SQL and AI cohort mocked."""
    def fake_versions(chunk, column, as_of=None):
        assert column == ROW_START
        return [r for r in versions_rows if r["id"] in chunk]

    with patch(f"{SQL}.get_claims_row_start_column", return_value=ROW_START), \
         patch(f"{SQL}.get_claim_comparison_scope", return_value=scope_rows), \
         patch(f"{SQL}.get_claim_status_versions", side_effect=fake_versions), \
         patch(f"{SQL}.get_deleted_runs_count", return_value=deleted), \
         patch(f"{SERVICE}._load_normalized_cohort", new_callable=AsyncMock,
               return_value=_ai_result(records, ai_complete)):
        return asyncio.run(svc.get_claim_payment_comparison(None, _filters()))


def _record(claim_id, base, **extra):
    return {"claim_id": claim_id, **base, **extra}


# ---------------------------------------------------------------------------
# Service behaviour
# ---------------------------------------------------------------------------

class TestEffectiveStatus:
    def test_archive_current_falls_back_to_previous_non_archive(self):
        rows = _versions(
            1,
            ("Payment Received", "2026-03-01T10:00:00"),
            ("Archived per FD", "2026-05-01T10:00:00"),
        )
        resp = _run([_scope_row(1)], rows)
        assert resp.overall.paid == 1
        assert resp.overall.total == 1
        assert resp.other_manual.paid == 1
        week = next(w for w in resp.weekly if w.week == "2026-W09")
        assert week.other_manual.paid == 1

    def test_latest_non_archive_version_wins(self):
        rows = _versions(
            1,
            ("Denied", "2026-02-01T10:00:00"),
            ("Payment Approved", "2026-02-20T10:00:00"),
        )
        resp = _run([_scope_row(1)], rows)
        assert resp.overall.ready_to_be_paid == 1
        assert resp.overall.denied == 0

    def test_only_archive_versions_is_unresolved(self):
        rows = _versions(1, ("Archived per FD", "2026-02-01T10:00:00"))
        resp = _run([_scope_row(1)], rows)
        assert resp.overall.total == 0
        assert resp.unresolved_claims == 1

    def test_versions_after_end_date_are_ignored(self):
        rows = _versions(
            1,
            ("Under Review", "2026-02-01T10:00:00"),
            ("Payment Received", "2026-07-15T10:00:00"),
        )
        resp = _run([_scope_row(1)], rows)
        assert resp.overall.under_review == 1
        assert resp.overall.paid == 0
        assert all(w.other_manual.paid == 0 for w in resp.weekly)

    def test_all_versions_after_end_date_is_unresolved(self):
        rows = _versions(1, ("Payment Received", "2026-07-15T10:00:00"))
        resp = _run([_scope_row(1)], rows)
        assert resp.overall.total == 0
        assert resp.unresolved_claims == 1

    def test_future_billing_statuses_do_not_set_target(self):
        rows = _versions(
            1,
            ("Under Review", "2026-02-01T10:00:00"),
            ("Invoiced Insurance", "2026-07-15T10:00:00"),
        )
        resp = _run([_scope_row(1)], rows)
        labels = {s.label: s for s in resp.by_billing_target}
        # Under-review is not a finished claim, so it never makes up a total.
        assert labels["Unknown"].overall.under_review == 1
        assert labels["Unknown"].overall.total == 0
        assert "Insurance" not in labels

    def test_not_billable_after_end_date_is_ignored(self):
        rows = _versions(
            1,
            ("Payment Received", "2026-02-01T10:00:00"),
            ("Unbillable", "2026-07-15T10:00:00"),
        )
        resp = _run([_scope_row(1)], rows)
        assert resp.not_billable.marked_by_processor == 0
        assert resp.not_billable.other.ever_not_billable == 0

    def test_unmapped_status_after_end_date_is_not_reported(self):
        rows = _versions(
            1,
            ("Payment Received", "2026-02-01T10:00:00"),
            ("Mystery Stage", "2026-07-15T10:00:00"),
        )
        resp = _run([_scope_row(1)], rows)
        assert resp.unmapped_statuses == {}


class TestEffectiveStatusTies:
    @pytest.mark.parametrize("reverse", [False, True])
    def test_tie_at_latest_row_start_prefers_live_claims_row(self, reverse):
        ts = "2026-02-10T10:00:00"
        rows = [
            {"id": 1, "status": "Under Review", "first_start": ts, "last_start": ts, "is_current": 0},
            {"id": 1, "status": "Payment Received", "first_start": ts, "last_start": ts, "is_current": 1},
        ]
        if reverse:
            rows.reverse()
        resp = _run([_scope_row(1)], rows)
        assert resp.overall.paid == 1
        assert resp.overall.under_review == 0


class TestCohorts:
    def test_released_rejected_and_manual_split(self):
        scope = [_scope_row(1), _scope_row(2), _scope_row(3)]
        rows = (
            _versions(1, ("Payment Received", "2026-02-10T10:00:00"))
            + _versions(2, ("Denied", "2026-02-11T10:00:00"))
            + _versions(3, ("Confirm the Receipt of Invoice", "2026-02-12T10:00:00"))
        )
        records = [_record(1, RELEASED), _record(2, REJECTED)]
        resp = _run(scope, rows, records=records)
        assert resp.ai_released.paid == 1
        assert resp.ai_released.total == 1
        assert resp.other.total == 2
        assert resp.other_ai_rejected.denied == 1
        assert resp.other_manual.ready_to_be_paid == 1

    def test_ai_pending_counted_but_excluded_from_rates(self):
        scope = [_scope_row(1), _scope_row(2)]
        rows = (
            _versions(1, ("Payment Received", "2026-02-10T10:00:00"))
            + _versions(2, ("Payment Received", "2026-02-10T10:00:00"))
        )
        resp = _run(scope, rows, records=[_record(2, PENDING)])
        assert resp.ai_pending == 1
        assert resp.overall.total == 1
        assert resp.overall.paid_rate == 100.0

    def test_run_outside_ai_cohort_is_manual(self):
        resp = _run([_scope_row(9)], _versions(9, ("Under Review", "2026-02-10T10:00:00")))
        assert resp.other_manual.under_review == 1
        assert resp.ai_released.total == 0


class TestNotBillable:
    def test_ever_not_billable_still_counts_effective_status(self):
        rows = _versions(
            1,
            ("Unbillable", "2026-02-01T10:00:00"),
            ("Payment Received", "2026-02-20T10:00:00"),
        )
        resp = _run([_scope_row(1)], rows)
        assert resp.overall.paid == 1
        assert resp.not_billable.marked_by_processor == 1
        assert resp.not_billable.other.ever_not_billable == 1
        assert resp.not_billable.other.effective_status == {BUCKET_PAID: 1}
        assert resp.not_billable.other.ready_vs_denied_rate == 100.0

    def test_identified_by_system_is_separate_from_processor_marking(self):
        rows = _versions(1, ("Payment Approved", "2026-02-10T10:00:00"))
        records = [_record(1, RELEASED, is_billable=False)]
        resp = _run([_scope_row(1)], rows, records=records)
        assert resp.not_billable.identified_by_system == 1
        assert resp.not_billable.marked_by_processor == 0

    def test_ready_vs_denied_rate_across_cohorts(self):
        scope = [_scope_row(1), _scope_row(2), _scope_row(3)]
        rows = (
            _versions(1, ("Unbillable", "2026-02-01T10:00:00"), ("Denied", "2026-02-05T10:00:00"))
            + _versions(2, ("Unbillable", "2026-02-01T10:00:00"), ("Payment Approved", "2026-02-05T10:00:00"))
            + _versions(3, ("Unbillable", "2026-02-01T10:00:00"), ("Unbillable", "2026-02-05T10:00:00"))
        )
        resp = _run(scope, rows)
        other = resp.not_billable.other
        assert other.ever_not_billable == 3
        assert other.ready_or_paid == 1
        assert other.denied == 1
        assert other.ready_vs_denied_rate == 50.0


class TestWeeklyAndRates:
    def test_weekly_uses_effective_date_and_covers_range(self):
        scope = [_scope_row(1), _scope_row(2)]
        rows = (
            _versions(1, ("Payment Received", "2026-02-10T10:00:00"))
            + _versions(2, ("Denied", "2026-02-10T12:00:00"))
        )
        resp = _run(scope, rows)
        assert resp.weekly[0].week == "2026-W01"
        assert resp.weekly[0].week_start == "2025-12-29"
        week = next(w for w in resp.weekly if w.week == "2026-W07")
        assert week.other_manual.paid == 1
        assert week.other_manual.denied == 1
        assert week.other_manual.resolved_success_rate == 50.0
        assert week.ai_released.resolved_success_rate is None

    def test_deleted_runs_reported_but_not_in_denominators(self):
        scope = [_scope_row(1), _scope_row(2)]
        rows = (
            _versions(1, ("Payment Received", "2026-02-10T10:00:00"))
            + _versions(2, ("Under Review", "2026-02-10T10:00:00"))
        )
        resp = _run(scope, rows, deleted=3)
        assert resp.deleted_runs == 3
        # Total is finished claims only (paid + ready + denied); the
        # under-review claim shows in its column but not the total or rates.
        assert resp.overall.total == 1
        assert resp.overall.under_review == 1
        assert resp.overall.paid_rate == 100.0

    def test_rate_denominator_is_finished_claims_only(self):
        scope = [_scope_row(i) for i in range(1, 5)]
        rows = (
            _versions(1, ("Payment Received", "2026-02-10T10:00:00"))
            + _versions(2, ("Denied", "2026-02-10T10:00:00"))
            + _versions(3, ("Under Review", "2026-02-10T10:00:00"))
            + _versions(4, ("Unbillable", "2026-02-10T10:00:00"))
        )
        resp = _run(scope, rows)
        # Only finished claims make up the total: paid + denied here.
        # Under-review and not-billable show in their columns, never in total.
        assert resp.overall.total == 2
        assert resp.overall.under_review == 1
        assert resp.overall.not_billable == 1
        assert resp.overall.paid_rate == 50.0  # (paid+ready)/(paid+ready+denied)
        assert resp.overall.denied_rate == 50.0

    def test_zero_denominators_return_none(self):
        resp = _run([_scope_row(1)], _versions(1, ("Unbillable", "2026-02-10T10:00:00")))
        assert resp.overall.paid_rate is None
        assert resp.overall.resolved_success_rate is None
        assert resp.overall.denied_rate is None

    def test_empty_cohort_returns_zero_shape(self):
        resp = _run([], [])
        assert resp.overall.total == 0
        assert resp.overall.paid_rate is None
        assert resp.weekly
        assert resp.data_complete is True


class TestSegments:
    def test_incident_type_from_run_type_for_ai_and_manual_alike(self):
        scope = [
            _scope_row(1, type_id=1, type_name="Motor Vehicle Incident"),
            _scope_row(2, type_id=1, type_name="Motor Vehicle Incident"),
            _scope_row(3, type_id=None, type_name=None),
            _scope_row(4, type_id=10, type_name=None),
        ]
        rows = (
            _versions(1, ("Payment Received", "2026-02-10T10:00:00"))
            + _versions(2, ("Payment Received", "2026-02-10T10:00:00"))
            + _versions(3, ("Denied", "2026-02-10T10:00:00"))
            + _versions(4, ("Denied", "2026-02-10T10:00:00"))
        )
        records = [_record(1, RELEASED, billing_category="Structure Fires")]
        resp = _run(scope, rows, records=records)
        labels = {s.label: s for s in resp.by_incident_type}
        assert labels["Motor Vehicle Incident"].ai_released.paid == 1
        assert labels["Motor Vehicle Incident"].other.paid == 1
        assert labels["Unspecified"].other.denied == 1
        assert labels["Type 10"].other.denied == 1
        assert "Structure Fires" not in labels

    def test_billing_target_from_status_versions(self):
        scope = [_scope_row(1), _scope_row(2), _scope_row(3)]
        rows = (
            _versions(1, ("Invoiced Direct", "2026-02-01T10:00:00"), ("Payment Received", "2026-02-10T10:00:00"))
            + _versions(2, ("Invoiced Insurance", "2026-02-01T10:00:00"), ("Denied", "2026-02-10T10:00:00"))
            + _versions(3, ("Payment Received", "2026-02-10T10:00:00"))
        )
        resp = _run(scope, rows)
        labels = {s.label: s for s in resp.by_billing_target}
        assert labels["Direct"].overall.paid == 1
        assert labels["Insurance"].overall.denied == 1
        assert labels["Unknown"].overall.paid == 1
        assert all(not hasattr(s, "group") for s in resp.by_billing_target)

    def test_unmapped_statuses_are_reported(self):
        rows = _versions(1, ("Mystery Stage", "2026-02-10T10:00:00"))
        resp = _run([_scope_row(1)], rows)
        assert resp.unmapped_statuses == {"Mystery Stage": 1}
        assert resp.overall.in_progress == 1

    def test_known_statuses_are_not_reported_unmapped(self):
        rows = _versions(1, ("Sent To Collections", "2026-02-10T10:00:00"))
        resp = _run([_scope_row(1)], rows)
        assert resp.unmapped_statuses == {}


class TestChunkingAndSql:
    def test_history_queries_are_chunked_sequentially(self, monkeypatch):
        monkeypatch.setattr(svc, "HISTORY_CHUNK_SIZE", 2)
        scope = [_scope_row(i) for i in range(1, 6)]
        rows = [r for i in range(1, 6) for r in _versions(i, ("Payment Received", "2026-02-10T10:00:00"))]
        with patch(f"{SQL}.get_claims_row_start_column", return_value=ROW_START), \
             patch(f"{SQL}.get_claim_comparison_scope", return_value=scope), \
             patch(f"{SQL}.get_claim_status_versions", side_effect=lambda chunk, col, as_of=None: [
                 r for r in rows if r["id"] in chunk]) as versions_mock, \
             patch(f"{SQL}.get_deleted_runs_count", return_value=0), \
             patch(f"{SERVICE}._load_normalized_cohort", new_callable=AsyncMock,
                   return_value=_ai_result()):
            resp = asyncio.run(svc.get_claim_payment_comparison(None, _filters()))
        sizes = [len(c.args[0]) for c in versions_mock.call_args_list]
        assert sizes == [2, 2, 1]
        assert resp.overall.paid == 5

    def test_status_history_query_filters_by_id_and_never_uses_system_time(self):
        with patch(f"{SQL}._execute_parameterized", return_value=[]) as execute:
            sql_repo.get_claim_status_versions([1, 2], ROW_START)
        query, params = execute.call_args.args
        assert "AuditData.claimsHistory" in query
        assert "FOR SYSTEM_TIME" not in query.upper()
        assert "h.id IN" in query
        assert "c.id IN" in query
        assert f"[{ROW_START}]" in query
        assert params == {"claim_ids_csv": "1,2", "as_of": None}

    def test_scope_query_excludes_rural_metro_and_joins_run_types(self):
        with patch(f"{SQL}._execute_parameterized", return_value=[]) as execute:
            sql_repo.get_claim_comparison_scope("2026-01-01", "2026-06-30", None)
        query, _ = execute.call_args.args
        assert "NOT IN (1136,2198,2627,2628,2629)" in query
        assert "c.submitted = 1" in query
        assert "c.original_run_id IS NOT NULL" in query
        assert "LEFT JOIN dbo.run_types rt ON rt.id = c.type_id" in query
        assert "incidents_billing" not in query
        assert "FOR SYSTEM_TIME" not in query.upper()

    def test_sql_scope_failure_returns_unavailable_empty_result(self):
        with patch(f"{SQL}.get_claims_row_start_column", return_value=ROW_START), \
             patch(f"{SQL}.get_claim_comparison_scope", side_effect=RuntimeError("timeout")):
            resp = asyncio.run(svc.get_claim_payment_comparison(None, _filters()))
        assert resp.source_status["recoveryhub_sql"] == "unavailable"
        assert resp.data_complete is False
        assert resp.overall.total == 0

    def test_ai_side_incomplete_marks_data_incomplete(self):
        resp = _run([_scope_row(1)], [], ai_complete=False)
        assert resp.data_complete is False

    def test_ai_mongo_unavailable_returns_incomplete_empty_result(self):
        with patch(f"{SQL}.get_claims_row_start_column", return_value=ROW_START), \
             patch(f"{SQL}.get_claim_comparison_scope",
                   return_value=[_scope_row(1)]), \
             patch(f"{SERVICE}._load_normalized_cohort", new_callable=AsyncMock,
                   return_value=(
                       [],
                       {"recoveryhub_sql": "available",
                        "recoveryhub_ai_mongo": "unavailable"},
                       False,
                   )):
            resp = asyncio.run(svc.get_claim_payment_comparison(None, _filters()))
        assert resp.data_complete is False
        assert resp.source_status["recoveryhub_ai_mongo"] == "unavailable"
        # No cohort data must not misclassify every run as manual.
        assert resp.other_manual.total == 0
        assert resp.overall.total == 0

    def test_deleted_runs_filters_date_of_submitted_when_present(self):
        with patch(f"{SQL}.get_table_column_names",
                   return_value=["id", "date_of_submitted", "created"]), \
             patch(f"{SQL}._execute_parameterized",
                   return_value=[{"deleted_runs": 4}]) as execute:
            assert sql_repo.get_deleted_runs_count("2026-01-01", "2026-06-30", None) == 4
        assert "x.date_of_submitted" in execute.call_args[0][0]

    def test_deleted_runs_falls_back_to_created_column(self):
        with patch(f"{SQL}.get_table_column_names",
                   return_value=["id", "created", "dept_id"]), \
             patch(f"{SQL}._execute_parameterized",
                   return_value=[{"deleted_runs": 4}]) as execute:
            assert sql_repo.get_deleted_runs_count("2026-01-01", "2026-06-30", None) == 4
        assert "x.created" in execute.call_args[0][0]

    def test_deleted_runs_query_error_propagates(self):
        with patch(f"{SQL}.get_table_column_names",
                   return_value=["id", "date_of_submitted"]), \
             patch(f"{SQL}._execute_parameterized",
                   side_effect=RuntimeError("timeout")):
            with pytest.raises(RuntimeError):
                sql_repo.get_deleted_runs_count("2026-01-01", "2026-06-30", None)

    def test_deleted_query_failure_marks_partial(self):
        with patch(f"{SQL}.get_claims_row_start_column", return_value=ROW_START), \
             patch(f"{SQL}.get_claim_comparison_scope", return_value=[]), \
             patch(f"{SQL}.get_claim_status_versions", return_value=[]), \
             patch(f"{SQL}.get_deleted_runs_count", side_effect=RuntimeError("no dept_id")), \
             patch(f"{SERVICE}._load_normalized_cohort", new_callable=AsyncMock,
                   return_value=_ai_result()):
            resp = asyncio.run(svc.get_claim_payment_comparison(None, _filters()))
        assert resp.data_complete is False
        assert resp.source_status["recoveryhub_sql"] == "partial"


# ---------------------------------------------------------------------------
# Cache warmer
# ---------------------------------------------------------------------------

class TestCacheWarmer:
    def test_warm_interval_defaults_to_1800(self, monkeypatch):
        monkeypatch.delenv("CLAIM_COMPARISON_WARM_INTERVAL_SECONDS", raising=False)
        assert svc._warm_interval_seconds() == 1800

    def test_warm_interval_invalid_value_falls_back(self, monkeypatch):
        monkeypatch.setenv("CLAIM_COMPARISON_WARM_INTERVAL_SECONDS", "bogus")
        assert svc._warm_interval_seconds() == 1800

    def test_chunk_delay_defaults_to_100ms(self, monkeypatch):
        monkeypatch.delenv("CLAIM_COMPARISON_CHUNK_DELAY_MS", raising=False)
        assert svc._chunk_delay_seconds() == 0.1

    def test_chunk_delay_invalid_value_falls_back(self, monkeypatch):
        monkeypatch.setenv("CLAIM_COMPARISON_CHUNK_DELAY_MS", "bogus")
        assert svc._chunk_delay_seconds() == 0.1

    def test_status_version_chunks_are_paced(self, monkeypatch):
        monkeypatch.setenv("CLAIM_COMPARISON_CHUNK_DELAY_MS", "50")
        with patch(f"{SERVICE}._chunks",
                   side_effect=lambda items, size: [items[:1], items[1:]]), \
             patch(f"{SQL}.get_claim_status_versions", return_value=[]), \
             patch(f"{SERVICE}.time.sleep") as sleep:
            svc._fetch_status_versions([1, 2], ROW_START)
        # Two chunks -> one inter-chunk yield of 50ms
        sleep.assert_called_once_with(0.05)

    def test_warm_interval_zero_disables_warmer(self, monkeypatch):
        monkeypatch.setenv("CLAIM_COMPARISON_WARM_INTERVAL_SECONDS", "0")
        assert asyncio.run(svc.warm_claim_comparison_cache(None)) is None

    def test_warm_once_recomputes_and_primes_cache(self):
        fake_db = object()
        result = SimpleNamespace(data_complete=True)
        with patch(f"{SERVICE}.try_acquire_lease",
                   new_callable=AsyncMock, return_value=True), \
             patch(f"{SERVICE}.release_lease", new_callable=AsyncMock), \
             patch(f"{SERVICE}.resolve_comparison_window",
                   return_value=("2026-01-01", "2026-06-30")), \
             patch(f"{SERVICE}._compute_claim_payment_comparison",
                   new_callable=AsyncMock, return_value=result) as compute, \
             patch(f"{SERVICE}.prime_cache") as prime:
            asyncio.run(svc._warm_once(fake_db))
        compute.assert_awaited_once()
        prime.assert_called_once()
        func_name, args, kwargs, value, ttl = prime.call_args[0]
        assert func_name == "get_claim_payment_comparison"
        assert args[0] is fake_db
        assert kwargs == {}
        assert value is result
        assert ttl == svc.CACHE_TTL_SECONDS

    def test_warm_once_keeps_previous_cache_on_incomplete_result(self):
        fake_db = object()
        with patch(f"{SERVICE}.try_acquire_lease",
                   new_callable=AsyncMock, return_value=True), \
             patch(f"{SERVICE}.release_lease", new_callable=AsyncMock), \
             patch(f"{SERVICE}.resolve_comparison_window",
                   return_value=("2026-01-01", "2026-06-30")), \
             patch(f"{SERVICE}._compute_claim_payment_comparison",
                   new_callable=AsyncMock,
                   return_value=SimpleNamespace(data_complete=False)), \
             patch(f"{SERVICE}.touch_cache", return_value=True) as touch, \
             patch(f"{SERVICE}.prime_cache") as prime:
            asyncio.run(svc._warm_once(fake_db))
        prime.assert_not_called()
        touch.assert_called_once()

    def test_warm_once_skips_when_another_worker_holds_lease(self):
        fake_db = object()
        with patch(f"{SERVICE}.try_acquire_lease",
                   new_callable=AsyncMock, return_value=False) as acquire, \
             patch(f"{SERVICE}._prime_from_shared_result",
                   new_callable=AsyncMock) as prime_shared, \
             patch(f"{SERVICE}._compute_claim_payment_comparison",
                   new_callable=AsyncMock) as compute:
            asyncio.run(svc._warm_once(fake_db))
        acquire.assert_awaited_once()
        compute.assert_not_awaited()
        prime_shared.assert_awaited_once_with(fake_db)

    def test_prime_from_shared_result_uses_published_value(self):
        from datetime import UTC, datetime
        fake_db = MagicMock()
        collection = fake_db.__getitem__.return_value
        value = AiClaimComparisonResponse(
            period_start="2026-01-01",
            period_end="2026-06-30",
            overall=ClaimStatusBreakdown(),
            ai_released=ClaimStatusBreakdown(),
            other=ClaimStatusBreakdown(),
            other_manual=ClaimStatusBreakdown(),
            other_ai_rejected=ClaimStatusBreakdown(),
            not_billable=ClaimComparisonNotBillable(
                ai_released=ClaimComparisonNotBillableCohort(),
                other=ClaimComparisonNotBillableCohort(),
            ),
            data_complete=True,
        )
        collection.find_one = AsyncMock(return_value={
            "_id": svc.WARM_RESULT_DOC_ID,
            "computed_at": datetime.now(UTC),
            "value": value.model_dump(),
        })
        with patch(f"{SERVICE}.resolve_comparison_window",
                   return_value=("2026-01-01", "2026-06-30")), \
             patch(f"{SERVICE}.prime_cache") as prime:
            asyncio.run(svc._prime_from_shared_result(fake_db))
        prime.assert_called_once()
        primed = prime.call_args[0][3]
        assert isinstance(primed, AiClaimComparisonResponse)
        assert primed.data_complete is True

    def test_prime_from_shared_result_rejects_stale_doc(self):
        from datetime import UTC, datetime, timedelta
        fake_db = MagicMock()
        collection = fake_db.__getitem__.return_value
        collection.find_one = AsyncMock(return_value={
            "_id": svc.WARM_RESULT_DOC_ID,
            "computed_at": datetime.now(UTC)
            - timedelta(seconds=svc.CACHE_TTL_SECONDS + 1),
            "value": {"data_complete": True},
        })
        with patch(f"{SERVICE}.prime_cache") as prime:
            asyncio.run(svc._prime_from_shared_result(fake_db))
        prime.assert_not_called()

    def test_warm_once_does_not_release_lease_on_cancel(self):
        fake_db = object()
        async def cancelled_mid_scan(_db):
            svc._WARM_SCAN_RUNNING = True  # to_thread read still running
            raise asyncio.CancelledError
        with patch(f"{SERVICE}.try_acquire_lease",
                   new_callable=AsyncMock, return_value=True), \
             patch(f"{SERVICE}.release_lease",
                   new_callable=AsyncMock) as release, \
             patch(f"{SERVICE}._warm_once_uncached",
                   new_callable=AsyncMock,
                   side_effect=cancelled_mid_scan):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(svc._warm_once(fake_db))
        release.assert_not_awaited()
        svc._WARM_SCAN_RUNNING = False

    def test_warm_once_releases_lease_when_cancelled_off_scan(self):
        fake_db = object()
        with patch(f"{SERVICE}.try_acquire_lease",
                   new_callable=AsyncMock, return_value=True), \
             patch(f"{SERVICE}.release_lease",
                   new_callable=AsyncMock) as release, \
             patch(f"{SERVICE}._warm_once_uncached",
                   new_callable=AsyncMock,
                   side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(svc._warm_once(fake_db))
        release.assert_awaited_once()

    def test_prime_from_shared_result_rejects_window_mismatch(self):
        from datetime import UTC, datetime
        fake_db = MagicMock()
        collection = fake_db.__getitem__.return_value
        collection.find_one = AsyncMock(return_value={
            "_id": svc.WARM_RESULT_DOC_ID,
            "computed_at": datetime.now(UTC),
            "value": {"period_start": "2026-01-01",
                      "period_end": "2026-06-30"},
        })
        with patch(f"{SERVICE}.resolve_comparison_window",
                   return_value=("2026-01-01", "2026-07-01")), \
             patch(f"{SERVICE}.prime_cache") as prime:
            asyncio.run(svc._prime_from_shared_result(fake_db))
        prime.assert_not_called()

    def test_warm_once_releases_lease_on_failure(self):
        fake_db = object()
        with patch(f"{SERVICE}.try_acquire_lease",
                   new_callable=AsyncMock, return_value=True), \
             patch(f"{SERVICE}.release_lease",
                   new_callable=AsyncMock) as release, \
             patch(f"{SERVICE}.resolve_comparison_window",
                   return_value=("2026-01-01", "2026-06-30")), \
             patch(f"{SERVICE}._compute_claim_payment_comparison",
                   new_callable=AsyncMock,
                   side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                asyncio.run(svc._warm_once(fake_db))
        release.assert_awaited_once()

    def test_cache_serves_stale_over_incomplete_recompute(self):
        from ai_analytics import cache as cache_mod
        calls = []

        @cache_mod.cached(ttl=60, func_name="probe")
        async def probe():
            calls.append(1)
            return SimpleNamespace(data_complete=False, n=len(calls))

        with patch.object(cache_mod, "_is_testing", return_value=False):
            cache_mod.clear_cache()
            cache_mod.prime_cache("probe", (), {}, "GOOD", ttl=-1)  # already expired
            # Expired entry + incomplete recompute -> stale value served once,
            # then from the bounded retry window without recomputing.
            assert asyncio.run(probe()) == "GOOD"
            assert asyncio.run(probe()) == "GOOD"
            assert len(calls) == 1
            cache_mod.clear_cache()

    def test_cache_returns_incomplete_after_max_stale_age(self):
        import time as time_mod
        from ai_analytics import cache as cache_mod
        calls = []

        @cache_mod.cached(ttl=60, func_name="probe3")
        async def probe3():
            calls.append(1)
            return SimpleNamespace(data_complete=False, n=len(calls))

        with patch.object(cache_mod, "_is_testing", return_value=False):
            cache_mod.clear_cache()
            # Good value computed long ago, already past the max stale age
            cache_mod.prime_cache(
                "probe3", (), {}, "OLD", ttl=-1,
                computed_at=time_mod.time() - cache_mod._MAX_STALE_AGE - 1,
            )
            result = asyncio.run(probe3())
            # Fresh incomplete result surfaces the outage, not the stale value
            assert result.data_complete is False
            assert result.n == 1
            cache_mod.clear_cache()

    def test_cache_does_not_store_incomplete_result(self):
        from ai_analytics import cache as cache_mod
        calls = []

        @cache_mod.cached(ttl=60, func_name="probe2")
        async def probe2():
            calls.append(1)
            return SimpleNamespace(data_complete=False, n=len(calls))

        with patch.object(cache_mod, "_is_testing", return_value=False):
            cache_mod.clear_cache()
            first = asyncio.run(probe2())
            second = asyncio.run(probe2())
            # Nothing cached -> every call retries the source
            assert len(calls) == 2
            assert first.n == 1 and second.n == 2
            cache_mod.clear_cache()

    def test_cached_retry_renewal_capped_at_stale_deadline(self):
        import time as time_mod
        from ai_analytics import cache as cache_mod
        calls = []

        @cache_mod.cached(ttl=60, func_name="probe4")
        async def probe4():
            calls.append(1)
            return SimpleNamespace(data_complete=False, n=len(calls))

        now = time_mod.time()
        with patch.object(cache_mod, "_is_testing", return_value=False):
            cache_mod.clear_cache()
            # Good value 10s short of the max stale age, already expired
            cache_mod.prime_cache(
                "probe4", (), {}, "GOOD", ttl=-1,
                computed_at=now - cache_mod._MAX_STALE_AGE + 10,
            )
            with patch.object(cache_mod.time, "time", return_value=now):
                assert asyncio.run(probe4()) == "GOOD"
                expires_at, _, _ = cache_mod._cache[list(cache_mod._cache)[0]]
                # Retry renewal capped at the deadline, not the full 120s window
                assert expires_at <= now + 10 + 1
            # Past the deadline the request surfaces the incomplete result
            with patch.object(cache_mod.time, "time", return_value=now + 11):
                result = asyncio.run(probe4())
                assert result.data_complete is False
                assert result.n == 2
            cache_mod.clear_cache()

    def test_touch_cache_extends_existing_entry(self):
        from ai_analytics import cache as cache_mod
        with patch.object(cache_mod, "_is_testing", return_value=False):
            cache_mod.clear_cache()
            cache_mod.prime_cache("probe", (1,), {}, "GOOD", ttl=-1)
            assert cache_mod.touch_cache("probe", (1,), {}, ttl=3600) is True
            expires_at, value, _ = cache_mod._cache[list(cache_mod._cache)[0]]
            assert value == "GOOD"
            assert expires_at > 0
            assert cache_mod.touch_cache("missing", (1,), {}, ttl=60) is False
            # An incomplete entry must not be renewed — let it expire so
            # requests retry the source.
            cache_mod.prime_cache("bad", (), {}, SimpleNamespace(data_complete=False), ttl=60)
            assert cache_mod.touch_cache("bad", (), {}, ttl=3600) is False
            # A complete value older than the max stale age is not renewed
            import time as time_mod
            cache_mod.prime_cache(
                "old", (), {}, "OLD", ttl=-1,
                computed_at=time_mod.time() - cache_mod._MAX_STALE_AGE - 1,
            )
            assert cache_mod.touch_cache("old", (), {}, ttl=3600) is False
            # Near the deadline, a renewal is capped at it — stale totals can
            # never outlive the max age via touches or retry renewals.
            deadline = time_mod.time() - cache_mod._MAX_STALE_AGE + 30
            cache_mod.prime_cache("edge", (), {}, "OK", ttl=-1, computed_at=deadline)
            assert cache_mod.touch_cache("edge", (), {}, ttl=3600) is True
            expires_at, _, _ = cache_mod._cache[list(cache_mod._cache)[-1]]
            assert expires_at <= deadline + cache_mod._MAX_STALE_AGE + 1
            cache_mod.clear_cache()

    def test_status_versions_bound_rows_before_grouping(self):
        from datetime import datetime as dt
        as_of = dt(2026, 7, 1, 7, 0, 0)
        with patch(f"{SQL}._execute_parameterized", return_value=[]) as execute:
            sql_repo.get_claim_status_versions([1, 2], ROW_START, as_of)
        query, params = execute.call_args[0]
        # The bound applies to each current/history row before GROUP BY, so a
        # status that recurs after the window still reports its in-window max.
        assert query.count("%(as_of)s") == 4
        assert params["as_of"] is as_of

    def test_status_versions_unbounded_by_default(self):
        with patch(f"{SQL}._execute_parameterized", return_value=[]) as execute:
            sql_repo.get_claim_status_versions([1], ROW_START)
        assert execute.call_args[0][1]["as_of"] is None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class TestClaimComparisonRoute:
    def _patches(self, scope_rows=None):
        return [
            patch(f"{SQL}.get_server_date", return_value="2026-08-13"),
            patch(f"{SQL}.get_claims_row_start_column", return_value=ROW_START),
            patch(f"{SQL}.get_claim_comparison_scope", return_value=scope_rows or []),
            patch(f"{SQL}.get_claim_status_versions", return_value=[]),
            patch(f"{SQL}.get_deleted_runs_count", return_value=0),
            patch(f"{SERVICE}._load_normalized_cohort", new_callable=AsyncMock,
                  return_value=_ai_result()),
        ]

    def test_claim_comparison_200_shape_year_to_date(self, test_client):
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            response = test_client.get("/api/ai-analytics/claim-comparison", headers=AUTH)
        finally:
            for p in patches:
                p.stop()
        assert response.status_code == 200
        data = response.json()
        assert data["period_start"] == "2026-07-01"
        assert data["period_end"] == "2026-08-13"
        for key in ("overall", "ai_released", "other", "other_manual", "other_ai_rejected",
                    "weekly", "by_incident_type", "by_billing_target", "not_billable",
                    "unmapped_statuses", "resolved_columns", "source_status", "data_complete"):
            assert key in data
        assert data["not_billable"]["marked_by_processor"] == 0
        assert data["weekly"][0]["week"] == "2026-W27"

    @pytest.mark.parametrize("server_date,expected_start", [
        ("2026-08-13", "2026-07-01"),  # after Jul 1 -> same year's Jul 1
        ("2026-07-01", "2026-07-01"),  # exactly Jul 1
        ("2027-02-10", "2026-07-01"),  # before Jul 1 -> previous year's Jul 1
        ("2026-06-30", "2025-07-01"),  # last day before Jul -> previous year
    ])
    def test_comparison_window_never_inverts(self, server_date, expected_start):
        from ai_analytics.claim_comparison_service import resolve_comparison_window
        with patch(f"{SQL}.get_server_date", return_value=server_date):
            start, end = resolve_comparison_window(None)
        assert start == expected_start
        assert end == server_date

    def test_comparison_window_explicit_end_before_july(self):
        from ai_analytics.claim_comparison_service import resolve_comparison_window
        with patch(f"{SQL}.get_server_date", return_value="2026-08-13"):
            start, end = resolve_comparison_window("2026-03-01")
        assert start == "2025-07-01"
        assert end == "2026-03-01"

    def test_claim_comparison_requires_auth(self, test_client):
        response = test_client.get("/api/ai-analytics/claim-comparison")
        assert response.status_code in (401, 403)

    def test_claim_comparison_sql_failure_is_200_incomplete(self, test_client):
        patches = self._patches()
        patches[2] = patch(f"{SQL}.get_claim_comparison_scope", side_effect=RuntimeError("down"))
        for p in patches:
            p.start()
        try:
            response = test_client.get("/api/ai-analytics/claim-comparison", headers=AUTH)
        finally:
            for p in patches:
                p.stop()
        assert response.status_code == 200
        assert response.json()["data_complete"] is False

    def test_schema_check_reports_status_buckets_and_run_types(self, test_client):
        with patch(f"{SQL}.get_server_date", return_value="2026-08-13"), \
             patch(f"{SQL}.get_claims_row_start_column", return_value=ROW_START), \
             patch(f"{SQL}.get_table_column_names", return_value=["id", "timestamp"]), \
             patch(f"{SQL}.get_scope_status_counts", return_value=[
                 {"status": "Payment Received", "claim_count": 5},
                 {"status": "Mystery Stage", "claim_count": 2},
             ]), \
             patch(f"{SQL}.get_run_type_values", return_value=[
                 {"type_id": 1, "type_name": "Motor Vehicle Incident", "claim_count": 7},
                 {"type_id": 10, "type_name": None, "claim_count": 3},
                 {"type_id": None, "type_name": None, "claim_count": 1},
             ]):
            response = test_client.get("/api/ai-analytics/claim-comparison/schema-check", headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        statuses = {s["status"]: s for s in data["status_values"]}
        assert statuses["Payment Received"]["bucket"] == "paid"
        assert statuses["Payment Received"]["mapped"] is True
        assert statuses["Mystery Stage"]["bucket"] == "in_progress"
        assert statuses["Mystery Stage"]["mapped"] is False
        runs = {r["label"]: r for r in data["run_type_values"]}
        assert runs["Motor Vehicle Incident"]["claim_count"] == 7
        assert runs["Type 10"]["claim_count"] == 3
        assert runs["Unspecified"]["claim_count"] == 1
        assert "billing_target_values" not in data
        assert data["claims_deleted_columns"] == ["id", "timestamp"]
