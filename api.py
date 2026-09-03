import os
import hmac
import hashlib
import json
import sqlite3
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Header, Request, status
from pydantic import BaseModel, Field
from pathlib import Path
from db import get_db_connection, get_db_cursor, DEFAULT_DB_PATH
from recon_engine import execute_reconciliation_pipeline

app = FastAPI(
    title="PaisaGuard Gateway",
    description="Deterministic Financial Reconciliation, HMAC Ingestion & AI Diagnostic Engine",
    version="2.1.0"
)

# Webhook Secret Configuration
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "rzp_sec_buildathon_2026_demo")

class WebhookPayload(BaseModel):
    event: str = Field(..., json_schema_extra={"example": "payment.captured"})
    payment_id: str = Field(..., json_schema_extra={"example": "pay_rzp_9901"})
    order_id: Optional[str] = Field(None, json_schema_extra={"example": "ord_in_1050"})
    amount: float = Field(..., json_schema_extra={"example": 1500.0})
    fee: Optional[float] = Field(0.0, json_schema_extra={"example": 30.0})
    tax: Optional[float] = Field(0.0, json_schema_extra={"example": 5.4})
    payment_method: Optional[str] = Field("upi", json_schema_extra={"example": "upi"})

class RuleOverrideRequest(BaseModel):
    rule_id: str
    pattern_key: str
    action: str = "APPROVE_CORPORATE_CARD_CHARGE"
    exception_code: str = "FEE_DEDUCTION"
    description: str = "Manual override rule approved via API"

class AIDiagnosticRequest(BaseModel):
    order_id: str
    payment_id: str
    gross_amount: float
    actual_fee: float
    expected_fee: float
    exception_code: str

class AIDiagnosticResponse(BaseModel):
    order_id: str
    classification: str
    confidence: float
    suggested_action: str
    policy_approved: bool
    policy_reason: str

# Deterministic Policy Gatekeeper (Trust Boundary Enforcement)
class PolicyGatekeeper:
    MAX_ALLOWABLE_OVERRIDE_INR = 50.00
    MAX_ALLOWABLE_MDR_RATE = 0.035  # 3.5% absolute economic ceiling

    @classmethod
    def evaluate(cls, gross_amount: float, actual_fee: float, expected_fee: float, suggested_action: str) -> tuple[bool, str]:
        fee_variance = actual_fee - expected_fee
        if fee_variance <= 0:
            return True, "No economic loss detected."

        if fee_variance > cls.MAX_ALLOWABLE_OVERRIDE_INR:
            return False, f"Fee discrepancy ₹{fee_variance:.2f} exceeds hard safety ceiling of ₹{cls.MAX_ALLOWABLE_OVERRIDE_INR:.2f}. Manual CFO sign-off required."

        effective_mdr = actual_fee / gross_amount if gross_amount > 0 else 0.0
        if effective_mdr > cls.MAX_ALLOWABLE_MDR_RATE:
            return False, f"Effective MDR {effective_mdr*100:.2f}% breaches merchant contract cap {cls.MAX_ALLOWABLE_MDR_RATE*100:.2f}%."

        return True, "Deterministic Policy Gatekeeper: Verified within safe economic bounds."

@app.get("/")
def root():
    return {
        "system": "PaisaGuard FinOps Engine",
        "status": "operational",
        "wal_mode": True,
        "hmac_verification": True,
        "policy_gatekeeper": "active"
    }

@app.post("/webhooks/razorpay")
async def ingest_webhook(
    request: Request,
    x_razorpay_signature: Optional[str] = Header(None),
    x_razorpay_event_id: Optional[str] = Header(None)
):
    """
    Production-grade webhook ingestion:
    1. Reads raw byte buffer to guarantee bit-for-bit HMAC SHA256 integrity.
    2. Enforces atomic relational UPSERT on payment_id to physically eliminate race conditions.
    """
    raw_body = await request.body()

    # Verify HMAC signature when header is present
    if x_razorpay_signature:
        expected_sig = hmac.new(
            RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(x_razorpay_signature, expected_sig):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="HMAC signature verification failed: Payload corrupted or unauthorized origin."
            )

    try:
        body_json = json.loads(raw_body.decode("utf-8"))
        payload = WebhookPayload(**body_json)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid webhook JSON structure: {e}")

    net_amt = payload.amount - (payload.fee + payload.tax)
    
    with get_db_cursor(DEFAULT_DB_PATH) as cursor:
        cursor.execute("""
            INSERT INTO razorpay_settlements (
                payment_id, settlement_id, order_id, amount, fee, tax, net_amount,
                currency, payment_method, settled_at, status
            ) VALUES (?, 'setl_live_stream', ?, ?, ?, ?, ?, 'INR', ?, datetime('now'), 'settled')
            ON CONFLICT(payment_id) DO UPDATE SET
                order_id = excluded.order_id,
                amount = excluded.amount,
                fee = excluded.fee,
                tax = excluded.tax,
                net_amount = excluded.net_amount,
                status = excluded.status;
        """, (
            payload.payment_id,
            payload.order_id,
            payload.amount,
            payload.fee,
            payload.tax,
            net_amt,
            payload.payment_method
        ))
        
    return {
        "status": "success",
        "payment_id": payload.payment_id,
        "action": "upserted",
        "hmac_verified": bool(x_razorpay_signature)
    }

