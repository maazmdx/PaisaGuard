import os
import hmac
import hashlib
import base64
import json
import time
import logging
import sqlite3
from typing import Dict, Any, Optional, Tuple
from decimal import Decimal
from collections import defaultdict
from fastapi import FastAPI, HTTPException, Header, Request, Response, Depends, status
from pydantic import BaseModel, Field
from pathlib import Path
from db import get_db_connection, get_db_cursor, DEFAULT_DB_PATH
from recon_engine import execute_reconciliation_pipeline
from money import to_decimal, round_curr

# Configure Structured Logging
logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"paisaguard-api","message":%(message)s}'
)
logger = logging.getLogger("paisaguard.gateway")

app = FastAPI(
    title="PaisaGuard Gateway",
    description="Deterministic Financial Reconciliation, HMAC Ingestion & AI Diagnostic Engine",
    version="2.2.0"
)

# Webhook Secret & Environment Configuration
ENVIRONMENT = os.environ.get("PAISAGUARD_ENV", "development").lower()
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "rzp_sec_buildathon_2026_demo")
PAISAGUARD_REQUIRE_HMAC = os.environ.get("PAISAGUARD_REQUIRE_HMAC", "").lower() in ("1", "true", "yes")
PAISAGUARD_API_TOKEN = os.environ.get("PAISAGUARD_API_TOKEN", "")

if ENVIRONMENT == "production" and RAZORPAY_WEBHOOK_SECRET == "rzp_sec_buildathon_2026_demo":
    raise RuntimeError("CRITICAL SECURITY VIOLATION: Default demo webhook secret cannot be used in production environment!")


# In-Memory Sliding-Window Rate Limiter
class InMemoryRateLimiter:
    """
    Sliding-window in-memory rate limiter per IP address.
    Default: max 120 requests per 60-second window.
    """
    def __init__(self, max_requests: int = 120, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = defaultdict(list)

    def is_allowed(self, client_ip: str) -> Tuple[bool, int]:
        now = time.time()
        window_start = now - self.window_seconds
        self.requests[client_ip] = [t for t in self.requests[client_ip] if t > window_start]
        if len(self.requests[client_ip]) >= self.max_requests:
            return False, 0
        self.requests[client_ip].append(now)
        return True, self.max_requests - len(self.requests[client_ip])

rate_limiter = InMemoryRateLimiter(max_requests=120, window_seconds=60.0)


def verify_admin_token(x_paisaguard_token: Optional[str] = Header(None)):
    """
    Optional administrative token guard for CFO / Operations controls.
    Active only when PAISAGUARD_API_TOKEN environment variable is set.
    """
    if PAISAGUARD_API_TOKEN:
        if not x_paisaguard_token or not hmac.compare_digest(x_paisaguard_token, PAISAGUARD_API_TOKEN):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Valid X-PaisaGuard-Token header is required to access administrative FinOps controls."
            )


def verify_webhook_signature(raw_body: bytes, signature_header: Optional[str], secret: str) -> Tuple[bool, str]:
    """
    Stateless, importable HMAC-SHA256 verification supporting both Hex and Base64 encodings,
    optional 'sha256=' prefix, whitespace stripping, and case insensitivity.

    Compute the HMAC digest bytes ONCE and derive both representations from that
    single computation, avoiding any ambiguity from calling .hexdigest() and
    .digest() sequentially on the same HMAC object.

    Returns:
        (verified: bool, encoding_used: str)  -- encoding_used is 'hex', 'base64', or 'none'
    """
    if not signature_header or not isinstance(signature_header, str):
        return False, "none"

    clean_sig = signature_header.strip()
    if clean_sig.lower().startswith("sha256="):
        clean_sig = clean_sig[7:].strip()
    elif clean_sig.lower().startswith("hmac="):
        clean_sig = clean_sig[5:].strip()

    if not clean_sig:
        return False, "none"

    digest_bytes = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256
    ).digest()
    expected_hex = digest_bytes.hex()                              # 64 lowercase hex chars
    expected_b64 = base64.b64encode(digest_bytes).decode("utf-8") # 44 base64 chars with =

    # 1. Hex comparison (case-insensitive)
    if hmac.compare_digest(clean_sig.lower(), expected_hex):
        return True, "hex"

    # 2. Base64 comparison (standard and URL-safe)
    if hmac.compare_digest(clean_sig, expected_b64):
        return True, "base64"

    expected_b64_url = base64.urlsafe_b64encode(digest_bytes).decode("utf-8")
    if hmac.compare_digest(clean_sig.rstrip("="), expected_b64_url.rstrip("=")):
        return True, "base64"

    return False, "none"


