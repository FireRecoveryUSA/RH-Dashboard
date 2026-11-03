"""Editable vocabulary for the Claim Payment Comparison dashboard.

Single source of truth for:
- how raw ``Claims.status`` values map onto final-payment buckets,
- which status names mark a run as direct-billed or insurance-billed,
- how ``Claims.type_id`` maps onto incident type labels.

Status names are matched exactly, case-insensitively, after trimming. The
mapping was settled from read-only discovery against production (distinct
``Claims.status`` values for runs since Jan 1). Any status not listed here
falls back to ``in_progress`` and is reported by the schema-check endpoint
as unmapped. Edit this module only.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, Optional, Tuple

BUCKET_ARCHIVE = "archive"
BUCKET_NOT_BILLABLE = "not_billable"
BUCKET_DENIED = "denied"
BUCKET_PAID = "paid"
BUCKET_READY_TO_BE_PAID = "ready_to_be_paid"
BUCKET_UNDER_REVIEW = "under_review"
BUCKET_IN_PROGRESS = "in_progress"

# Buckets that participate in payment-rate denominators (not_billable,
# archive and deleted claims are reported separately).
RATED_BUCKETS = (
    BUCKET_PAID,
    BUCKET_READY_TO_BE_PAID,
    BUCKET_DENIED,
    BUCKET_UNDER_REVIEW,
    BUCKET_IN_PROGRESS,
)

# Exact (lower-cased) status name -> bucket. Anything absent falls back to
# in_progress. The fallback names are listed too so schema-check reports them
# as mapped rather than unmapped.
STATUS_BUCKETS: Dict[str, str] = {
    # archive (ignored entirely)
    "review for archives": BUCKET_ARCHIVE,
    "archived per fd": BUCKET_ARCHIVE,
    "archived payment plan": BUCKET_ARCHIVE,
    # final payment
    "payment received": BUCKET_PAID,
    "payment approved": BUCKET_READY_TO_BE_PAID,
    "confirm the receipt of invoice": BUCKET_READY_TO_BE_PAID,
    "denied": BUCKET_DENIED,
    "denial letter review": BUCKET_DENIED,
    "under review": BUCKET_UNDER_REVIEW,
    "unbillable": BUCKET_NOT_BILLABLE,
    "non-billable (insufficient information)": BUCKET_NOT_BILLABLE,
    # everything else that is not final -> in_progress
    "sent to collections": BUCKET_IN_PROGRESS,
    "on hold": BUCKET_IN_PROGRESS,
    "invoiced direct": BUCKET_IN_PROGRESS,
    "invoiced insurance": BUCKET_IN_PROGRESS,
    "follow up": BUCKET_IN_PROGRESS,
    "resident": BUCKET_IN_PROGRESS,
    "requesting information": BUCKET_IN_PROGRESS,
    "unassigned": BUCKET_IN_PROGRESS,
    "assigned": BUCKET_IN_PROGRESS,
    "payment plan": BUCKET_IN_PROGRESS,
    "ready to invoice insurance": BUCKET_IN_PROGRESS,
    "ready to invoice direct": BUCKET_IN_PROGRESS,
    "ready for collections": BUCKET_IN_PROGRESS,
    "rm pending action": BUCKET_IN_PROGRESS,
    "false alarms": BUCKET_IN_PROGRESS,
    "false alarm below threshold": BUCKET_IN_PROGRESS,
    "invalid address": BUCKET_IN_PROGRESS,
    "law office": BUCKET_IN_PROGRESS,
    "uncollectable by law office": BUCKET_IN_PROGRESS,
}

# Statuses that place a run in a billing target. Latest occurrence wins when
# both appear; otherwise the billing target is "unknown".
DIRECT_BILLING_STATUSES: FrozenSet[str] = frozenset({
    "invoiced direct",
    "ready to invoice direct",
})
INSURANCE_BILLING_STATUSES: FrozenSet[str] = frozenset({
    "invoiced insurance",
    "ready to invoice insurance",
})

BILLING_TARGET_DIRECT = "direct"
BILLING_TARGET_INSURANCE = "insurance"
BILLING_TARGET_UNKNOWN = "unknown"

BILLING_TARGET_LABELS: Dict[str, str] = {
    BILLING_TARGET_DIRECT: "Direct",
    BILLING_TARGET_INSURANCE: "Insurance",
    BILLING_TARGET_UNKNOWN: "Unknown",
}

UNSPECIFIED_INCIDENT_LABEL = "Unspecified"


def _normalize(status: Optional[str]) -> Optional[str]:
    if status is None:
        return None
    text = status.strip().lower()
    return text or None


def is_mapped_claim_status(status: Optional[str]) -> bool:
    """True when the status name has an explicit entry in STATUS_BUCKETS."""
    name = _normalize(status)
    return name is not None and name in STATUS_BUCKETS


def classify_claim_status(status: Optional[str]) -> str:
    """Return the bucket for a raw ``Claims.status`` value.

    Unlisted statuses fall back to ``in_progress`` so they still count against
    the payment-rate denominators. Use ``is_mapped_claim_status`` to detect them.
    """
    name = _normalize(status)
    if name is None:
        return BUCKET_IN_PROGRESS
    return STATUS_BUCKETS.get(name, BUCKET_IN_PROGRESS)


def billing_target_from_statuses(
    versions: Iterable[Tuple[Optional[str], object]],
) -> str:
    """Billing target from (status, last_start) pairs for one claim.

    The direct or insurance status with the latest last_start wins. Without
    either, the target is unknown. ``last_start`` only needs to be orderable.
    """
    latest_direct = None
    latest_insurance = None
    for status, last_start in versions:
        name = _normalize(status)
        if name in DIRECT_BILLING_STATUSES:
            if latest_direct is None or last_start > latest_direct:
                latest_direct = last_start
        elif name in INSURANCE_BILLING_STATUSES:
            if latest_insurance is None or last_start > latest_insurance:
                latest_insurance = last_start
    if latest_direct is None and latest_insurance is None:
        return BILLING_TARGET_UNKNOWN
    if latest_insurance is None:
        return BILLING_TARGET_DIRECT
    if latest_direct is None:
        return BILLING_TARGET_INSURANCE
    return BILLING_TARGET_DIRECT if latest_direct > latest_insurance else BILLING_TARGET_INSURANCE


def billing_target_label(target: str) -> str:
    return BILLING_TARGET_LABELS.get(target, BILLING_TARGET_LABELS[BILLING_TARGET_UNKNOWN])


def incident_type_label(type_id: Optional[int], type_name: Optional[str]) -> str:
    """Label for a run's incident type from its ``run_types`` row.

    NULL type_id is "Unspecified". A type_id with no run_types row is "Type {id}".
    """
    if type_id is None:
        return UNSPECIFIED_INCIDENT_LABEL
    if type_name:
        return str(type_name).strip()
    return f"Type {type_id}"
