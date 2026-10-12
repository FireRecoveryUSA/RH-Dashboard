"""Pydantic models for the AI Analytics API surface.

These models define the contract between the FastAPI routes, the analytics
services, and the React frontend. They are deliberately explicit so a future
read-model migration can swap the repository internals without changing the
API contract.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

class AiAnalyticsFilters(BaseModel):
    """Universal filter model shared by outcome and diagnostic dashboards."""

    start_date: Optional[str] = Field(None, description="ISO date YYYY-MM-DD")
    end_date: Optional[str] = Field(None, description="ISO date YYYY-MM-DD")
    department_id: Optional[int] = None
    business_outcome: Optional[str] = Field(
        None, description="released | cancelled_rejected | pending | unknown"
    )
    ai_processing_status: Optional[str] = Field(
        None, description="INITIATED | IN_PROGRESS | COMPLETED | ERROR | CANCELLED | BILLING_LEVEL_NOT_ENABLED"
    )
    agent_execution_status: Optional[str] = Field(
        None, description="pending | in_progress | success | error | retry | completed_with_issues"
    )
    confidence_min: Optional[float] = Field(None, ge=0, le=100)
    confidence_max: Optional[float] = Field(None, ge=0, le=100)
    has_retry: Optional[bool] = None
    writeback_status: Optional[str] = Field(
        None, description="success | not_required | pending | failed_or_not_saved | unknown"
    )
    billing_category: Optional[str] = None
    reason_category: Optional[str] = Field(
        None, description="Normalized rejection reason category"
    )
    # Pagination
    page: int = Field(1, ge=1)
    page_size: int = Field(50, ge=1, le=250)
    sort_by: str = Field("ai_business_updated_at", description="Column to sort by")
    sort_direction: str = Field("desc", description="asc | desc")
    # Date basis for filtering
    date_basis: str = Field(
        "business_status_date",
        description="business_status_date | claim_created_date | ai_record_updated_date",
    )


# ---------------------------------------------------------------------------
# Outcome models
# ---------------------------------------------------------------------------

class AiOutcomeSummary(BaseModel):
    total_ai_invoices: int
    did_not_qualify: int = 0
    released: int
    cancelled_rejected: int
    pending: int
    unknown: int
    terminal_count: int
    business_release_rate: float
    rejection_rate: float
    ai_completed: int
    ai_failed: int
    ai_not_enabled: int
    writeback_success: int
    writeback_failed: int
    avg_confidence: Optional[float] = None
    source_status: Dict[str, str] = Field(
        default_factory=dict,
        description="recoveryhub_sql / recoveryhub_ai_mongo availability",
    )
    data_complete: bool = True


class AiOutcomeTrendPoint(BaseModel):
    period: str
    total: int
    released: int
    rejected: int
    pending: int
    release_rate: Optional[float] = None


class AiRejectionReasonBreakdown(BaseModel):
    raw_reason: str
    count: int


class AiRejectionReasonStat(BaseModel):
    normalized_category: str
    count: int
    percent: float
    raw_reason_breakdown: List[AiRejectionReasonBreakdown] = Field(default_factory=list)


class AiDepartmentOutcomeStat(BaseModel):
    department_id: int
    department_name: Optional[str] = None
    state: Optional[str] = None
    volume: int
    released: int
    rejected: int
    pending: int
    release_rate: Optional[float] = None
    ai_completion_rate: Optional[float] = None
    writeback_failure_rate: Optional[float] = None
    avg_confidence: Optional[float] = None
    retry_count: int = 0
    human_intervention_count: int = 0


class AiPipelineStageBreakdown(BaseModel):
    label: str
    count: int
    # Step-1 result rows carry a catalog match_status and a description;
    # other stages leave them unset.
    match_status: Optional[str] = None
    description: str = ""


class AiPipelineStageStat(BaseModel):
    stage: str
    count: int
    description: str = ""
    breakdown: Optional[List[AiPipelineStageBreakdown]] = None


class AiBillabilityStat(BaseModel):
    """Phase 4 incident/billability evaluation metrics."""

    ai_records: int
    billability_determined: int
    billability_undetermined: int
    billable: int
    not_billable: int
    billing_category_distribution: Dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Diagnostics models
# ---------------------------------------------------------------------------

class AiDiagnosticsSummary(BaseModel):
    ai_runs: int
    did_not_qualify: int = 0
    completed: int
    errors: int
    retries: int
    retry_success: int
    low_confidence: int
    writeback_failures: int
    avg_duration: Optional[float] = None
    p50_duration: Optional[float] = None
    p90_duration: Optional[float] = None
    p95_duration: Optional[float] = None
    source_status: Dict[str, str] = Field(default_factory=dict)
    data_complete: bool = True


class AiConfidenceBucketStat(BaseModel):
    bucket: str
    count: int
    released: int
    rejected: int
    pending: int
    release_rate: Optional[float] = None


class AiAgentStat(BaseModel):
    agent: str
    status: str
    processing_stage: str
    request_type: str
    count: int
    avg_execution_time: Optional[float] = None


# ---------------------------------------------------------------------------
# Invoice cohort / trace models
# ---------------------------------------------------------------------------

class AiInvoiceListItem(BaseModel):
    claim_id: int
    invoice_number: Optional[str] = None
    department_id: Optional[int] = None
    department_name: Optional[str] = None
    run_number: Optional[str] = None
    claim_created_at: Optional[str] = None
    ai_business_updated_at: Optional[str] = None
    business_outcome: str
    raw_rejection_reason: Optional[str] = None
    raw_rejection_description: Optional[str] = None
    normalized_rejection_category: Optional[str] = None
    ai_processing_status: Optional[str] = None
    agent_execution_status: Optional[str] = None
    is_billable: Optional[bool] = None
    billing_category: Optional[str] = None
    confidence: Optional[float] = None
    writeback_status: str = "unknown"
    retry_count: int = 0
    thread_id: Optional[str] = None
    ai_record_state: str = "present"
    business_record_state: str = "present"
    invoice_total: Optional[float] = None
    amount_invoiced: Optional[float] = None
    processing_time_seconds: Optional[float] = None


class AiInvoiceCohortResponse(BaseModel):
    invoices: List[AiInvoiceListItem]
    total_count: int
    page: int
    page_size: int
    source_status: Dict[str, str] = Field(default_factory=dict)
    data_complete: bool = True


class AiConversationRecord(BaseModel):
    conversation_id: str
    agent: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[str] = None
    processing_stage: Optional[str] = None
    request_type: Optional[str] = None
    execution_time_seconds: Optional[float] = None
    input_data: Optional[Any] = None
    incident_json: Optional[Any] = None
    results: Optional[Any] = None
    output_data: Optional[Any] = None


class AiLineItemEntry(BaseModel):
    item: Optional[str] = None
    description: Optional[str] = None
    quantity: Optional[float] = None
    rate: Optional[float] = None
    line_item_total: Optional[float] = None
    resources: List[Dict[str, Any]] = Field(default_factory=list)


class AiFinalLineItemEntry(BaseModel):
    claim_service_id: int
    item: Optional[str] = None
    rate: Optional[float] = None
    quantity: Optional[float] = None
    description: Optional[str] = None
    resources: List[Dict[str, Any]] = Field(default_factory=list)


class AiLineItemComparison(BaseModel):
    ai_original_amount: Optional[float] = None
    final_rh_amount: Optional[float] = None
    difference: Optional[float] = None
    ai_only_items: List[str] = Field(default_factory=list)
    rh_only_items: List[str] = Field(default_factory=list)
    quantity_changes: List[Dict[str, Any]] = Field(default_factory=list)
    rate_changes: List[Dict[str, Any]] = Field(default_factory=list)


class AiInvoiceTrace(BaseModel):
    # Identity
    claim_id: int
    invoice_number: Optional[str] = None
    run_number: Optional[str] = None
    department_id: Optional[int] = None
    department_name: Optional[str] = None
    claim_created_at: Optional[str] = None
    alarm_received: Optional[str] = None
    call_cleared: Optional[str] = None
    recoveryhub_claim_status: Optional[str] = None

    # Business outcome
    business_outcome: str
    ai_inv_process_status: Optional[int] = None
    business_status_updated_at: Optional[str] = None
    process_logs: List[Dict[str, Any]] = Field(default_factory=list)
    cancellation_reason: Optional[str] = None
    cancellation_description: Optional[str] = None
    cancellation_date: Optional[str] = None
    business_user_id: Optional[int] = None

    # AI processing
    ai_record_state: str = "present"
    claim_processing_status: Optional[str] = None
    agent_exec_status: Optional[str] = None
    inserted_at: Optional[str] = None
    updated_at: Optional[str] = None
    completed_at: Optional[str] = None
    billing_category: Optional[str] = None
    incident_duration_in_minutes: Optional[int] = None
    confidence_level: Optional[float] = None
    review_msg: Optional[str] = None
    line_items_save_to_rh_status: Optional[bool] = None
    invoice_total: Optional[float] = None
    processing_time_seconds: Optional[float] = None
    retry_count: int = 0
    conversation_id: Optional[str] = None

    # Threads/retries (mostly unpopulated in current production but kept
    # for forward compatibility)
    thread_id_is_billable: Optional[str] = None
    thread_id: Optional[str] = None
    retry_thread_id: Optional[str] = None

    # Agent conversations
    conversations: List[AiConversationRecord] = Field(default_factory=list)

    # AI-generated line items
    ai_line_items: List[AiLineItemEntry] = Field(default_factory=list)

    # RecoveryHub final line items
    final_line_items: List[AiFinalLineItemEntry] = Field(default_factory=list)

    # Comparison
    comparison: Optional[AiLineItemComparison] = None

    # Raw data (view-only)
    raw_ai_record: Optional[Dict[str, Any]] = None
    source_status: Dict[str, str] = Field(default_factory=dict)
    data_complete: bool = True
