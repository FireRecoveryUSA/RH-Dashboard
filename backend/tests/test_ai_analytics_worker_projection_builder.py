"""Unit tests for ai_analytics_worker.projection_builder (Phase 3).

Feature under test: the deterministic projection builder that converts a
single claim's source data (one ai_line_items document + its matching
ai_agent_conversations documents) into an ai_invoice_analytics projection
conforming to the frozen schema in Phase 0 Section 9.

Failure prevented:
- A malformed source document silently producing a wrong projection (e.g.
  wrong billability state, wrong confidence bucket, dropped data-quality
  flag) — the dashboard would then show incorrect analytics.
- A single bad claim raising an exception that stops the entire worker —
  the builder must never raise on malformed input.

Test level: unit. The builder is a pure function — no I/O, no async.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from bson import ObjectId

from ai_analytics_worker.projection_builder import (
    FLAG_INVALID_CONFIDENCE_VALUE,
    FLAG_MISSING_AI_LINE_ITEM,
    FLAG_MISSING_CLAIM_ID,
    FLAG_MISSING_CONVERSATIONS,
    FLAG_MISSING_UPDATED_TIMESTAMP,
    FLAG_RETRY_THREAD_WITHOUT_MAIN_THREAD,
    FLAG_WRITEBACK_STATE_INCONSISTENT,
    build_projection,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

PROCESSED_AT = datetime(2026, 8, 13, 12, 0, 0)


def make_ai_line_items(**overrides):
    """Build a representative ai_line_items source document.

    Defaults match the verified production schema (Phase 0). Override any
    field via kwargs.
    """
    base = {
        "_id": ObjectId(),
        "claim_id": 12345,
        "draft_claim_id": 67890,
        "run_number": "RUN-001",
        "department_id": 42,
        "department_name": "Fire Department",
        "inserted_at": datetime(2026, 7, 1, 9, 0, 0),
        "updated_at": datetime(2026, 7, 2, 10, 30, 0),
        "completed_at": datetime(2026, 7, 2, 10, 25, 0),
        "processing_time_seconds": 12.5,
        "claim_processing_status": "COMPLETED",
        "agent_exec_status": "success",
        "confidence_level": 85,
        "review_msg": "Auto-approved",
        "is_billable": None,
        "is_billable_not_determined": None,
        "billing_category": "Fire Suppression",
        "incident_duration_in_minutes": 45,
        "line_items_save_to_rh_status": True,
        "retry_count": 0,
        "thread_id": None,
        "retry_thread_id": None,
        "invoice_total": 1500.00,
        "line_items": [
            {
                "item": "Equipment usage",
                "description": "Engine 1 - 2 hours",
                "quantity": 2,
                "rate": 500.00,
                "line_item_total": 1000.00,
            },
            {
                "item": "Personnel",
                "description": "3 firefighters - 1 hour",
                "quantity": 3,
                "rate": 166.67,
                "line_item_total": 500.01,
            },
        ],
    }
    base.update(overrides)
    return base


def make_conversation(**overrides):
    """Build a representative ai_agent_conversations document."""
    base = {
        "_id": ObjectId(),
        "agent": "multi_agent_workflow",
        "status": "completed",
        "created_at": datetime(2026, 7, 1, 9, 5, 0),
        "processing_stage": "completed_all_agents",
        "request_type": "incident_analysis",
        "execution_time_seconds": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Identity (Section 9.1)
# ---------------------------------------------------------------------------


class TestIdentity:
    """Tests that Section 9.1 identity fields are correctly populated."""

    def test_claim_id_is_set_from_argument(self):
        doc = make_ai_line_items()
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["claim_id"] == 12345
        assert proj["_id"] == 12345  # _id = claim_id per Section 9.1

    def test_department_fields_copied_from_source(self):
        doc = make_ai_line_items(department_id=99, department_name="Rescue Squad")
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["department_id"] == 99
        assert proj["department_name"] == "Rescue Squad"

    def test_run_number_and_draft_claim_id_copied(self):
        doc = make_ai_line_items(run_number="RUN-42", draft_claim_id=11111)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["run_number"] == "RUN-42"
        assert proj["draft_claim_id"] == 11111

    def test_identity_fields_are_none_when_no_source_record(self):
        proj = build_projection(12345, None, [], PROCESSED_AT)

        assert proj["claim_id"] == 12345
        assert proj["_id"] == 12345
        assert proj["department_id"] is None
        assert proj["department_name"] is None
        assert proj["run_number"] is None
        assert proj["draft_claim_id"] is None


# ---------------------------------------------------------------------------
# Source tracking (Section 9.2)
# ---------------------------------------------------------------------------


class TestSourceTracking:
    """Tests that Section 9.2 source-tracking fields are correct."""

    def test_source_ai_line_item_ids_contains_source_doc_id(self):
        doc = make_ai_line_items()
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["source_ai_line_item_ids"] == [doc["_id"]]

    def test_source_ai_line_item_ids_empty_when_no_source(self):
        proj = build_projection(12345, None, [], PROCESSED_AT)

        assert proj["source_ai_line_item_ids"] == []

    def test_source_conversation_ids_contains_all_conversation_ids(self):
        doc = make_ai_line_items()
        c1 = make_conversation()
        c2 = make_conversation()
        proj = build_projection(12345, doc, [c1, c2], PROCESSED_AT)

        assert proj["source_conversation_ids"] == [c1["_id"], c2["_id"]]

    def test_source_conversation_ids_empty_when_no_conversations(self):
        doc = make_ai_line_items()
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["source_conversation_ids"] == []

    def test_source_latest_updated_at_copied_from_source(self):
        updated = datetime(2026, 7, 15, 14, 0, 0)
        doc = make_ai_line_items(updated_at=updated)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["source_latest_updated_at"] == updated

    def test_source_latest_updated_at_none_when_no_source(self):
        proj = build_projection(12345, None, [], PROCESSED_AT)

        assert proj["source_latest_updated_at"] is None

    def test_worker_processed_at_set_from_argument(self):
        doc = make_ai_line_items()
        when = datetime(2026, 8, 14, 8, 30, 0)
        proj = build_projection(12345, doc, [], when)

        assert proj["worker_processed_at"] == when

    def test_worker_version_set_from_config(self):
        doc = make_ai_line_items()
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        # worker_version comes from worker_config; just verify it's present
        assert "worker_version" in proj
        assert isinstance(proj["worker_version"], str)

    def test_projection_schema_version_set_from_config(self):
        doc = make_ai_line_items()
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert "projection_schema_version" in proj
        assert isinstance(proj["projection_schema_version"], int)


# ---------------------------------------------------------------------------
# AI processing (Section 9.3)
# ---------------------------------------------------------------------------


class TestAIProcessing:
    """Tests that Section 9.3 AI-processing fields are direct copies."""

    def test_processing_fields_copied_from_source(self):
        doc = make_ai_line_items(
            claim_processing_status="IN_PROGRESS",
            agent_exec_status="in_progress",
            processing_time_seconds=42.7,
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["ai_processing_status"] == "IN_PROGRESS"
        assert proj["agent_execution_status"] == "in_progress"
        assert proj["processing_duration_seconds"] == 42.7

    def test_timestamps_copied_from_source(self):
        inserted = datetime(2026, 6, 1, 8, 0, 0)
        updated = datetime(2026, 6, 2, 9, 0, 0)
        completed = datetime(2026, 6, 2, 8, 55, 0)
        doc = make_ai_line_items(
            inserted_at=inserted,
            updated_at=updated,
            completed_at=completed,
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["ai_inserted_at"] == inserted
        assert proj["ai_updated_at"] == updated
        assert proj["ai_completed_at"] == completed

    def test_processing_fields_none_when_no_source(self):
        proj = build_projection(12345, None, [], PROCESSED_AT)

        assert proj["ai_processing_status"] is None
        assert proj["agent_execution_status"] is None
        assert proj["processing_duration_seconds"] is None
        assert proj["ai_inserted_at"] is None
        assert proj["ai_updated_at"] is None
        assert proj["ai_completed_at"] is None


# ---------------------------------------------------------------------------
# Incident / billability (Section 9.4)
# ---------------------------------------------------------------------------


class TestBillability:
    """Tests that Section 9.4 billability fields and derived state are correct."""

    def test_billability_state_determined_when_billing_category_present(self):
        doc = make_ai_line_items(billing_category="Fire Suppression")
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        # classify_billability: billing_category present → determined, billable
        assert proj["billability_state"] == {
            "determined": True,
            "undetermined": False,
            "billable": True,
            "not_billable": False,
        }

    def test_billability_state_undetermined_when_billing_category_null(self):
        doc = make_ai_line_items(billing_category=None)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["billability_state"] == {
            "determined": False,
            "undetermined": True,
            "billable": False,
            "not_billable": False,
        }

    def test_billing_category_and_incident_duration_copied(self):
        doc = make_ai_line_items(
            billing_category="Motor Vehicle Accident",
            incident_duration_in_minutes=120,
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["billing_category"] == "Motor Vehicle Accident"
        assert proj["incident_duration_in_minutes"] == 120

    def test_billability_fields_none_when_no_source(self):
        proj = build_projection(12345, None, [], PROCESSED_AT)

        assert proj["is_billable"] is None
        assert proj["is_billable_not_determined"] is None
        assert proj["billing_category"] is None
        assert proj["incident_duration_in_minutes"] is None
        # billability_state is always a dict (classify_billability never raises)
        assert proj["billability_state"]["undetermined"] is True


# ---------------------------------------------------------------------------
# Quality (Section 9.5)
# ---------------------------------------------------------------------------


class TestQuality:
    """Tests that Section 9.5 quality fields and confidence bucket are correct."""

    def test_confidence_level_copied_and_bucketed(self):
        doc = make_ai_line_items(confidence_level=85)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["confidence_level"] == 85
        assert proj["confidence_bucket"] == "80-89"

    def test_confidence_bucket_low(self):
        doc = make_ai_line_items(confidence_level=45)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["confidence_bucket"] == "0-49"

    def test_confidence_bucket_high(self):
        doc = make_ai_line_items(confidence_level=95)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["confidence_bucket"] == "90-100"

    def test_confidence_bucket_unknown_when_none(self):
        doc = make_ai_line_items(confidence_level=None)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["confidence_level"] is None
        assert proj["confidence_bucket"] == "unknown"

    def test_review_message_copied(self):
        doc = make_ai_line_items(review_msg="Needs manual review")
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["review_message"] == "Needs manual review"

    def test_quality_fields_none_when_no_source(self):
        proj = build_projection(12345, None, [], PROCESSED_AT)

        assert proj["confidence_level"] is None
        assert proj["confidence_bucket"] == "unknown"
        assert proj["review_message"] is None


# ---------------------------------------------------------------------------
# Retry (Section 9.6)
# ---------------------------------------------------------------------------


class TestRetry:
    """Tests that Section 9.6 retry fields and evidence are correct."""

    def test_no_retry_when_retry_count_zero(self):
        doc = make_ai_line_items(retry_count=0)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["has_retry"] is False
        assert proj["retry_count"] == 0
        assert proj["retry_evidence"] == []

    def test_retry_count_from_field_and_evidence_recorded(self):
        doc = make_ai_line_items(retry_count=2)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["has_retry"] is True
        assert proj["retry_count"] == 2
        assert "retry_count_field" in proj["retry_evidence"]

    def test_retry_from_agent_exec_status(self):
        doc = make_ai_line_items(retry_count=0, agent_exec_status="retry")
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["has_retry"] is True
        assert proj["retry_count"] == 1
        assert "agent_exec_status" in proj["retry_evidence"]

    def test_retry_from_retry_thread_id(self):
        doc = make_ai_line_items(
            retry_count=0, thread_id="main-thread", retry_thread_id="retry-thread"
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["has_retry"] is True
        assert proj["retry_count"] == 1
        assert "retry_thread_id" in proj["retry_evidence"]

    def test_multiple_evidence_sources_combined(self):
        doc = make_ai_line_items(retry_count=1, agent_exec_status="retry")
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["retry_count"] == 1  # max of all sources
        assert "retry_count_field" in proj["retry_evidence"]
        assert "agent_exec_status" in proj["retry_evidence"]

    def test_retry_fields_default_when_no_source(self):
        proj = build_projection(12345, None, [], PROCESSED_AT)

        assert proj["has_retry"] is False
        assert proj["retry_count"] == 0
        assert proj["retry_evidence"] == []


# ---------------------------------------------------------------------------
# Conversation summary (Section 9.7)
# ---------------------------------------------------------------------------


class TestConversationSummary:
    """Tests that Section 9.7 conversation summary fields are correct."""

    def test_empty_conversations_produce_zero_counts(self):
        doc = make_ai_line_items()
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["conversation_count"] == 0
        assert proj["agent_count"] == 0
        assert proj["agents"] == []
        assert proj["processing_stages"] == []
        assert proj["failed_conversation_count"] == 0
        assert proj["successful_conversation_count"] == 0
        assert proj["conversation_duration_total_seconds"] is None

    def test_single_completed_conversation(self):
        doc = make_ai_line_items()
        conv = make_conversation(
            agent="multi_agent_workflow",
            status="completed",
            processing_stage="completed_all_agents",
        )
        proj = build_projection(12345, doc, [conv], PROCESSED_AT)

        assert proj["conversation_count"] == 1
        assert proj["agent_count"] == 1
        assert proj["agents"] == ["multi_agent_workflow"]
        assert proj["processing_stages"] == ["completed_all_agents"]
        assert proj["successful_conversation_count"] == 1
        assert proj["failed_conversation_count"] == 0

    def test_multiple_conversations_with_distinct_agents(self):
        doc = make_ai_line_items()
        convs = [
            make_conversation(agent="agent_a", processing_stage="stage_1"),
            make_conversation(agent="agent_b", processing_stage="stage_2"),
            make_conversation(agent="agent_a", processing_stage="stage_1"),
        ]
        proj = build_projection(12345, doc, convs, PROCESSED_AT)

        assert proj["conversation_count"] == 3
        assert proj["agent_count"] == 2  # distinct agents
        assert proj["agents"] == ["agent_a", "agent_b"]
        assert proj["processing_stages"] == ["stage_1", "stage_2"]

    def test_failed_conversation_count_includes_non_completed(self):
        doc = make_ai_line_items()
        convs = [
            make_conversation(status="completed"),
            make_conversation(status="error"),
            make_conversation(status="pending"),
        ]
        proj = build_projection(12345, doc, convs, PROCESSED_AT)

        assert proj["successful_conversation_count"] == 1
        assert proj["failed_conversation_count"] == 2

    def test_conversation_duration_summed_when_populated(self):
        doc = make_ai_line_items()
        convs = [
            make_conversation(execution_time_seconds=10.5),
            make_conversation(execution_time_seconds=5.0),
        ]
        proj = build_projection(12345, doc, convs, PROCESSED_AT)

        assert proj["conversation_duration_total_seconds"] == 15.5

    def test_conversation_duration_none_when_all_unpopulated(self):
        """Phase 0: execution_time_seconds is 0% populated → None."""
        doc = make_ai_line_items()
        convs = [make_conversation(execution_time_seconds=None)]
        proj = build_projection(12345, doc, convs, PROCESSED_AT)

        assert proj["conversation_duration_total_seconds"] is None

    def test_conversation_duration_partial_population_sums_only_present(self):
        doc = make_ai_line_items()
        convs = [
            make_conversation(execution_time_seconds=None),
            make_conversation(execution_time_seconds=7.0),
        ]
        proj = build_projection(12345, doc, convs, PROCESSED_AT)

        assert proj["conversation_duration_total_seconds"] == 7.0


# ---------------------------------------------------------------------------
# Line items (Section 9.8)
# ---------------------------------------------------------------------------


class TestLineItems:
    """Tests that Section 9.8 line-item summary fields are correct."""

    def test_line_item_count_matches_source(self):
        doc = make_ai_line_items()
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["ai_line_item_count"] == 2

    def test_line_items_summary_extracts_only_summary_fields(self):
        doc = make_ai_line_items(
            line_items=[
                {
                    "item": "Equipment",
                    "description": "Engine 1",
                    "quantity": 2,
                    "rate": 500.00,
                    "line_item_total": 1000.00,
                    "extra_field": "should not appear",
                    "incident_info": "large payload",
                }
            ]
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert len(proj["ai_line_items"]) == 1
        entry = proj["ai_line_items"][0]
        assert entry["item"] == "Equipment"
        assert entry["description"] == "Engine 1"
        assert entry["quantity"] == 2
        assert entry["rate"] == 500.00
        assert entry["line_item_total"] == 1000.00
        # Extra fields must NOT be in the summary
        assert "extra_field" not in entry
        assert "incident_info" not in entry

    def test_invoice_total_copied(self):
        doc = make_ai_line_items(invoice_total=2500.00)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["ai_invoice_total"] == 2500.00

    def test_line_items_empty_when_source_field_is_none(self):
        doc = make_ai_line_items(line_items=None)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["ai_line_items"] == []
        assert proj["ai_line_item_count"] == 0

    def test_line_items_empty_when_source_field_is_not_list(self):
        """Malformed line_items (not a list) → empty summary, no raise."""
        doc = make_ai_line_items(line_items="not a list")
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["ai_line_items"] == []
        assert proj["ai_line_item_count"] == 0

    def test_line_items_skips_non_dict_entries(self):
        """Malformed entries (non-dict) are skipped, not raised."""
        doc = make_ai_line_items(
            line_items=[
                {"item": "valid", "quantity": 1, "rate": 10, "line_item_total": 10},
                "not a dict",
                42,
            ]
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert len(proj["ai_line_items"]) == 1
        assert proj["ai_line_items"][0]["item"] == "valid"

    def test_line_items_none_when_no_source(self):
        proj = build_projection(12345, None, [], PROCESSED_AT)

        assert proj["ai_line_items"] == []
        assert proj["ai_line_item_count"] == 0
        assert proj["ai_invoice_total"] is None


# ---------------------------------------------------------------------------
# Writeback (Section 9.9)
# ---------------------------------------------------------------------------


class TestWriteback:
    """Tests that Section 9.9 writeback fields and derived state are correct."""

    def test_writeback_success_when_saved_true(self):
        doc = make_ai_line_items(line_items_save_to_rh_status=True)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["line_items_save_to_rh_status"] is True
        assert proj["writeback_state"] == "success"

    def test_writeback_failed_when_saved_false_and_completed(self):
        doc = make_ai_line_items(
            line_items_save_to_rh_status=False,
            claim_processing_status="COMPLETED",
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["writeback_state"] == "failed_or_not_saved"

    def test_writeback_not_required_when_billing_not_enabled(self):
        doc = make_ai_line_items(
            line_items_save_to_rh_status=False,
            claim_processing_status="BILLING_LEVEL_NOT_ENABLED",
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["writeback_state"] == "not_required"

    def test_writeback_pending_when_in_progress_and_not_saved(self):
        doc = make_ai_line_items(
            line_items_save_to_rh_status=False,
            claim_processing_status="IN_PROGRESS",
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["writeback_state"] == "pending"

    def test_writeback_unknown_when_status_none(self):
        doc = make_ai_line_items(
            line_items_save_to_rh_status=None,
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["writeback_state"] == "unknown"
        assert proj["line_items_save_to_rh_status"] is None


# ---------------------------------------------------------------------------
# Data-quality flags (Section 9.10 / 9.11)
# ---------------------------------------------------------------------------


class TestDataQualityFlags:
    """Tests that Section 9.11 data-quality flags are set correctly."""

    def test_no_flags_when_all_data_present_and_valid(self):
        doc = make_ai_line_items()
        conv = make_conversation()
        proj = build_projection(12345, doc, [conv], PROCESSED_AT)

        assert proj["data_quality_flags"] == []

    def test_missing_ai_line_item_flag_when_no_source(self):
        proj = build_projection(12345, None, [], PROCESSED_AT)

        assert FLAG_MISSING_AI_LINE_ITEM in proj["data_quality_flags"]

    def test_missing_conversations_flag_when_no_conversations(self):
        doc = make_ai_line_items()
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert FLAG_MISSING_CONVERSATIONS in proj["data_quality_flags"]

    def test_missing_claim_id_flag_when_claim_id_none(self):
        doc = make_ai_line_items()
        proj = build_projection(None, doc, [], PROCESSED_AT)

        # claim_id is derived from the source doc, so it won't be None here.
        # The flag only fires when claim_id is None AND can't be derived.
        assert FLAG_MISSING_CLAIM_ID not in proj["data_quality_flags"]
        assert proj["claim_id"] == 12345  # derived from doc

    def test_missing_claim_id_flag_when_claim_id_none_and_no_source(self):
        proj = build_projection(None, None, [], PROCESSED_AT)

        # No claim_id and no source → nothing to project
        assert proj is None

    def test_invalid_confidence_flag_when_above_100(self):
        doc = make_ai_line_items(confidence_level=150)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert FLAG_INVALID_CONFIDENCE_VALUE in proj["data_quality_flags"]

    def test_invalid_confidence_flag_when_below_0(self):
        doc = make_ai_line_items(confidence_level=-5)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert FLAG_INVALID_CONFIDENCE_VALUE in proj["data_quality_flags"]

    def test_no_invalid_confidence_flag_at_boundary_0(self):
        doc = make_ai_line_items(confidence_level=0)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert FLAG_INVALID_CONFIDENCE_VALUE not in proj["data_quality_flags"]

    def test_no_invalid_confidence_flag_at_boundary_100(self):
        doc = make_ai_line_items(confidence_level=100)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert FLAG_INVALID_CONFIDENCE_VALUE not in proj["data_quality_flags"]

    def test_missing_updated_timestamp_flag_when_updated_at_none(self):
        doc = make_ai_line_items(updated_at=None)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert FLAG_MISSING_UPDATED_TIMESTAMP in proj["data_quality_flags"]

    def test_no_missing_updated_timestamp_flag_when_no_source(self):
        """If there's no source, we don't flag missing updated_at (redundant)."""
        proj = build_projection(12345, None, [], PROCESSED_AT)

        assert FLAG_MISSING_UPDATED_TIMESTAMP not in proj["data_quality_flags"]

    def test_retry_thread_without_main_thread_flag(self):
        doc = make_ai_line_items(thread_id=None, retry_thread_id="retry-1")
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert FLAG_RETRY_THREAD_WITHOUT_MAIN_THREAD in proj["data_quality_flags"]

    def test_no_retry_thread_flag_when_both_present(self):
        doc = make_ai_line_items(thread_id="main", retry_thread_id="retry")
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert FLAG_RETRY_THREAD_WITHOUT_MAIN_THREAD not in proj["data_quality_flags"]

    def test_no_retry_thread_flag_when_neither_present(self):
        doc = make_ai_line_items(thread_id=None, retry_thread_id=None)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert FLAG_RETRY_THREAD_WITHOUT_MAIN_THREAD not in proj["data_quality_flags"]

    def test_writeback_inconsistent_flag_when_saved_true_but_in_progress(self):
        doc = make_ai_line_items(
            line_items_save_to_rh_status=True,
            claim_processing_status="IN_PROGRESS",
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert FLAG_WRITEBACK_STATE_INCONSISTENT in proj["data_quality_flags"]

    def test_writeback_inconsistent_flag_when_saved_true_but_initiated(self):
        doc = make_ai_line_items(
            line_items_save_to_rh_status=True,
            claim_processing_status="INITIATED",
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert FLAG_WRITEBACK_STATE_INCONSISTENT in proj["data_quality_flags"]

    def test_no_writeback_inconsistent_flag_when_saved_true_and_completed(self):
        doc = make_ai_line_items(
            line_items_save_to_rh_status=True,
            claim_processing_status="COMPLETED",
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert FLAG_WRITEBACK_STATE_INCONSISTENT not in proj["data_quality_flags"]

    def test_multiple_flags_accumulate(self):
        """Multiple data-quality issues produce multiple flags."""
        doc = make_ai_line_items(
            updated_at=None,
            confidence_level=200,
            thread_id=None,
            retry_thread_id="retry-1",
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        flags = proj["data_quality_flags"]
        assert FLAG_MISSING_CONVERSATIONS in flags
        assert FLAG_MISSING_UPDATED_TIMESTAMP in flags
        assert FLAG_INVALID_CONFIDENCE_VALUE in flags
        assert FLAG_RETRY_THREAD_WITHOUT_MAIN_THREAD in flags

    def test_data_quality_booleans_correct_when_no_source(self):
        proj = build_projection(12345, None, [], PROCESSED_AT)

        assert proj["has_ai_line_item_record"] is False
        assert proj["has_conversation_records"] is False
        assert proj["multiple_ai_records"] is False
        assert proj["source_record_count"] == 0

    def test_data_quality_booleans_correct_when_source_present(self):
        doc = make_ai_line_items()
        conv = make_conversation()
        proj = build_projection(12345, doc, [conv], PROCESSED_AT)

        assert proj["has_ai_line_item_record"] is True
        assert proj["has_conversation_records"] is True
        assert proj["multiple_ai_records"] is False  # single doc from source
        assert proj["source_record_count"] == 1


# ---------------------------------------------------------------------------
# Robustness — never raise on malformed input
# ---------------------------------------------------------------------------


class TestRobustness:
    """Tests that the builder never raises on malformed source data."""

    def test_empty_dict_as_source_does_not_raise(self):
        """A source doc with no fields should not crash the builder."""
        proj = build_projection(12345, {}, [], PROCESSED_AT)

        assert proj is not None
        assert proj["claim_id"] == 12345
        assert proj["has_ai_line_item_record"] is True
        # All derived fields should have safe defaults
        assert proj["confidence_bucket"] == "unknown"
        assert proj["writeback_state"] == "unknown"
        assert proj["retry_count"] == 0

    def test_conversation_with_missing_fields_does_not_raise(self):
        """Conversations with missing agent/status should not crash.

        Missing status != "completed" → counted as failed (Section 9.7).
        """
        doc = make_ai_line_items()
        convs = [{}, {"agent": "a"}]  # missing status, missing _id
        proj = build_projection(12345, doc, convs, PROCESSED_AT)

        assert proj["conversation_count"] == 2
        assert proj["successful_conversation_count"] == 0
        assert proj["failed_conversation_count"] == 2

    def test_confidence_as_string_does_not_raise(self):
        """Non-numeric confidence should not crash — flagged as invalid."""
        doc = make_ai_line_items(confidence_level="not a number")
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert FLAG_INVALID_CONFIDENCE_VALUE in proj["data_quality_flags"]
        assert proj["confidence_bucket"] == "unknown"

    def test_line_items_with_non_dict_entries_does_not_raise(self):
        doc = make_ai_line_items(line_items=[None, 42, "bad", {"item": "ok"}])
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert len(proj["ai_line_items"]) == 1
        assert proj["ai_line_items"][0]["item"] == "ok"


# ---------------------------------------------------------------------------
# DRY — reuses normalization_core.py
# ---------------------------------------------------------------------------


class TestDRYReuse:
    """Tests that the builder delegates to normalization_core.py functions."""

    def test_uses_classify_writeback_status_from_core(self):
        """writeback_state must match classify_writeback_status output."""
        doc = make_ai_line_items(
            line_items_save_to_rh_status=False,
            claim_processing_status="BILLING_LEVEL_NOT_ENABLED",
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        # classify_writeback_status(False, "BILLING_LEVEL_NOT_ENABLED") → "not_required"
        assert proj["writeback_state"] == "not_required"

    def test_uses_confidence_bucket_from_core(self):
        doc = make_ai_line_items(confidence_level=75)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        # confidence_bucket(75) → "70-79"
        assert proj["confidence_bucket"] == "70-79"

    def test_uses_classify_billability_from_core(self):
        doc = make_ai_line_items(billing_category="Fire Suppression")
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        # classify_billability with billing_category → determined, billable
        assert proj["billability_state"]["determined"] is True
        assert proj["billability_state"]["billable"] is True

    def test_uses_calculate_retry_count_from_core(self):
        doc = make_ai_line_items(retry_count=3)
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        # calculate_retry_count with retry_count=3 → 3
        assert proj["retry_count"] == 3
        assert proj["has_retry"] is True


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Tests that the builder is deterministic — same inputs, same output."""

    def test_same_inputs_produce_same_output(self):
        doc = make_ai_line_items()
        conv = make_conversation()

        proj1 = build_projection(12345, doc, [conv], PROCESSED_AT)
        proj2 = build_projection(12345, doc, [conv], PROCESSED_AT)

        assert proj1 == proj2

    def test_different_worker_processed_at_changes_only_that_field(self):
        doc = make_ai_line_items()
        t1 = datetime(2026, 8, 13, 12, 0, 0)
        t2 = datetime(2026, 8, 14, 8, 0, 0)

        proj1 = build_projection(12345, doc, [], t1)
        proj2 = build_projection(12345, doc, [], t2)

        assert proj1["worker_processed_at"] == t1
        assert proj2["worker_processed_at"] == t2
        # Everything else is identical
        proj1.pop("worker_processed_at")
        proj2.pop("worker_processed_at")
        assert proj1 == proj2