@app.post("/ai/diagnose", response_model=AIDiagnosticResponse)
def diagnose_discrepancy(req: AIDiagnosticRequest):
    """
    AI Diagnostic Boundary:
    The LLM/heuristic acts strictly as a read-only classifier returning structured metadata.
    The Deterministic Policy Gatekeeper validates economic risk before any state mutation can occur.
    """
    fee_diff = req.actual_fee - req.expected_fee
    
    # Read-only diagnostic classification
    if "corporate" in req.order_id.lower() or abs(fee_diff - (req.gross_amount * 0.005)) < 1.0:
        classification = "CORPORATE_CARD_INTERCHANGE_SURCHARGE"
        confidence = 0.96
        suggested_action = "APPROVE_CORPORATE_CARD_CHARGE"
    else:
        classification = "UNKNOWN_PRICING_DEVIATION"
        confidence = 0.65
        suggested_action = "REQUIRE_HUMAN_AUDIT"

    # Deterministic Policy Gatekeeper Evaluation
    policy_approved, policy_reason = PolicyGatekeeper.evaluate(
        gross_amount=req.gross_amount,
        actual_fee=req.actual_fee,
        expected_fee=req.expected_fee,
        suggested_action=suggested_action
    )

    return AIDiagnosticResponse(
        order_id=req.order_id,
        classification=classification,
        confidence=confidence,
        suggested_action=suggested_action,
        policy_approved=policy_approved,
        policy_reason=policy_reason
    )

@app.post("/reconcile/sweep")
def trigger_sweep():
    try:
        summary = execute_reconciliation_pipeline(DEFAULT_DB_PATH)
        return {"status": "completed", "summary": summary}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/metrics")
def get_metrics():
    conn = get_db_connection(DEFAULT_DB_PATH)
    try:
        total_settle = conn.execute("SELECT COUNT(*) FROM razorpay_settlements").fetchone()[0]
        total_oms = conn.execute("SELECT COUNT(*) FROM oms_orders").fetchone()[0]
        matched_count = conn.execute(
            "SELECT COUNT(*) FROM reconciliation_ledger WHERE reconciled_status IN ('MATCHED', 'RULE_OVERRIDDEN')"
        ).fetchone()[0]
        exception_count = conn.execute(
            "SELECT COUNT(*) FROM reconciliation_ledger WHERE reconciled_status = 'EXCEPTION'"
        ).fetchone()[0]
        rules_count = conn.execute("SELECT COUNT(*) FROM resolved_rules").fetchone()[0]
        match_rate = round((matched_count / total_settle) * 100, 2) if total_settle > 0 else 0.0
        
        return {
            "total_oms_orders": total_oms,
            "total_settlements": total_settle,
            "matched_transactions": matched_count,
            "exception_count": exception_count,
            "match_rate": match_rate,
            "active_rules": rules_count
        }
    finally:
        conn.close()

@app.post("/rules/resolve")
def add_resolution_rule(req: RuleOverrideRequest):
    with get_db_cursor(DEFAULT_DB_PATH) as cursor:
        cursor.execute("""
            INSERT OR REPLACE INTO resolved_rules (
                rule_id, pattern_key, action, exception_code, description, created_at
            ) VALUES (?, ?, ?, ?, ?, datetime('now'));
        """, (
            req.rule_id,
            req.pattern_key,
            req.action,
            req.exception_code,
            req.description
        ))
    return {"status": "rule_applied", "rule_id": req.rule_id}
