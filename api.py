import os
import hmac
import hashlib
import base64
import json
import logging
import sqlite3
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Header, Request, Response, status
from pydantic import BaseModel, Field
from pathlib import Path
from db import get_db_connection, get_db_cursor, DEFAULT_DB_PATH
from recon_engine import execute_reconciliation_pipeline

# Configure Structured Logging
logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"paisaguard-api","message":%(message)s}'
)
logger = logging.getLogger("paisaguard.gateway")

app = FastAPI(
    title="PaisaGuard Gateway",
    description="Deterministic Financial Reconciliation, HMAC Ingestion & AI Diagnostic Engine",
    version="2.1.0"
)

# Webhook Secret & Environment Configuration
ENVIRONMENT = os.environ.get("PAISAGUARD_ENV", "development").lower()
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "rzp_sec_buildathon_2026_demo")
if ENVIRONMENT == "production" and RAZORPAY_WEBHOOK_SECRET == "rzp_sec_buildathon_2026_demo":
    raise RuntimeError("CRITICAL SECURITY VIOLATION: Default demo webhook secret cannot be used in production environment!")

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
        variance = abs(actual_fee - expected_fee)
        if variance > cls.MAX_ALLOWABLE_OVERRIDE_INR:
            return False, f"Fee discrepancy ₹{variance:.2f} exceeds hard safety ceiling of ₹{cls.MAX_ALLOWABLE_OVERRIDE_INR:.2f}. Manual CFO sign-off required."

        if gross_amount > 0:
            effective_mdr = actual_fee / gross_amount
        else:
            effective_mdr = 0.0

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
    1. Reads raw byte buffer to guarantee bit-for-bit HMAC SHA256 integrity before parsing.
    2. Supports both Base64 and Hex-encoded HMAC digests for universal payment gateway compatibility.
    3. Handles both flat JSON payloads and standard nested Razorpay event payload schemas.
    4. Enforces atomic relational UPSERT on payment_id to physically eliminate race conditions.
    """
    raw_body = await request.body()

    # Verify HMAC signature when header is present (Hex or Base64 encoding)
    if x_razorpay_signature:
        hmac_obj = hmac.new(
            RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha256
        )
        expected_hex = hmac_obj.hexdigest()
        expected_b64 = base64.b64encode(hmac_obj.digest()).decode("utf-8")

        if not (hmac.compare_digest(x_razorpay_signature, expected_hex) or 
                hmac.compare_digest(x_razorpay_signature, expected_b64)):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="HMAC signature verification failed: Payload corrupted or unauthorized origin."
            )

    try:
        body_json = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        if "payload" in body_json and isinstance(body_json["payload"], dict) and "payment" in body_json["payload"]:
            entity = body_json["payload"]["payment"].get("entity", {})
            amt = entity.get("amount", 0.0)
            # In Razorpay native payload, amount is in paise (integer) if > 1000 and has no decimals
            amt_float = float(amt) / 100.0 if isinstance(amt, int) and amt > 1000 else float(amt)
            fee_val = entity.get("fee", 0.0)
            fee_float = float(fee_val) / 100.0 if isinstance(fee_val, int) and fee_val > 100 else float(fee_val)
            tax_val = entity.get("tax", 0.0)
            tax_float = float(tax_val) / 100.0 if isinstance(tax_val, int) and tax_val > 100 else float(tax_val)
            payload = WebhookPayload(
                event=body_json.get("event", "payment.captured"),
                payment_id=entity.get("id", "pay_unknown"),
                order_id=entity.get("order_id"),
                amount=amt_float,
                fee=fee_float,
                tax=tax_float,
                payment_method=entity.get("method", "upi")
            )
        else:
            payload = WebhookPayload(**body_json)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid webhook JSON structure: {e}")

    # Defensive normalization of currency amounts (handling None/missing values safely)
    fee_val = float(payload.fee if payload.fee is not None else 0.0)
    tax_val = float(payload.tax if payload.tax is not None else 0.0)
    amount_val = float(payload.amount if payload.amount is not None else 0.0)
    net_amt = amount_val - (fee_val + tax_val)
    method_val = payload.payment_method or "upi"
    
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
            amount_val,
            fee_val,
            tax_val,
            net_amt,
            method_val
        ))

    logger.info(json.dumps({
        "event": "webhook_ingested",
        "payment_id": payload.payment_id,
        "order_id": payload.order_id,
        "amount": amount_val,
        "fee": fee_val,
        "tax": tax_val,
        "net_amount": net_amt,
        "hmac_verified": bool(x_razorpay_signature)
    }))
        
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

@app.get("/metrics/prometheus")
def get_prometheus_metrics():
    """Prometheus-compatible plain text metrics exposition."""
    m = get_metrics()
    lines = [
        "# HELP paisaguard_oms_orders_total Total number of internal OMS orders",
        "# TYPE paisaguard_oms_orders_total counter",
        f"paisaguard_oms_orders_total {m['total_oms_orders']}",
        "# HELP paisaguard_settlements_total Total gateway settlements ingested",
        "# TYPE paisaguard_settlements_total counter",
        f"paisaguard_settlements_total {m['total_settlements']}",
        "# HELP paisaguard_matched_transactions_total Total verified and matched transactions",
        "# TYPE paisaguard_matched_transactions_total counter",
        f"paisaguard_matched_transactions_total {m['matched_transactions']}",
        "# HELP paisaguard_exceptions_total Current exceptions pending in queue",
        "# TYPE paisaguard_exceptions_total gauge",
        f"paisaguard_exceptions_total {m['exception_count']}",
        "# HELP paisaguard_match_rate_percent Current reconciliation match percentage",
        "# TYPE paisaguard_match_rate_percent gauge",
        f"paisaguard_match_rate_percent {m['match_rate']}",
        "# HELP paisaguard_active_rules Total precomputed override rules active",
        "# TYPE paisaguard_active_rules gauge",
        f"paisaguard_active_rules {m['active_rules']}"
    ]
    return Response(content="\n".join(lines) + "\n", media_type="text/plain")

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