# ---------------------------------------------------------------------------
# Nothing-to-project edge case
# ---------------------------------------------------------------------------


class TestNothingToProject:
    """Tests the edge case where there's no claim_id and no source document."""

    def test_returns_none_when_no_claim_id_and_no_source(self):
        proj = build_projection(None, None, [], PROCESSED_AT)
        assert proj is None

    def test_returns_projection_when_claim_id_none_but_source_present(self):
        """claim_id is derived from the source doc if the caller passed None."""
        doc = make_ai_line_items(claim_id=99999)
        proj = build_projection(None, doc, [], PROCESSED_AT)

        assert proj is not None
        assert proj["claim_id"] == 99999
        assert proj["_id"] == 99999

    def test_returns_projection_with_none_id_when_claim_id_derivation_fails(self):
        """When claim_id derivation fails, projection has _id=None and the flag.

        The caller passes claim_id=None and the source doc has no usable
        claim_id field. The builder still produces a projection (so the
        caller can decide what to do with it), but with _id=None and the
        MISSING_CLAIM_ID data-quality flag set. The caller
        (projection_repository, in a later phase) is responsible for
        checking _id before persisting — MongoDB rejects _id=None on
        insert, and an auto-generated ObjectId would be wrong since _id
        must be the integer claim_id per Section 9.1.
        """
        # Source doc has no claim_id field
        doc = make_ai_line_items()
        doc.pop("claim_id")
        proj = build_projection(None, doc, [], PROCESSED_AT)

        assert proj is not None
        assert proj["_id"] is None
        assert proj["claim_id"] is None
        assert FLAG_MISSING_CLAIM_ID in proj["data_quality_flags"]

    def test_returns_projection_with_none_id_when_claim_id_non_numeric(self):
        """Non-numeric claim_id on the source doc also fails derivation.

        The source doc has a claim_id that can't be converted to int
        (e.g., a malformed string). Derivation returns None, _id is None,
        and the MISSING_CLAIM_ID flag is set.
        """
        doc = make_ai_line_items(claim_id="not-a-number")
        proj = build_projection(None, doc, [], PROCESSED_AT)

        assert proj is not None
        assert proj["_id"] is None
        assert proj["claim_id"] is None
        assert FLAG_MISSING_CLAIM_ID in proj["data_quality_flags"]


