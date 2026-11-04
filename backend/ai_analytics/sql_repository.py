"""Read-only SQL repository for AI Analytics.

All methods are retrieval-only (SELECT). No INSERT/UPDATE/DELETE/MERGE.

Uses the existing ``target_db`` connection infrastructure to query
RecoveryHub Azure SQL.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from target_db import target_db, TargetDatabaseError
from ai_adoption_service import EXCLUDED_DEPARTMENT_IDS

logger = logging.getLogger(__name__)


def _execute_raw_sql(query: str) -> List[Dict[str, Any]]:
    """Execute a raw SELECT query via target_db's connection.

    Bypasses ``execute_read`` because that method auto-injects Claims filters
    and requires a DashboardFilters object. For analytics we need raw queries
    against AIInvoiceProcessRHTemp and other tables without Claims-specific
    rewriting.
    """
    conn = target_db._get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(query)
            rows = cursor.fetchall()
            if cursor.description is None:
                return []
            columns = [desc[0] for desc in cursor.description]
            cleaned = []
            for row in rows:
                d = {}
                for i, col in enumerate(columns):
                    val = row[i]
                    if hasattr(val, "isoformat"):
                        d[col] = val.isoformat()
                    elif val.__class__.__name__ == "Decimal":
                        d[col] = float(val)
                    else:
                        d[col] = val
                cleaned.append(d)
            return cleaned
    finally:
        conn.close()


def _execute_parameterized(query: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Execute a parameterized SELECT query via target_db's connection.

    Handles both pymssql (named params) and pyodbc (positional params).
    """
    conn = target_db._get_connection()
    try:
        with conn.cursor() as cursor:
            target_db._execute_query(cursor, query, params)
            rows = cursor.fetchall()
            if cursor.description is None:
                return []
            columns = [desc[0] for desc in cursor.description]
            cleaned = []
            for row in rows:
                d = {}
                for i, col in enumerate(columns):
                    val = row[i]
                    if hasattr(val, "isoformat"):
                        d[col] = val.isoformat()
                    elif val.__class__.__name__ == "Decimal":
                        d[col] = float(val)
                    else:
                        d[col] = val
                cleaned.append(d)
            return cleaned
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# AI invoice cohort
# ---------------------------------------------------------------------------

_DATE_BASIS_COLUMNS = {
    "business_status_date": "c.date_of_submitted",
    "claim_created_date": "c.created",
    "ai_record_updated_date": "p.updated_time",
}


COHORT_SQL_TEMPLATE = """
SELECT
    p.claim_id,
    p.AI_inv_process_status,
    p.created_date AS ai_business_created_at,
    p.updated_time AS ai_business_updated_at,

    c.dept_id,
    c.run_number,
    c.invoice_number,
    c.amount_invoiced,
    c.status AS recoveryhub_claim_status,
    c.submitted,
    c.original_run_id,
    c.date_of_submitted AS business_status_date,
    c.created AS claim_created_at,
    c.alarm_received,
    c.call_cleared,
    c.run_date,

    d.Name AS department_name,
    d.physical_state AS department_state
FROM dbo.AIInvoiceProcessRHTemp p
INNER JOIN dbo.Claims c ON c.id = p.claim_id
LEFT JOIN dbo.Departments d ON d.ID = c.dept_id
WHERE 1 = 1
  AND (%(start_date)s IS NULL OR {date_column} >= %(start_date)s)
  AND (%(end_date)s IS NULL OR {date_column} < DATEADD(day, 1, %(end_date)s))
  AND (%(department_id)s IS NULL OR c.dept_id = %(department_id)s)
"""


def get_ai_invoice_cohort(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    department_id: Optional[int] = None,
    date_basis: str = "business_status_date",
) -> List[Dict[str, Any]]:
    """Fetch the base AI invoice cohort from SQL.

    Returns one row per AIInvoiceProcessRHTemp record joined to Claims and
    Departments. Does NOT include cancellation details or process logs —
    those are fetched separately in batch.
    """
    try:
        date_column = _DATE_BASIS_COLUMNS[date_basis]
    except KeyError:
        raise ValueError(
            "date_basis must be one of: business_status_date, claim_created_date, ai_record_updated_date"
        )

    params = {
        "start_date": start_date,
        "end_date": end_date,
        "department_id": department_id,
    }
    query = COHORT_SQL_TEMPLATE.format(date_column=date_column)
    try:
        return _execute_parameterized(query, params)
    except TargetDatabaseError as e:
        logger.error(f"Failed to fetch AI invoice cohort: {e}")
        raise


