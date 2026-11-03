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
      AND c.generated_always_type_desc = 'AS_ROW_START'
    """
    rows = _execute_raw_sql(query)
    if not rows or not rows[0].get("column_name"):
        raise TargetDatabaseError("Claims has no AS_ROW_START column; cannot read status history.")
    _row_start_column_cache["Claims"] = str(rows[0]["column_name"])
    return _row_start_column_cache["Claims"]


def _quote_identifier(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def get_claim_comparison_scope(
    start_date: str,
    end_date: str,
    department_id: Optional[int],
) -> List[Dict[str, Any]]:
    """In-scope runs submitted in the window, excluding Rural Metro departments."""
    query = f"""
    SELECT
        c.id,
        c.dept_id,
        c.run_number,
        c.date_of_submitted,
        d.Name AS department_name,
        c.type_id,
        rt.type AS type_name
    FROM dbo.Claims c
    LEFT JOIN dbo.Departments d ON d.ID = c.dept_id
    LEFT JOIN dbo.run_types rt ON rt.id = c.type_id
    WHERE {_CLAIM_SCOPE_WHERE}
    """
    return _execute_parameterized(
        query,
        {"start_date": start_date, "end_date": end_date, "department_id": department_id},
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
    """Deleted runs created in the window (dbo.claims_deleted), excluding Rural Metro.

    ``dept_id`` on claims_deleted is not verified against production; the
    schema-check endpoint reports the table's columns.
    """
    query = f"""
    SELECT COUNT(DISTINCT x.id) AS deleted_runs
    FROM dbo.claims_deleted x
    WHERE x.submitted = 1
      AND x.original_run_id IS NOT NULL
      AND x.created >= %(start_date)s
      AND x.created < DATEADD(day, 1, %(end_date)s)
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
) -> List[Dict[str, Any]]:
    """Distinct (status) versions per claim across current and history rows.

    History is filtered by claim id only. Never use FOR SYSTEM_TIME here:
    AuditData.claimsHistory is 38.6M+ rows.
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
        UNION ALL
        SELECT h.id, h.status, h.{row_start} AS row_start, CAST(0 AS int) AS is_current
        FROM AuditData.claimsHistory h
        WHERE h.id IN (
            SELECT CAST(value AS int) FROM STRING_SPLIT(CAST(%(claim_ids_csv)s AS nvarchar(max)), ',')
        )
    ) x
    GROUP BY x.id, x.status
    """
    return _execute_parameterized(query, {"claim_ids_csv": claim_ids_csv})
