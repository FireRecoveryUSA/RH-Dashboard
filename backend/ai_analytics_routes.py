"""FastAPI routes for AI Analytics — outcome and billability endpoints.

All endpoints are read-only and require authentication via ``get_current_user``.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from database import get_ai_db
from ai_analytics.models import (
    AiAnalyticsFilters,
    AiOutcomeSummary,
    AiOutcomeTrendPoint,
    AiRejectionReasonStat,
    AiDepartmentOutcomeStat,
    AiPipelineStageStat,
    AiBillabilityStat,
    AiInvoiceCohortResponse,
    AiDiagnosticsSummary,
    AiConfidenceBucketStat,
    AiAgentStat,
)
from ai_analytics.outcome_service import (
    get_outcome_summary,
    get_outcome_funnel,
    get_outcome_trend,
    get_rejection_reasons,
    get_department_outcomes,
    get_invoice_cohort,
    get_billability_stats,
)
from ai_analytics.diagnostics_service import (
    get_diagnostics_summary,
    get_status_distribution,
    get_confidence_distribution,
    get_retry_analysis,
    get_writeback_analysis,
    get_agent_stats,
)
from ai_analytics.invoice_trace_service import get_invoice_trace
from ai_analytics.models import AiInvoiceTrace

logger = logging.getLogger(__name__)

ai_analytics_router = APIRouter(prefix="/ai-analytics", tags=["AI Analytics"])


def _handle_route_errors(
    operation: str,
    *,
    context_params: tuple[str, ...] = (),
) -> Callable:
    """Maps service-layer exceptions to HTTP responses for a route handler.

    Every endpoint in this module shares the same error contract, so it lives
    here rather than being repeated per handler:

    - ``HTTPException`` is re-raised untouched. Handlers that validate their
      own arguments (e.g. the ``grain`` check on the trend endpoint) raise it
      directly, and it already carries the intended status code. Catching it
      as a generic error would silently turn deliberate 400s into 500s.
    - ``ValueError`` becomes a 400. The service and repository layers raise it
      for caller-correctable input problems such as an over-long date span.
    - Anything else is logged with a traceback and becomes a generic 500, so
      internal details are never returned to the client.

    ``operation`` completes the log line "Failed to {operation}".
    ``context_params`` names handler arguments to append to that line for
    debuggability (e.g. ``("claim_id",)`` -> "... (claim_id=123)"). Only pass
    non-sensitive identifiers, since the value is written to the log.
    """
    def decorator(func: Callable) -> Callable:
        # functools.wraps sets __wrapped__, which inspect.signature follows.
        # FastAPI therefore still sees the original signature and resolves
        # query params and dependencies exactly as it would undecorated.
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except HTTPException:
                raise
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except Exception:
                context = ", ".join(
                    f"{name}={kwargs.get(name)!r}" for name in context_params
                )
                suffix = f" ({context})" if context else ""
                logger.exception(f"Failed to {operation}{suffix}")
                raise HTTPException(status_code=500, detail="Internal server error")
        return wrapper
    return decorator


def _build_filters(
    start_date: Optional[str],
    end_date: Optional[str],
    department_id: Optional[int],
    business_outcome: Optional[str],
    ai_processing_status: Optional[str],
    agent_execution_status: Optional[str],
    confidence_min: Optional[float],
    confidence_max: Optional[float],
    has_retry: Optional[bool],
    writeback_status: Optional[str],
    billing_category: Optional[str],
    reason_category: Optional[str],
    page: int,
    page_size: int,
    sort_by: str,
    sort_direction: str,
    date_basis: str,
) -> AiAnalyticsFilters:
    """Build the unified filter model from query parameters."""
    return AiAnalyticsFilters(
        start_date=start_date,
        end_date=end_date,
        department_id=department_id,
        business_outcome=business_outcome,
        ai_processing_status=ai_processing_status,
        agent_execution_status=agent_execution_status,
        confidence_min=confidence_min,
        confidence_max=confidence_max,
        has_retry=has_retry,
        writeback_status=writeback_status,
        billing_category=billing_category,
        reason_category=reason_category,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_direction=sort_direction,
        date_basis=date_basis,
    )


# Common query parameter definitions to reduce duplication
_Q_START_DATE = Query(None, description="ISO start date (YYYY-MM-DD)")
_Q_END_DATE = Query(None, description="ISO end date (YYYY-MM-DD)")
_Q_DEPT_ID = Query(None, description="Filter by department ID")
_Q_BUSINESS_OUTCOME = Query(
    None,
    description="Filter by business outcome: released | cancelled_rejected | pending | unknown",
)
_Q_AI_PROCESSING_STATUS = Query(
    None,
    description="Filter by AI processing status (e.g. COMPLETED, INITIATED, BILLING_LEVEL_NOT_ENABLED)",
)
_Q_AGENT_EXEC_STATUS = Query(
    None,
    description="Filter by agent execution status (e.g. success, completed_with_issues)",
)
_Q_CONFIDENCE_MIN = Query(None, ge=0, le=100, description="Minimum confidence (0-100)")
_Q_CONFIDENCE_MAX = Query(None, ge=0, le=100, description="Maximum confidence (0-100)")
_Q_HAS_RETRY = Query(None, description="Filter by retry presence (true/false)")
_Q_WRITEBACK_STATUS = Query(
    None,
    description="Filter by writeback status: success | not_required | pending | failed_or_not_saved | unknown",
)
_Q_BILLING_CATEGORY = Query(None, description="Filter by billing category")
_Q_REASON_CATEGORY = Query(None, description="Filter by normalized rejection reason category")
_Q_PAGE = Query(1, ge=1, description="Page number (1-based)")
_Q_PAGE_SIZE = Query(50, ge=1, le=250, description="Page size")
_Q_SORT_BY = Query("ai_business_updated_at", description="Sort column")
_Q_SORT_DIRECTION = Query("desc", description="Sort direction: asc | desc")
_Q_DATE_BASIS = Query(
    "business_status_date",
    description="Date basis: business_status_date | claim_created_date | ai_record_inserted_date | ai_record_updated_date (all resolve to the AI record's submitted-to-us timestamp except the last, which uses the AI record's last update)",
)


# ---------------------------------------------------------------------------
# Outcome endpoints
# ---------------------------------------------------------------------------

@ai_analytics_router.get(
    "/outcomes/summary",
    response_model=AiOutcomeSummary,
    dependencies=[Depends(get_current_user)],
)
@_handle_route_errors("build outcome summary")
async def outcomes_summary(
    start_date: Optional[str] = _Q_START_DATE,
    end_date: Optional[str] = _Q_END_DATE,
    department_id: Optional[int] = _Q_DEPT_ID,
    business_outcome: Optional[str] = _Q_BUSINESS_OUTCOME,
    ai_processing_status: Optional[str] = _Q_AI_PROCESSING_STATUS,
    agent_execution_status: Optional[str] = _Q_AGENT_EXEC_STATUS,
    confidence_min: Optional[float] = _Q_CONFIDENCE_MIN,
    confidence_max: Optional[float] = _Q_CONFIDENCE_MAX,
    has_retry: Optional[bool] = _Q_HAS_RETRY,
    writeback_status: Optional[str] = _Q_WRITEBACK_STATUS,
    billing_category: Optional[str] = _Q_BILLING_CATEGORY,
    reason_category: Optional[str] = _Q_REASON_CATEGORY,
    date_basis: str = _Q_DATE_BASIS,
    ai_db=Depends(get_ai_db),
):
    """Executive KPI summary for AI invoice outcomes."""
    filters = _build_filters(
        start_date, end_date, department_id, business_outcome,
        ai_processing_status, agent_execution_status,
        confidence_min, confidence_max, has_retry, writeback_status,
        billing_category, reason_category,
        1, 1, "ai_business_updated_at", "desc", date_basis,
    )
    return await get_outcome_summary(ai_db, filters)


@ai_analytics_router.get(
    "/outcomes/funnel",
    response_model=List[AiPipelineStageStat],
    dependencies=[Depends(get_current_user)],
)
@_handle_route_errors("build outcome funnel")
async def outcomes_funnel(
    start_date: Optional[str] = _Q_START_DATE,
    end_date: Optional[str] = _Q_END_DATE,
    department_id: Optional[int] = _Q_DEPT_ID,
    business_outcome: Optional[str] = _Q_BUSINESS_OUTCOME,
    ai_processing_status: Optional[str] = _Q_AI_PROCESSING_STATUS,
    agent_execution_status: Optional[str] = _Q_AGENT_EXEC_STATUS,
    confidence_min: Optional[float] = _Q_CONFIDENCE_MIN,
    confidence_max: Optional[float] = _Q_CONFIDENCE_MAX,
    has_retry: Optional[bool] = _Q_HAS_RETRY,
    writeback_status: Optional[str] = _Q_WRITEBACK_STATUS,
    billing_category: Optional[str] = _Q_BILLING_CATEGORY,
    reason_category: Optional[str] = _Q_REASON_CATEGORY,
    date_basis: str = _Q_DATE_BASIS,
    ai_db=Depends(get_ai_db),
):
    """Pipeline funnel showing stage-by-stage drop-off."""
    filters = _build_filters(
        start_date, end_date, department_id, business_outcome,
        ai_processing_status, agent_execution_status,
        confidence_min, confidence_max, has_retry, writeback_status,
        billing_category, reason_category,
        1, 1, "ai_business_updated_at", "desc", date_basis,
    )
    return await get_outcome_funnel(ai_db, filters)


@ai_analytics_router.get(
    "/outcomes/trend",
    response_model=List[AiOutcomeTrendPoint],
    dependencies=[Depends(get_current_user)],
)
@_handle_route_errors("build outcome trend")
async def outcomes_trend(
    grain: str = Query("day", description="Time grain: day | week | month"),
    start_date: Optional[str] = _Q_START_DATE,
    end_date: Optional[str] = _Q_END_DATE,
    department_id: Optional[int] = _Q_DEPT_ID,
    business_outcome: Optional[str] = _Q_BUSINESS_OUTCOME,
    ai_processing_status: Optional[str] = _Q_AI_PROCESSING_STATUS,
    agent_execution_status: Optional[str] = _Q_AGENT_EXEC_STATUS,
    confidence_min: Optional[float] = _Q_CONFIDENCE_MIN,
    confidence_max: Optional[float] = _Q_CONFIDENCE_MAX,
    has_retry: Optional[bool] = _Q_HAS_RETRY,
    writeback_status: Optional[str] = _Q_WRITEBACK_STATUS,
    billing_category: Optional[str] = _Q_BILLING_CATEGORY,
    reason_category: Optional[str] = _Q_REASON_CATEGORY,
    date_basis: str = _Q_DATE_BASIS,
    ai_db=Depends(get_ai_db),
):
    """Time-series trend of AI invoice outcomes."""
    if grain not in ("day", "week", "month"):
        raise HTTPException(
            status_code=400,
            detail="grain must be one of: day, week, month",
        )
    filters = _build_filters(
        start_date, end_date, department_id, business_outcome,
        ai_processing_status, agent_execution_status,
        confidence_min, confidence_max, has_retry, writeback_status,
        billing_category, reason_category,
        1, 1, "ai_business_updated_at", "desc", date_basis,
    )
    return await get_outcome_trend(ai_db, filters, grain=grain)


@ai_analytics_router.get(
    "/outcomes/rejection-reasons",
    response_model=List[AiRejectionReasonStat],
    dependencies=[Depends(get_current_user)],
)
@_handle_route_errors("build rejection reasons")
async def rejection_reasons(
    start_date: Optional[str] = _Q_START_DATE,
    end_date: Optional[str] = _Q_END_DATE,
    department_id: Optional[int] = _Q_DEPT_ID,
    reason_category: Optional[str] = _Q_REASON_CATEGORY,
    date_basis: str = _Q_DATE_BASIS,
    ai_db=Depends(get_ai_db),
):
    """Rejection reason analytics with normalized categories and drill-down."""
    filters = _build_filters(
        start_date, end_date, department_id, None,
        None, None, None, None, None, None, None, reason_category,
        1, 1, "ai_business_updated_at", "desc", date_basis,
    )
    return await get_rejection_reasons(ai_db, filters)


@ai_analytics_router.get(
    "/outcomes/departments",
    response_model=List[AiDepartmentOutcomeStat],
    dependencies=[Depends(get_current_user)],
)
@_handle_route_errors("build department outcomes")
async def department_outcomes(
    start_date: Optional[str] = _Q_START_DATE,
    end_date: Optional[str] = _Q_END_DATE,
    department_id: Optional[int] = _Q_DEPT_ID,
    business_outcome: Optional[str] = _Q_BUSINESS_OUTCOME,
    ai_processing_status: Optional[str] = _Q_AI_PROCESSING_STATUS,
    agent_execution_status: Optional[str] = _Q_AGENT_EXEC_STATUS,
    confidence_min: Optional[float] = _Q_CONFIDENCE_MIN,
    confidence_max: Optional[float] = _Q_CONFIDENCE_MAX,
    has_retry: Optional[bool] = _Q_HAS_RETRY,
    writeback_status: Optional[str] = _Q_WRITEBACK_STATUS,
    billing_category: Optional[str] = _Q_BILLING_CATEGORY,
    reason_category: Optional[str] = _Q_REASON_CATEGORY,
    date_basis: str = _Q_DATE_BASIS,
    ai_db=Depends(get_ai_db),
):
    """Department comparison table for AI invoice outcomes."""
    filters = _build_filters(
        start_date, end_date, department_id, business_outcome,
        ai_processing_status, agent_execution_status,
        confidence_min, confidence_max, has_retry, writeback_status,
        billing_category, reason_category,
        1, 1, "ai_business_updated_at", "desc", date_basis,
    )
    return await get_department_outcomes(ai_db, filters)


@ai_analytics_router.get(
    "/outcomes/invoices",
    response_model=AiInvoiceCohortResponse,
    dependencies=[Depends(get_current_user)],
)
@_handle_route_errors("build invoice cohort")
async def invoice_cohort(
    start_date: Optional[str] = _Q_START_DATE,
    end_date: Optional[str] = _Q_END_DATE,
    department_id: Optional[int] = _Q_DEPT_ID,
    business_outcome: Optional[str] = _Q_BUSINESS_OUTCOME,
    ai_processing_status: Optional[str] = _Q_AI_PROCESSING_STATUS,
    agent_execution_status: Optional[str] = _Q_AGENT_EXEC_STATUS,
    confidence_min: Optional[float] = _Q_CONFIDENCE_MIN,
    confidence_max: Optional[float] = _Q_CONFIDENCE_MAX,
    has_retry: Optional[bool] = _Q_HAS_RETRY,
    writeback_status: Optional[str] = _Q_WRITEBACK_STATUS,
    billing_category: Optional[str] = _Q_BILLING_CATEGORY,
    reason_category: Optional[str] = _Q_REASON_CATEGORY,
    page: int = _Q_PAGE,
    page_size: int = _Q_PAGE_SIZE,
    sort_by: str = _Q_SORT_BY,
    sort_direction: str = _Q_SORT_DIRECTION,
    date_basis: str = _Q_DATE_BASIS,
    ai_db=Depends(get_ai_db),
):
    """Paged invoice cohort for drill-down analysis."""
    filters = _build_filters(
        start_date, end_date, department_id, business_outcome,
        ai_processing_status, agent_execution_status,
        confidence_min, confidence_max, has_retry, writeback_status,
        billing_category, reason_category,
        page, page_size, sort_by, sort_direction, date_basis,
    )
    return await get_invoice_cohort(ai_db, filters)


# ---------------------------------------------------------------------------
# Billability endpoint (Phase 4)
# ---------------------------------------------------------------------------

@ai_analytics_router.get(
    "/billability/stats",
    response_model=AiBillabilityStat,
    dependencies=[Depends(get_current_user)],
)
@_handle_route_errors("build billability stats")
async def billability_stats(
    start_date: Optional[str] = _Q_START_DATE,
    end_date: Optional[str] = _Q_END_DATE,
    department_id: Optional[int] = _Q_DEPT_ID,
    billing_category: Optional[str] = _Q_BILLING_CATEGORY,
    date_basis: str = _Q_DATE_BASIS,
    ai_db=Depends(get_ai_db),
):
    """Incident/billability evaluation metrics."""
    filters = _build_filters(
        start_date, end_date, department_id, None,
        None, None, None, None, None, None,
        billing_category, None,
        1, 1, "ai_business_updated_at", "desc", date_basis,
    )
    return await get_billability_stats(ai_db, filters)


# ---------------------------------------------------------------------------
# Diagnostics endpoints (Phase 5)
# ---------------------------------------------------------------------------

@ai_analytics_router.get(
    "/diagnostics/summary",
    response_model=AiDiagnosticsSummary,
    dependencies=[Depends(get_current_user)],
)
@_handle_route_errors("build diagnostics summary")
async def diagnostics_summary(
    start_date: Optional[str] = _Q_START_DATE,
    end_date: Optional[str] = _Q_END_DATE,
    department_id: Optional[int] = _Q_DEPT_ID,
    business_outcome: Optional[str] = _Q_BUSINESS_OUTCOME,
    ai_processing_status: Optional[str] = _Q_AI_PROCESSING_STATUS,
    agent_execution_status: Optional[str] = _Q_AGENT_EXEC_STATUS,
    confidence_min: Optional[float] = _Q_CONFIDENCE_MIN,
    confidence_max: Optional[float] = _Q_CONFIDENCE_MAX,
    has_retry: Optional[bool] = _Q_HAS_RETRY,
    writeback_status: Optional[str] = _Q_WRITEBACK_STATUS,
    billing_category: Optional[str] = _Q_BILLING_CATEGORY,
    reason_category: Optional[str] = _Q_REASON_CATEGORY,
    date_basis: str = _Q_DATE_BASIS,
    ai_db=Depends(get_ai_db),
):
    """AI diagnostics health summary."""
    filters = _build_filters(
        start_date, end_date, department_id, business_outcome,
        ai_processing_status, agent_execution_status,
        confidence_min, confidence_max, has_retry, writeback_status,
        billing_category, reason_category,
        1, 1, "ai_business_updated_at", "desc", date_basis,
    )
    return await get_diagnostics_summary(ai_db, filters)


@ai_analytics_router.get(
    "/diagnostics/status",
    dependencies=[Depends(get_current_user)],
)
@_handle_route_errors("build status distribution")
async def diagnostics_status(
    start_date: Optional[str] = _Q_START_DATE,
    end_date: Optional[str] = _Q_END_DATE,
    department_id: Optional[int] = _Q_DEPT_ID,
    business_outcome: Optional[str] = _Q_BUSINESS_OUTCOME,
    ai_processing_status: Optional[str] = _Q_AI_PROCESSING_STATUS,
    agent_execution_status: Optional[str] = _Q_AGENT_EXEC_STATUS,
    date_basis: str = _Q_DATE_BASIS,
    ai_db=Depends(get_ai_db),
):
    """AI processing status distribution."""
    filters = _build_filters(
        start_date, end_date, department_id, business_outcome,
        ai_processing_status, agent_execution_status,
        None, None, None, None, None, None,
        1, 1, "ai_business_updated_at", "desc", date_basis,
    )
    return await get_status_distribution(ai_db, filters)


@ai_analytics_router.get(
    "/diagnostics/confidence",
    response_model=List[AiConfidenceBucketStat],
    dependencies=[Depends(get_current_user)],
)
@_handle_route_errors("build confidence distribution")
async def diagnostics_confidence(
    start_date: Optional[str] = _Q_START_DATE,
    end_date: Optional[str] = _Q_END_DATE,
    department_id: Optional[int] = _Q_DEPT_ID,
    business_outcome: Optional[str] = _Q_BUSINESS_OUTCOME,
    confidence_min: Optional[float] = _Q_CONFIDENCE_MIN,
    confidence_max: Optional[float] = _Q_CONFIDENCE_MAX,
    date_basis: str = _Q_DATE_BASIS,
    ai_db=Depends(get_ai_db),
):
    """Confidence bucket distribution with outcome breakdown."""
    filters = _build_filters(
        start_date, end_date, department_id, business_outcome,
        None, None, confidence_min, confidence_max, None, None, None, None,
        1, 1, "ai_business_updated_at", "desc", date_basis,
    )
    return await get_confidence_distribution(ai_db, filters)


@ai_analytics_router.get(
    "/diagnostics/retries",
    dependencies=[Depends(get_current_user)],
)
@_handle_route_errors("build retry analysis")
async def diagnostics_retries(
    start_date: Optional[str] = _Q_START_DATE,
    end_date: Optional[str] = _Q_END_DATE,
    department_id: Optional[int] = _Q_DEPT_ID,
    date_basis: str = _Q_DATE_BASIS,
    ai_db=Depends(get_ai_db),
):
    """Retry pattern analysis."""
    filters = _build_filters(
        start_date, end_date, department_id, None,
        None, None, None, None, None, None, None, None,
        1, 1, "ai_business_updated_at", "desc", date_basis,
    )
    return await get_retry_analysis(ai_db, filters)


@ai_analytics_router.get(
    "/diagnostics/writeback",
    dependencies=[Depends(get_current_user)],
)
@_handle_route_errors("build writeback analysis")
async def diagnostics_writeback(
    start_date: Optional[str] = _Q_START_DATE,
    end_date: Optional[str] = _Q_END_DATE,
    department_id: Optional[int] = _Q_DEPT_ID,
    writeback_status: Optional[str] = _Q_WRITEBACK_STATUS,
    date_basis: str = _Q_DATE_BASIS,
    ai_db=Depends(get_ai_db),
):
    """Writeback status analysis."""
    filters = _build_filters(
        start_date, end_date, department_id, None,
        None, None, None, None, None, writeback_status, None, None,
        1, 1, "ai_business_updated_at", "desc", date_basis,
    )
    return await get_writeback_analysis(ai_db, filters)


@ai_analytics_router.get(
    "/diagnostics/agents",
    response_model=List[AiAgentStat],
    dependencies=[Depends(get_current_user)],
)
@_handle_route_errors("build agent stats")
async def diagnostics_agents(
    start_date: Optional[str] = _Q_START_DATE,
    end_date: Optional[str] = _Q_END_DATE,
    ai_db=Depends(get_ai_db),
):
    """Agent execution statistics from ai_agent_conversations."""
    filters = _build_filters(
        start_date, end_date, None, None,
        None, None, None, None, None, None, None, None,
        1, 1, "ai_business_updated_at", "desc", "business_status_date",
    )
    return await get_agent_stats(ai_db, filters)


# ---------------------------------------------------------------------------
# Invoice trace endpoint (Phase 7)
# ---------------------------------------------------------------------------

@ai_analytics_router.get(
    "/invoices/{claim_id}/trace",
    response_model=AiInvoiceTrace,
    dependencies=[Depends(get_current_user)],
)
@_handle_route_errors("build invoice trace", context_params=("claim_id",))
async def invoice_trace(
    claim_id: int,
    ai_db=Depends(get_ai_db),
):
    """Forensic per-claim timeline trace."""
    return await get_invoice_trace(ai_db, claim_id)