# ---------------------------------------------------------------------------
# Phase 10: line items with resources, conversation summaries, trace fields
# ---------------------------------------------------------------------------


class TestPhase10LineItemsWithResources:
    """Phase 10: line items now include ``resources`` for Invoice Trace."""

    def test_resources_included_in_line_item_summary(self):
        """``resources`` from the source line item is carried into the projection."""
        doc = make_ai_line_items(
            line_items=[
                {
                    "item": "Equipment usage",
                    "description": "Engine 1",
                    "quantity": 2,
                    "rate": 500.00,
                    "line_item_total": 1000.00,
                    "resources": [
                        {"resourceLabel": "Fuel", "quantity": 10, "amount": 50.00},
                        {"resourceLabel": "Oil", "quantity": 1, "amount": 25.00},
                    ],
                }
            ]
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert len(proj["ai_line_items"]) == 1
        entry = proj["ai_line_items"][0]
        assert entry["item"] == "Equipment usage"
        assert isinstance(entry["resources"], list)
        assert len(entry["resources"]) == 2
        assert entry["resources"][0]["resourceLabel"] == "Fuel"

    def test_resources_empty_when_not_in_source(self):
        """When the source line item has no ``resources``, the projection has an empty list."""
        doc = make_ai_line_items(
            line_items=[
                {"item": "Equipment", "quantity": 1, "rate": 100, "line_item_total": 100}
            ]
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        entry = proj["ai_line_items"][0]
        assert entry["resources"] == []

    def test_line_item_aliases_are_canonicalized(self):
        """Legacy label/amount/total aliases preserve trace comparisons."""
        doc = make_ai_line_items(
            line_items=[
                {
                    "label": "Equipment",
                    "quantity": 2,
                    "amount": 125.0,
                    "total": 250.0,
                }
            ]
        )
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["ai_line_items"] == [{
            "item": "Equipment",
            "description": None,
            "quantity": 2,
            "rate": 125.0,
            "line_item_total": 250.0,
            "resources": [],
        }]


class TestPhase10ConversationSummaries:
    """Phase 10: per-conversation summaries for /diagnostics/agents."""

    def test_conversation_summaries_populated(self):
        """Per-conversation summaries are stored in the projection."""
        conv1 = make_conversation(
            agent="agent_a",
            status="completed",
            processing_stage="stage_1",
            request_type="incident_analysis",
        )
        conv2 = make_conversation(
            agent="agent_b",
            status="failed",
            processing_stage="stage_2",
            request_type="billability_check",
        )
        doc = make_ai_line_items()
        proj = build_projection(12345, doc, [conv1, conv2], PROCESSED_AT)

        summaries = proj["conversation_summaries"]
        assert len(summaries) == 2
        assert summaries[0]["agent"] == "agent_a"
        assert summaries[0]["status"] == "completed"
        assert summaries[0]["processing_stage"] == "stage_1"
        assert summaries[0]["request_type"] == "incident_analysis"
        assert summaries[0]["created_at"] == "2026-07-01T09:05:00"
        assert summaries[0]["conversation_id"] is not None
        assert summaries[1]["agent"] == "agent_b"
        assert summaries[1]["status"] == "failed"

    def test_conversation_summaries_exclude_payload_fields(self):
        """Large payload fields are NOT in the conversation summaries."""
        conv = make_conversation(
            input_data={"claim_id": 12345, "large": "payload"},
            incident_json={"claim_id": 12345, "data": "here"},
            results={"output": "result"},
            output_data={"final": "output"},
        )
        doc = make_ai_line_items()
        proj = build_projection(12345, doc, [conv], PROCESSED_AT)

        summary = proj["conversation_summaries"][0]
        assert "input_data" not in summary
        assert "incident_json" not in summary
        assert "results" not in summary
        assert "output_data" not in summary

    def test_conversation_summaries_empty_when_no_conversations(self):
        """No conversations → empty conversation_summaries list."""
        doc = make_ai_line_items()
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["conversation_summaries"] == []


class TestPhase10TraceFields:
    """Phase 10: conversation_id and thread_id_is_billable from ai_line_items."""

    def test_conversation_id_copied_from_source(self):
        """``conversation_id`` from ai_line_items is in the projection."""
        doc = make_ai_line_items(conversation_id="conv-abc-123")
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["conversation_id"] == "conv-abc-123"

    def test_conversation_id_none_when_not_in_source(self):
        """No conversation_id in source → None in projection."""
        doc = make_ai_line_items()
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["conversation_id"] is None

    def test_thread_id_is_billable_copied_from_source(self):
        """``thread_id_is_billable`` from ai_line_items is in the projection."""
        doc = make_ai_line_items(thread_id_is_billable="yes")
        proj = build_projection(12345, doc, [], PROCESSED_AT)

        assert proj["thread_id_is_billable"] == "yes"

    def test_trace_fields_none_when_no_source(self):
        """Trace fields are None when there's no source document."""
        proj = build_projection(12345, None, [], PROCESSED_AT)

        assert proj["conversation_id"] is None
        assert proj["thread_id_is_billable"] is None


# ---------------------------------------------------------------------------
# Step-1 identification + intake evidence (schema v4)
# ---------------------------------------------------------------------------


class TestStep1SourceFields:
    """fee_schedule.STEP1_SOURCE_FIELDS pass through the projection so the
    funnel can match results against the department fee catalog."""

    SOURCE = {
        "billing_level": "Vehicle Fire",
        "level_identification_confidence": 88,
        "level_identification_low_confidence": False,
        "level_identification_reasoning": "matched tile",
        "intake_status": "IDENTIFIED",
        "intake_evaluated_at": "2026-10-01T00:00:00",
        "intake_evaluation_count": 2,
        "level_label_matched": "Vehicle fire",
    }

    def test_step1_fields_copied_from_source(self):
        doc = make_ai_line_items(**self.SOURCE)
        proj = build_projection(12345, doc, [], PROCESSED_AT)
        for field, value in self.SOURCE.items():
            assert proj[field] == value

    def test_step1_fields_none_when_no_source(self):
        proj = build_projection(12345, None, [], PROCESSED_AT)
        for field in self.SOURCE:
            assert proj[field] is None
