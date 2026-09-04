"""
audit_service.py — FinOps Human Approval & Append-Only Audit Service for PaisaGuard.

Architectural Guarantees:
1. Strictly Append-Only:
   - ZERO SQL UPDATE statements on reconciliation_decisions or financial ledger.
   - All human decisions are recorded as append-only records in human_approvals and audit_events.
2. Current State Projection:
   - The current resolution disposition of any decision is dynamically projected
     from v_current_decisions view (the latest valid human_approvals entry).
"""

from typing import Optional, Dict, Any, Union
from pathlib import Path
from datetime import datetime, timezone

from db import get_db_cursor, get_db_path, log_audit_event

ALLOWED_APPROVAL_ACTIONS = {"APPROVE", "REJECT", "ESCALATE", "OVERRIDE"}


def record_human_approval(
    decision_id: int,
    action: str,
    reviewer: str,
    notes: Optional[str] = None,
    investigation_id: Optional[int] = None,
    db_path: Optional[Union[str, Path]] = None
) -> int:
    """
    Appends a human FinOps approval or rejection to human_approvals and audit_events.
    Strictly preserves immutable history without updating reconciliation_decisions directly.
    """
    target_db = Path(db_path) if db_path else get_db_path()
    clean_action = str(action).upper().strip()

    if clean_action not in ALLOWED_APPROVAL_ACTIONS:
        raise ValueError(f"Invalid approval action '{action}'. Must be one of: {sorted(list(ALLOWED_APPROVAL_ACTIONS))}")

    clean_reviewer = str(reviewer).strip()
    if not clean_reviewer:
        raise ValueError("Reviewer identity is required for auditable human approval sign-off.")

    timestamp = datetime.now(timezone.utc).isoformat()

    with get_db_cursor(target_db) as cursor:
        cursor.execute("""
            INSERT INTO human_approvals (decision_id, investigation_id, action, reviewer, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?);
        """, (decision_id, investigation_id, clean_action, clean_reviewer, notes or "", timestamp))
        approval_id = cursor.lastrowid

    log_audit_event(
        target_db,
        event_type="HUMAN_APPROVAL_RECORDED",
        aggregate_type="DECISION",
        aggregate_id=str(decision_id),
        payload={
            "approval_id": approval_id,
            "decision_id": decision_id,
            "investigation_id": investigation_id,
            "action": clean_action,
            "reviewer": clean_reviewer,
            "notes": notes or ""
        }
    )

    return approval_id