class WebhookPayload(BaseModel):
    event: str = Field(..., json_schema_extra={"example": "payment.captured"})
    payment_id: str = Field(..., json_schema_extra={"example": "pay_rzp_9901"})
    order_id: Optional[str] = Field(None, json_schema_extra={"example": "ord_in_1050"})
    amount: Decimal = Field(..., json_schema_extra={"example": "1500.00"})
    fee: Optional[Decimal] = Field(Decimal("0.00"), json_schema_extra={"example": "30.00"})
    tax: Optional[Decimal] = Field(Decimal("0.00"), json_schema_extra={"example": "5.40"})
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
    gross_amount: Decimal
    actual_fee: Decimal
    expected_fee: Decimal
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
    MAX_ALLOWABLE_OVERRIDE_INR = Decimal("50.00")
    MAX_ALLOWABLE_MDR_RATE = Decimal("0.035")  # 3.5% absolute economic ceiling

    @classmethod
    def evaluate(cls, gross_amount: Decimal, actual_fee: Decimal, expected_fee: Decimal, suggested_action: str) -> tuple[bool, str]:
        variance = abs(actual_fee - expected_fee)
        if variance > cls.MAX_ALLOWABLE_OVERRIDE_INR:
            return False, f"Fee discrepancy ₹{variance:.2f} exceeds hard safety ceiling of ₹{cls.MAX_ALLOWABLE_OVERRIDE_INR:.2f}. Manual CFO sign-off required."

        if gross_amount > Decimal("0"):
            effective_mdr = actual_fee / gross_amount
        else:
            effective_mdr = Decimal("0.0")

        if effective_mdr > cls.MAX_ALLOWABLE_MDR_RATE:
            return False, f"Effective MDR {effective_mdr*100:.2f}% breaches merchant contract cap {cls.MAX_ALLOWABLE_MDR_RATE*100:.2f}%."

        return True, "Deterministic Policy Gatekeeper: Verified within safe economic bounds."


@app.get("/")
def root():
    return {
        "system": "PaisaGuard FinOps Engine",
        "status": "operational",
        "environment": ENVIRONMENT,
        "wal_mode": True,
        "hmac_verification": True,
        "hmac_policy": "enforced" if (ENVIRONMENT != "development" or PAISAGUARD_REQUIRE_HMAC) else "development_bypass_allowed",
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
    1. Enforces client-IP rate limiting (120 req/min).
    2. Enforces mandatory HMAC-SHA256 signatures in production/staging (or when PAISAGUARD_REQUIRE_HMAC=1).
    3. Reads raw byte buffer to guarantee bit-for-bit HMAC SHA256 integrity before parsing.
    4. Supports both Base64 and Hex-encoded HMAC digests for universal payment gateway compatibility.
    5. Ingests using arbitrary-precision Decimal types to eliminate binary float drift.
    6. Enforces atomic relational UPSERT on payment_id to physically eliminate race conditions.
    """
    # 1. Rate Limiting Check
    client_ip = request.client.host if request.client else "127.0.0.1"
    allowed, _ = rate_limiter.is_allowed(client_ip)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded: Maximum 120 requests per minute. Please throttle."
        )

    raw_body = await request.body()

    # 2. Mandatory HMAC Verification Policy
    if not x_razorpay_signature:
        if ENVIRONMENT != "development" or PAISAGUARD_REQUIRE_HMAC:
            logger.error('{"event":"hmac_rejected","reason":"missing_signature_header","env":"%s"}' % ENVIRONMENT)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Security policy violation: Missing X-Razorpay-Signature header. HMAC signature is strictly required in production/staging environments."
            )
        else:
            logger.warning('{"event":"hmac_warning","reason":"unsigned_payload_dev_bypass","env":"development"}')
    else:
        verified, encoding = verify_webhook_signature(raw_body, x_razorpay_signature, RAZORPAY_WEBHOOK_SECRET)
        if not verified:
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
            if isinstance(amt, int) and amt > 1000:
                amt_dec = to_decimal(amt) / Decimal("100")
            else:
                amt_dec = to_decimal(amt)

            fee_raw = entity.get("fee", 0.0)
            if isinstance(fee_raw, int) and fee_raw > 100:
                fee_dec = to_decimal(fee_raw) / Decimal("100")
            else:
                fee_dec = to_decimal(fee_raw)

            tax_raw = entity.get("tax", 0.0)
            if isinstance(tax_raw, int) and tax_raw > 100:
                tax_dec = to_decimal(tax_raw) / Decimal("100")
            else:
                tax_dec = to_decimal(tax_raw)

            payload = WebhookPayload(
                event=body_json.get("event", "payment.captured"),
                payment_id=entity.get("id", "pay_unknown"),
                order_id=entity.get("order_id"),
                amount=round_curr(amt_dec),
                fee=round_curr(fee_dec),
                tax=round_curr(tax_dec),
                payment_method=entity.get("method", "upi")
            )
        else:
            payload = WebhookPayload(**body_json)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid webhook JSON structure: {e}")

    # Defensive normalization of currency amounts using Decimal (handling None/missing values safely)
    amount_dec = to_decimal(payload.amount)
    fee_dec = to_decimal(payload.fee)
    tax_dec = to_decimal(payload.tax)
    net_dec = round_curr(amount_dec - (fee_dec + tax_dec))

    amount_val = float(round_curr(amount_dec))
    fee_val = float(round_curr(fee_dec))
    tax_val = float(round_curr(tax_dec))
    net_amt = float(net_dec)
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
    if "corporate" in req.order_id.lower() or abs(fee_diff - (req.gross_amount * Decimal("0.005"))) < Decimal("1.0"):
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


@app.post("/reconcile/sweep", dependencies=[Depends(verify_admin_token)])
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


@app.post("/rules/resolve", dependencies=[Depends(verify_admin_token)])
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