SINGLE_CLAIM_SQL = """
SELECT
    p.claim_id,
    p.AI_inv_process_status,
    p.created_date AS ai_business_created_at,
    p.updated_time AS ai_business_updated_at,

    c.dept_id,
    c.run_number,
    c.invoice_number,
    c.amount_invoiced,
    c.status AS recoveryhub_claim_status,
    c.submitted,
    c.original_run_id,
    c.date_of_submitted,
    c.created AS claim_created_at,
    c.alarm_received,
    c.call_cleared,
    c.run_date,

    d.Name AS department_name,
    d.physical_state AS department_state
FROM dbo.AIInvoiceProcessRHTemp p
INNER JOIN dbo.Claims c ON c.id = p.claim_id
LEFT JOIN dbo.Departments d ON d.ID = c.dept_id
WHERE p.claim_id = %(claim_id)s
"""


def get_ai_invoice_record_for_claim(claim_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a single AI invoice process record by claim_id.

    Returns the same row shape as get_ai_invoice_cohort but for one claim,
    without scanning the entire AIInvoiceProcessRHTemp table.
    Returns None if the claim is not in the AI invoice process table.
    """
    try:
        rows = _execute_parameterized(SINGLE_CLAIM_SQL, {"claim_id": claim_id})
        return rows[0] if rows else None
    except TargetDatabaseError as e:
        logger.error(f"Failed to fetch AI invoice record for claim {claim_id}: {e}")
        raise


CLAIM_LIFECYCLE_SQL = """
SELECT
    c.id,
    c.ClaimCurrentTypeId,
    c.status,
    c.archived
FROM dbo.Claims c
WHERE c.id IN (
    -- CAST is required: for large claim batches pyodbc sends the CSV param
    -- as ntext, which STRING_SPLIT rejects ("Argument data type ntext is
    -- invalid for argument 1"). Without the cast the query fails and the
    -- caller silently gets no lifecycle data.
    SELECT value FROM STRING_SPLIT(CAST(%(claim_ids_csv)s AS nvarchar(max)), ',')
)
"""


def get_claim_lifecycle_for_claims(
    claim_ids: List[int],
) -> Dict[int, Dict[str, Any]]:
    """Fetch the run-lifecycle fields for a batch of claims.

    This is the run record — the same source the Claims Breakdown buckets
    use — so the AI dashboard's "New runs / Active runs" stages reconcile
    exactly with those widgets:
    - ``ClaimCurrentTypeId`` 1 = New Run, 4 = Active Run.
    - ``status`` is the RH claim status (e.g. 'Unassigned' while the AI
      works, 'Payment Received' / 'Denied' / 'Confirm the Receipt of
      Invoice' after release).

    Returns a dict keyed by claim_id →
    ``{"type_id": int|None, "status": str|None, "archived": int|None}``.
    Claims missing from Claims are absent from the dict.
    """
    csv = ",".join(str(int(cid)) for cid in claim_ids)
    try:
        rows = _execute_parameterized(CLAIM_LIFECYCLE_SQL, {"claim_ids_csv": csv})
    except TargetDatabaseError as e:
        logger.error(f"Failed to fetch claim lifecycle: {e}")
        raise
    by_claim: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        try:
            cid = int(row["id"])
        except (ValueError, TypeError):
            continue
        by_claim[cid] = {
            "type_id": row.get("ClaimCurrentTypeId"),
            "status": row.get("status"),
            "archived": row.get("archived"),
        }
    return by_claim


# ---------------------------------------------------------------------------
# Cancellation details
# ---------------------------------------------------------------------------

CANCELLATION_DETAILS_SQL = """
SELECT
    d.id AS cancellation_id,
    d.claim_id,
    d.reason_id,
    r.reason AS raw_reason,
    d.reason_descr AS reason_description,
    d.date_of_cancellation,
    d.created_on
FROM dbo.AIClaimInvoiceCancellationDetails d
LEFT JOIN dbo.AIClaimInvoiceCancellationReasons r
    ON r.id = d.reason_id
WHERE d.claim_id IN (
    -- CAST is required: for large claim batches pyodbc sends the CSV param
    -- as ntext, which STRING_SPLIT rejects ("Argument data type ntext is
    -- invalid for argument 1"). Without the cast the query fails and the
    -- caller silently gets no cancellation data.
    SELECT value FROM STRING_SPLIT(CAST(%(claim_ids_csv)s AS nvarchar(max)), ',')
)
"""


def get_cancellation_details_for_claims(
    claim_ids: List[int],
) -> Dict[int, Dict[str, Any]]:
    """Fetch cancellation details for a batch of claim IDs.

    Returns a dict keyed by claim_id → most recent cancellation record.
    If multiple records exist for one claim, the most recent by created_on
    is used for statistics, but all are preserved in the forensic view.
    """
    if not claim_ids:
        return {}

    # STRING_SPLIT requires a comma-separated string
    claim_ids_csv = ",".join(str(int(cid)) for cid in claim_ids)
    params = {"claim_ids_csv": claim_ids_csv}

    try:
        rows = _execute_parameterized(CANCELLATION_DETAILS_SQL, params)
    except TargetDatabaseError as e:
        logger.error(f"Failed to fetch cancellation details: {e}")
        raise

    # Index by claim_id, keeping the most recent record
    by_claim: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        cid = row.get("claim_id")
        if cid is None:
            continue
        try:
            cid_int = int(cid)
        except (ValueError, TypeError):
            continue
        existing = by_claim.get(cid_int)
        if existing is None:
            by_claim[cid_int] = row
        else:
            existing_created = existing.get("created_on")
            new_created = row.get("created_on")
            if new_created and (not existing_created or new_created > existing_created):
                by_claim[cid_int] = row
    return by_claim


def get_cancellation_details_for_claim(claim_id: int) -> List[Dict[str, Any]]:
    """Fetch ALL cancellation records for a single claim (forensic trace)."""
    query = """
    SELECT
        d.id AS cancellation_id,
        d.claim_id,
        d.reason_id,
        r.reason AS raw_reason,
        d.reason_descr AS reason_description,
        d.date_of_cancellation,
        d.created_on
    FROM dbo.AIClaimInvoiceCancellationDetails d
    LEFT JOIN dbo.AIClaimInvoiceCancellationReasons r
        ON r.id = d.reason_id
    WHERE d.claim_id = %(claim_id)s
    ORDER BY d.created_on DESC
    """
    try:
        return _execute_parameterized(query, {"claim_id": claim_id})
    except TargetDatabaseError as e:
        logger.error(f"Failed to fetch cancellation details for claim {claim_id}: {e}")
        raise


# ---------------------------------------------------------------------------
# Process logs
# ---------------------------------------------------------------------------

def get_process_logs_for_claims(
    claim_ids: List[int],
) -> Dict[int, List[Dict[str, Any]]]:
    """Fetch process logs for a batch of claim IDs.

    Returns a dict keyed by claim_id → list of log entries sorted by
    created_date ascending.
    """
    if not claim_ids:
        return {}

    claim_ids_csv = ",".join(str(int(cid)) for cid in claim_ids)
    query = """
    SELECT
        id,
        claim_id,
        log_text,
        user_id,
        user_type_id,
        created_date
    FROM dbo.ai_claims_process_logs
    WHERE claim_id IN (
        -- CAST is required: for large claim batches pyodbc sends the CSV param
        -- as ntext, which STRING_SPLIT rejects ("Argument data type ntext is
        -- invalid for argument 1"). Without the cast the query fails and the
        -- caller silently gets no process-log data.
        SELECT value FROM STRING_SPLIT(CAST(%(claim_ids_csv)s AS nvarchar(max)), ',')
    )
    ORDER BY claim_id, created_date ASC
    """
    try:
        rows = _execute_parameterized(query, {"claim_ids_csv": claim_ids_csv})
    except TargetDatabaseError as e:
        logger.error(f"Failed to fetch process logs: {e}")
        raise

    by_claim: Dict[int, List[Dict[str, Any]]] = {}
    for row in rows:
        cid = row.get("claim_id")
        if cid is None:
            continue
        try:
            cid_int = int(cid)
        except (ValueError, TypeError):
            continue
        by_claim.setdefault(cid_int, []).append(row)
    return by_claim


def get_process_logs_for_claim(claim_id: int) -> List[Dict[str, Any]]:
    """Fetch ALL process logs for a single claim (forensic trace)."""
    query = """
    SELECT
        id,
        claim_id,
        log_text,
        user_id,
        user_type_id,
        created_date
    FROM dbo.ai_claims_process_logs
    WHERE claim_id = %(claim_id)s
    ORDER BY created_date ASC, id ASC
    """
    try:
        return _execute_parameterized(query, {"claim_id": claim_id})
    except TargetDatabaseError as e:
        logger.error(f"Failed to fetch process logs for claim {claim_id}: {e}")
        raise


# ---------------------------------------------------------------------------
# Final line items
# ---------------------------------------------------------------------------

def get_final_line_items(claim_id: int) -> List[Dict[str, Any]]:
    """Fetch final RecoveryHub claim_services line items for a claim."""
    query = """
    SELECT
        cs.id AS claim_service_id,
        cs.claim_id,
        cs.item,
        cs.rate,
        cs.quantity,
        cs.description,
        cs.[order],
        cs.isFeeFromNew
    FROM dbo.claim_services cs
    WHERE cs.claim_id = %(claim_id)s
    ORDER BY cs.[order], cs.id
    """
    try:
        return _execute_parameterized(query, {"claim_id": claim_id})
    except TargetDatabaseError as e:
        logger.error(f"Failed to fetch final line items for claim {claim_id}: {e}")
        raise


def get_final_resource_line_items(claim_id: int) -> List[Dict[str, Any]]:
    """Fetch Claim_Service_ResourceFeeMapping for a claim's line items."""
    query = """
    SELECT
        m.MappingId,
        m.ClaimServiceId,
        m.FeeId,
        m.Quantity,
        m.Amount,
        m.unit,
        m.resourceLabel
    FROM dbo.Claim_Service_ResourceFeeMapping m
    INNER JOIN dbo.claim_services cs ON cs.id = m.ClaimServiceId
    WHERE cs.claim_id = %(claim_id)s
    ORDER BY m.ClaimServiceId, m.MappingId
    """
    try:
        return _execute_parameterized(query, {"claim_id": claim_id})
    except TargetDatabaseError as e:
        logger.error(f"Failed to fetch resource fee mappings for claim {claim_id}: {e}")
        raise


# ---------------------------------------------------------------------------
# Department lookup
# ---------------------------------------------------------------------------

def get_department_lookup(
    department_ids: Optional[List[int]] = None,
) -> Dict[int, Dict[str, Any]]:
    """Fetch department metadata for a set of department IDs.

    If department_ids is None, returns all active departments.
    """
    if department_ids:
        ids_csv = ",".join(str(int(d)) for d in department_ids)
        query = f"""
        SELECT
            d.ID AS department_id,
            d.Name AS department_name,
            d.physical_state AS state,
            d.active,
            d.deleted,
            d.incidents_billing,
            d.IsSendInvoiceAI
        FROM dbo.Departments d
        WHERE d.ID IN (
            SELECT value FROM STRING_SPLIT('{ids_csv}', ',')
        )
        """
    else:
        query = """
        SELECT
            d.ID AS department_id,
            d.Name AS department_name,
            d.physical_state AS state,
            d.active,
            d.deleted,
            d.incidents_billing,
            d.IsSendInvoiceAI
        FROM dbo.Departments d
        WHERE d.deleted = 0
        ORDER BY d.Name
        """
    try:
        rows = _execute_raw_sql(query)
    except TargetDatabaseError as e:
        logger.error(f"Failed to fetch department lookup: {e}")
        raise

    return {
        int(r["department_id"]): r for r in rows if r.get("department_id") is not None
    }


# ---------------------------------------------------------------------------
# Cancellation reason inventory
# ---------------------------------------------------------------------------

def get_cancellation_reason_inventory() -> List[Dict[str, Any]]:
    """Fetch the full cancellation reason inventory with usage counts."""
    query = """
    SELECT
        r.id AS reason_id,
        r.reason,
        COUNT(d.id) AS usage_count,
        MIN(d.created_on) AS first_used,
        MAX(d.created_on) AS last_used
    FROM dbo.AIClaimInvoiceCancellationReasons r
    LEFT JOIN dbo.AIClaimInvoiceCancellationDetails d
        ON d.reason_id = r.id
    GROUP BY r.id, r.reason
    ORDER BY usage_count DESC, r.id
    """
    try:
        return _execute_raw_sql(query)
    except TargetDatabaseError as e:
        logger.error(f"Failed to fetch cancellation reason inventory: {e}")
        raise


# ---------------------------------------------------------------------------
# Claim payment comparison (read-only)
# ---------------------------------------------------------------------------

def get_server_date() -> str:
    """Current date from SQL Server (ISO string), via target_db's cached clock."""
    return target_db.get_server_date().isoformat()


_EXCLUDED_DEPT_SQL = ",".join(str(int(d)) for d in sorted(EXCLUDED_DEPARTMENT_IDS))

_CLAIM_SCOPE_WHERE = f"""
  c.submitted = 1
  AND c.original_run_id IS NOT NULL
  AND c.date_of_submitted >= %(start_date)s
  AND c.date_of_submitted < DATEADD(day, 1, %(end_date)s)
  AND (%(department_id)s IS NULL OR c.dept_id = %(department_id)s)
  AND (c.dept_id IS NULL OR c.dept_id NOT IN ({_EXCLUDED_DEPT_SQL}))
"""

_row_start_column_cache: Dict[str, str] = {}


def get_table_column_names(table_name: str) -> List[str]:
    """Column names of a dbo table, in ordinal order."""
    query = """
    SELECT c.COLUMN_NAME
    FROM INFORMATION_SCHEMA.COLUMNS c
    WHERE c.TABLE_SCHEMA = 'dbo' AND c.TABLE_NAME = %(table_name)s
    ORDER BY c.ORDINAL_POSITION
    """
    rows = _execute_parameterized(query, {"table_name": table_name})
    return [str(r["COLUMN_NAME"]) for r in rows if r.get("COLUMN_NAME")]


def get_claims_row_start_column() -> str:
    """Name of Claims' AS_ROW_START temporal column, resolved from metadata."""
    if "Claims" in _row_start_column_cache:
        return _row_start_column_cache["Claims"]
    query = """
    SELECT TOP 1 c.name AS column_name
    FROM sys.tables t
    INNER JOIN sys.columns c ON c.object_id = t.object_id
    WHERE t.name = 'Claims'
      AND SCHEMA_NAME(t.schema_id) = 'dbo'
      AND c.generated_always_type_desc = 'AS_ROW_START'
    """
    rows = _execute_raw_sql(query)
    if not rows or not rows[0].get("column_name"):
        raise TargetDatabaseError("Claims has no AS_ROW_START column; cannot read status history.")
    _row_start_column_cache["Claims"] = str(rows[0]["column_name"])
    return _row_start_column_cache["Claims"]


def _quote_identifier(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


_table_columns_cache: Dict[str, List[str]] = {}


def _cached_table_columns(table_name: str) -> List[str]:
    """``get_table_column_names`` with per-process caching (schema never changes mid-run)."""
    if table_name not in _table_columns_cache:
        _table_columns_cache[table_name] = get_table_column_names(table_name)
    return _table_columns_cache[table_name]


def _pick_column(columns: List[str], *candidates: str) -> Optional[str]:
    """First candidate (case-insensitive) that exists in the column list."""
    lowered = {c.lower(): c for c in columns}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return None


def _claim_scope_extra_columns() -> str:
    """Optional Claims columns for the comparison scope — verified at runtime."""
    claims_cols = _cached_table_columns("Claims")
    cols = ""
    for col in ("user_id", "debug"):
        if _pick_column(claims_cols, col):
            cols += f",\n        c.{_quote_identifier(col)}"
    return cols


def get_claim_comparison_scope(
    start_date: str,
    end_date: str,
    department_id: Optional[int],
    row_start_column: str,
    start_utc: Any,
    end_utc: Any,
) -> List[Dict[str, Any]]:
    """Candidate runs for the claim comparison, excluding Rural Metro.

    The payload gate is when a claim entered the line-item process, not
    ``date_of_submitted``. A claim is a candidate when it was submitted inside
    the window OR has any status version (current row or history) starting at
    or after the UTC window start — ``row_start`` values are UTC. The caller
    applies the exact entry-date gate per claim.
    """
    row_start = _quote_identifier(row_start_column)
    query = f"""
    SELECT
        c.id,
        c.dept_id,
        c.run_number,
        c.date_of_submitted,
        c.amount_invoiced,
        c.invoice_number,
        c.status AS current_status,
        d.Name AS department_name,
        d.incidents_billing AS dept_incidents_billing,
        c.type_id,
        rt.type AS type_name{_claim_scope_extra_columns()}
    FROM dbo.Claims c
    LEFT JOIN dbo.Departments d ON d.ID = c.dept_id
    LEFT JOIN dbo.run_types rt ON rt.id = c.type_id
    WHERE c.submitted = 1
      AND c.original_run_id IS NOT NULL
      AND c.date_of_submitted < DATEADD(day, 1, %(end_date)s)
      AND (%(department_id)s IS NULL OR c.dept_id = %(department_id)s)
      AND (c.dept_id IS NULL OR c.dept_id NOT IN ({_EXCLUDED_DEPT_SQL}))
      AND (
        c.date_of_submitted >= %(start_date)s
        OR (c.{row_start} >= %(start_utc)s AND c.{row_start} < %(end_utc)s)
        OR c.id IN (
            SELECT h.id FROM AuditData.claimsHistory h
            WHERE h.{row_start} >= %(start_utc)s AND h.{row_start} < %(end_utc)s
        )
      )
    """
    return _execute_parameterized(
        query,
        {
            "start_date": start_date,
            "end_date": end_date,
            "department_id": department_id,
            "start_utc": start_utc,
            "end_utc": end_utc,
        },
    )


def get_run_type_values(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """Run types with the number of in-scope runs using each (schema check)."""
    query = f"""
    SELECT
        c.type_id,
        rt.type AS type_name,
        COUNT(*) AS claim_count
    FROM dbo.Claims c
    LEFT JOIN dbo.run_types rt ON rt.id = c.type_id
    WHERE {_CLAIM_SCOPE_WHERE}
    GROUP BY c.type_id, rt.type
    ORDER BY claim_count DESC
    """
    return _execute_parameterized(
        query,
        {"start_date": start_date, "end_date": end_date, "department_id": None},
    )


def get_scope_status_counts(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """Current Claims.status values among in-scope runs, with counts (schema check)."""
    query = f"""
    SELECT c.status, COUNT(*) AS claim_count
    FROM dbo.Claims c
    WHERE {_CLAIM_SCOPE_WHERE}
    GROUP BY c.status
    ORDER BY claim_count DESC
    """
    return _execute_parameterized(
        query,
        {"start_date": start_date, "end_date": end_date, "department_id": None},
    )


def get_deleted_runs_count(
    start_date: str,
    end_date: str,
    department_id: Optional[int],
) -> int:
    """Deleted runs submitted in the window (dbo.claims_deleted), excluding Rural Metro.

    Filters ``date_of_submitted`` to match the live scope query, so a run
    created last year and submitted this year is still counted. Uses
    ``created`` when claims_deleted lacks that column — the column is chosen
    once up front from the table's metadata, so a transient query failure can
    never silently swap the metric's date semantics.
    """
    columns = {c.lower() for c in get_table_column_names("claims_deleted")}
    date_column = "date_of_submitted" if "date_of_submitted" in columns else "created"
    query = f"""
    SELECT COUNT(DISTINCT x.id) AS deleted_runs
    FROM dbo.claims_deleted x
    WHERE x.submitted = 1
      AND x.original_run_id IS NOT NULL
      AND x.{date_column} >= %(start_date)s
      AND x.{date_column} < DATEADD(day, 1, %(end_date)s)
      AND (%(department_id)s IS NULL OR x.dept_id = %(department_id)s)
      AND (x.dept_id IS NULL OR x.dept_id NOT IN ({_EXCLUDED_DEPT_SQL}))
    """
    rows = _execute_parameterized(
        query,
        {"start_date": start_date, "end_date": end_date, "department_id": department_id},
    )
    return int(rows[0]["deleted_runs"]) if rows else 0


def get_claim_status_versions(
    claim_ids: List[int],
    row_start_column: str,
    as_of: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Distinct (status) versions per claim across current and history rows.

    History is filtered by claim id only. Never use FOR SYSTEM_TIME here:
    AuditData.claimsHistory is 38.6M+ rows.

    ``as_of`` bounds each current/history row *before* the GROUP BY — without
    it, a status that recurs after the cutoff would collapse to its post-cutoff
    MAX(row_start) and drop out of a past window entirely, hiding the earlier
    in-window occurrence that was the claim's real state at the cutoff.
    """
    if not claim_ids:
        return []
    row_start = _quote_identifier(row_start_column)
    claim_ids_csv = ",".join(str(int(cid)) for cid in claim_ids)
    query = f"""
    SELECT
        x.id,
        x.status,
        MIN(x.row_start) AS first_start,
        MAX(x.row_start) AS last_start,
        MAX(x.is_current) AS is_current
    FROM (
        SELECT c.id, c.status, c.{row_start} AS row_start, CAST(1 AS int) AS is_current
        FROM dbo.Claims c
        WHERE c.id IN (
            SELECT CAST(value AS int) FROM STRING_SPLIT(CAST(%(claim_ids_csv)s AS nvarchar(max)), ',')
        )
          AND (%(as_of)s IS NULL OR c.{row_start} < %(as_of)s)
        UNION ALL
        SELECT h.id, h.status, h.{row_start} AS row_start, CAST(0 AS int) AS is_current
        FROM AuditData.claimsHistory h
        WHERE h.id IN (
            SELECT CAST(value AS int) FROM STRING_SPLIT(CAST(%(claim_ids_csv)s AS nvarchar(max)), ',')
        )
          AND (%(as_of)s IS NULL OR h.{row_start} < %(as_of)s)
    ) x
    GROUP BY x.id, x.status
    """
    return _execute_parameterized(
        query, {"claim_ids_csv": claim_ids_csv, "as_of": as_of}
    )


# ---------------------------------------------------------------------------
# Claim drill-down details
# ---------------------------------------------------------------------------

_NOT_BILLABLE_STATUS_SQL = "'unbillable','non-billable (insufficient information)'"


def _chunked_ids(claim_ids: List[int], size: int = 2000) -> List[List[int]]:
    return [claim_ids[i:i + size] for i in range(0, len(claim_ids), size)]


def get_claim_services_totals(claim_ids: List[int]) -> Dict[int, float]:
    """Summed invoice line-item totals (rate * quantity) per claim."""
    totals: Dict[int, float] = {}
    for chunk in _chunked_ids(claim_ids):
        csv = ",".join(str(int(cid)) for cid in chunk)
        rows = _execute_parameterized(
            """
            SELECT cs.claim_id,
                   SUM(CONVERT(float, cs.rate) * CONVERT(float, cs.quantity)) AS services_total
            FROM dbo.claim_services cs
            WHERE cs.claim_id IN (
                SELECT CAST(value AS int) FROM STRING_SPLIT(CAST(%(ids)s AS nvarchar(max)), ',')
            )
            GROUP BY cs.claim_id
            """,
            {"ids": csv},
        )
        for row in rows:
            if row.get("claim_id") is not None:
                totals[int(row["claim_id"])] = float(row.get("services_total") or 0.0)
    return totals


def get_not_billable_markers(claim_ids: List[int], row_start_column: str) -> Dict[int, Dict[str, Any]]:
    """First 'not billable' version per claim: when it was marked and by whom (user_id).

    ``user_id`` is the processor who changed the status. When claimsHistory
    lacks the column the marker still carries the timestamp.
    """
    history_cols = _cached_table_columns("claimsHistory")
    user_id_col = _pick_column(history_cols or [], "user_id")
    row_start = _quote_identifier(row_start_column)
    user_select = f"h.{_quote_identifier(user_id_col)}" if user_id_col else "CAST(NULL AS int)"
    markers: Dict[int, Dict[str, Any]] = {}
    for chunk in _chunked_ids(claim_ids):
        csv = ",".join(str(int(cid)) for cid in chunk)
        rows = _execute_parameterized(
            f"""
            SELECT h.id, {user_select} AS user_id, h.{row_start} AS marked_at
            FROM AuditData.claimsHistory h
            WHERE LOWER(h.status) IN ({_NOT_BILLABLE_STATUS_SQL})
              AND h.id IN (
                  SELECT CAST(value AS int) FROM STRING_SPLIT(CAST(%(ids)s AS nvarchar(max)), ',')
              )
            ORDER BY h.id, h.{row_start}
            """,
            {"ids": csv},
        )
        for row in rows:
            if row.get("id") is None:
                continue
            cid = int(row["id"])
            if cid not in markers:  # rows arrive ordered by row_start — first wins
                markers[cid] = {
                    "user_id": row.get("user_id"),
                    "marked_at": row.get("marked_at"),
                }
    return markers


_ADJUSTER_TABLE_CANDIDATES = ("adjusters", "Adjusters")


def get_claim_adjusters(claim_ids: List[int]) -> Dict[int, Dict[str, Optional[str]]]:
    """Insurance company and adjuster name per claim from the adjusters table.

    The table's exact column names are resolved from metadata: a claim link
    column, an insurance-company column, and an adjuster-name column. Returns
    an empty map when the table or its claim link cannot be resolved.
    """
    adj_cols: List[str] = []
    for table in _ADJUSTER_TABLE_CANDIDATES:
        adj_cols = _cached_table_columns(table)
        if adj_cols:
            table_name = table
            break
    if not adj_cols:
        return {}
    link_col = _pick_column(adj_cols, "claim_id", "claimid", "claimId", "claim")
    company_col = _pick_column(
        adj_cols,
        "insurance_company", "insurancecompany", "insurance_company_name",
        "insurance_name", "company_name", "company", "carrier", "insurer",
    )
    name_col = _pick_column(
        adj_cols,
        "adjuster_name", "adjustername", "name", "full_name", "fullname", "adjuster",
    )
    if link_col is None or (company_col is None and name_col is None):
        logger.warning(
            f"adjusters table found but claim link/company columns unresolved: {adj_cols}"
        )
        return {}
    selects = [f"a.{_quote_identifier(link_col)} AS claim_id"]
    selects.append(
        f"a.{_quote_identifier(company_col)} AS insurance_company" if company_col
        else "CAST(NULL AS nvarchar) AS insurance_company"
    )
    selects.append(
        f"a.{_quote_identifier(name_col)} AS adjuster_name" if name_col
        else "CAST(NULL AS nvarchar) AS adjuster_name"
    )
    by_claim: Dict[int, Dict[str, Optional[str]]] = {}
    for chunk in _chunked_ids(claim_ids):
        csv = ",".join(str(int(cid)) for cid in chunk)
        rows = _execute_parameterized(
            f"""
            SELECT {", ".join(selects)}
            FROM dbo.{_quote_identifier(table_name)} a
            WHERE a.{_quote_identifier(link_col)} IN (
                SELECT CAST(value AS int) FROM STRING_SPLIT(CAST(%(ids)s AS nvarchar(max)), ',')
            )
            """,
            {"ids": csv},
        )
        for row in rows:
            if row.get("claim_id") is not None:
                by_claim[int(row["claim_id"])] = {
                    "insurance_company": row.get("insurance_company"),
                    "adjuster_name": row.get("adjuster_name"),
                }
    return by_claim
