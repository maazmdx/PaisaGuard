"""
api.py — FastAPI FinOps Webhook Gateway, Reconcile Controller & AI Diagnosis API.

Architectural Guarantees:
1. Fail-Closed Token Security:
   - PAISAGUARD_API_TOKEN is required for state-changing endpoints (/reconcile/sweep, /ai/investigate, /approvals/decision).
   - If PAISAGUARD_API_TOKEN is unset, all state mutations fail closed (HTTP 503).
   - Validated via constant-time hmac.compare_digest.
2. Canonical Currency:
   - Webhook payloads strictly require integer `*_paise` fields (e.g. amount_paise: int).
3. Webhook Deduplication:
   - Deduplicated by unique event_id in webhook_events. Replay of identical event_id returns duplicate_ignored.
4. HMAC-SHA256 Signature Policy:
   - Enforced by default in all environments.
   - Unsigned requests permitted ONLY if PAISAGUARD_DEMO_ALLOW_UNSIGNED_WEBHOOKS=true.
5. Zero Secret Logging:
   - Secrets, tokens, and prefixes are NEVER printed or logged.
6. Isolated Dashboard API:
   - Exposes clean read endpoints (/metrics, /payouts, /exceptions, /audit-events, /evaluation-report)
     allowing the Streamlit dashboard to operate with zero direct SQLite volume access.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional, Tuple

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "out"
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from audit_service import record_human_approval
from db import get_db_connection, get_db_cursor
from exception_agent import investigate_exception
from money import format_paise_inr, require_paise
from recon_engine import execute_reconciliation_pipeline

# Configure Structured JSON Logging without secret leakage
logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"paisaguard-api","message":%(message)s}',
)
logger = logging.getLogger("paisaguard.gateway")

app = FastAPI(
    title="PaisaGuard FinOps Engine",
    description="3-Source Financial Reconciliation, HMAC Ingestion & AI Diagnostic Controller",
    version="3.0.0",
)

# Security Configuration
ENVIRONMENT = os.environ.get("PAISAGUARD_ENV", "development").lower()
PAISAGUARD_API_TOKEN = os.environ.get("PAISAGUARD_API_TOKEN", "")
PAISAGUARD_DEMO_ALLOW_UNSIGNED = os.environ.get("PAISAGUARD_DEMO_ALLOW_UNSIGNED_WEBHOOKS", "false").lower() in (
    "1",
    "true",
    "yes",
)
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")


# Sliding-Window Rate Limiter
class InMemoryRateLimiter:
    def __init__(self, max_requests: int = 200, window_seconds: float = 60.0):
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


rate_limiter = InMemoryRateLimiter(max_requests=200, window_seconds=60.0)


def verify_api_token(x_paisaguard_token: Optional[str] = Header(None)):
    """
    Fail-closed token verification for state-changing FinOps endpoints.
    If PAISAGUARD_API_TOKEN is unset on the server, all calls fail closed (HTTP 503).
    """
    expected_token = os.environ.get("PAISAGUARD_API_TOKEN", PAISAGUARD_API_TOKEN)
    if not expected_token:
        logger.error('{"event":"auth_rejection","reason":"server_token_unset"}')
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FinOps Security Policy: Server PAISAGUARD_API_TOKEN is not configured. Protected write actions are disabled.",
        )

    if not x_paisaguard_token or not hmac.compare_digest(x_paisaguard_token, expected_token):
        logger.warning('{"event":"auth_rejection","reason":"invalid_or_missing_token"}')
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Valid X-PaisaGuard-Token header is required for this operation.",
        )


def verify_webhook_signature(raw_body: bytes, signature_header: Optional[str], secret: str) -> Tuple[bool, str]:
    """
    Constant-time HMAC-SHA256 verification supporting Hex and Base64 encodings.
    """
    if not signature_header or not secret:
        return False, "none"

    clean_sig = signature_header.strip()
    if clean_sig.lower().startswith("sha256="):
        clean_sig = clean_sig[7:].strip()
    elif clean_sig.lower().startswith("hmac="):
        clean_sig = clean_sig[5:].strip()

    if not clean_sig:
        return False, "none"

    digest_bytes = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected_hex = digest_bytes.hex()
    expected_b64 = base64.b64encode(digest_bytes).decode("utf-8")

    # 1. Hex comparison (case-insensitive)
    if hmac.compare_digest(clean_sig.lower(), expected_hex):
        return True, "hex"

    # 2. Base64 comparison (standard & url-safe)
    if hmac.compare_digest(clean_sig, expected_b64):
        return True, "base64"

    expected_b64_url = base64.urlsafe_b64encode(digest_bytes).decode("utf-8")
    if hmac.compare_digest(clean_sig.rstrip("="), expected_b64_url.rstrip("=")):
        return True, "base64"

    return False, "none"


# Pydantic Request Models with Strict Integer Paise
class RazorpayWebhookPayload(BaseModel):
    event_id: str = Field(..., json_schema_extra={"example": "evt_rzp_9901"})
    event: str = Field("payment.captured", json_schema_extra={"example": "payment.captured"})
    payment_id: str = Field(..., json_schema_extra={"example": "pay_rzp_9901"})
    order_id: Optional[str] = Field(None, json_schema_extra={"example": "ord_in_1050"})
    payout_id: Optional[str] = Field(None, json_schema_extra={"example": "payout_aug_01"})
    amount_paise: int = Field(
        ..., description="Payment amount in canonical integer paise", json_schema_extra={"example": 150000}
    )
    fee_paise: Optional[int] = Field(
        0, description="Deducted gateway fee in integer paise", json_schema_extra={"example": 3000}
    )
    tax_paise: Optional[int] = Field(
        0, description="Deducted GST tax in integer paise", json_schema_extra={"example": 540}
    )
    payment_method: Optional[str] = Field("upi", json_schema_extra={"example": "upi"})


class AIInvestigateRequest(BaseModel):
    decision_id: int = Field(..., description="ID of reconciliation decision to investigate")


class HumanApprovalRequest(BaseModel):
    decision_id: int = Field(..., description="ID of reconciliation decision being resolved")
    action: str = Field(..., description="Action: APPROVE, REJECT, ESCALATE, or OVERRIDE")
    reviewer: str = Field(..., description="Reviewer name or employee identity")
    notes: Optional[str] = Field("", description="Audit notes explaining disposition rationale")
    investigation_id: Optional[int] = Field(None, description="Optional associated agent investigation ID")


@app.get("/")
def root():
    has_token = bool(os.environ.get("PAISAGUARD_API_TOKEN", PAISAGUARD_API_TOKEN))
    has_secret = bool(os.environ.get("RAZORPAY_WEBHOOK_SECRET", RAZORPAY_WEBHOOK_SECRET))
    return {
        "system": "PaisaGuard FinOps Engine",
        "status": "operational",
        "version": "3.0.0",
        "environment": ENVIRONMENT,
        "canonical_currency": "integer_paise",
        "security_policy": {
            "token_configured": has_token,
            "hmac_secret_configured": has_secret,
            "demo_unsigned_allowed": PAISAGUARD_DEMO_ALLOW_UNSIGNED,
        },
    }


@app.post("/webhooks/razorpay")
async def ingest_webhook(
    request: Request,
    x_razorpay_signature: Optional[str] = Header(None),
    x_razorpay_event_id: Optional[str] = Header(None),
):
    """
    Production-grade webhook ingestion:
    1. Client-IP rate limiting.
    2. Mandatory HMAC-SHA256 signature verification (unless demo bypass flag is active).
    3. Idempotent deduplication by event_id in webhook_events.
    4. Canonical integer paise insertion.
    """
    client_ip = request.client.host if request.client else "127.0.0.1"
    allowed, _ = rate_limiter.is_allowed(client_ip)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded: 200 requests/min ceiling."
        )

    raw_body = await request.body()
    webhook_secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", RAZORPAY_WEBHOOK_SECRET)

    # HMAC Signature Policy
    if not x_razorpay_signature:
        if not PAISAGUARD_DEMO_ALLOW_UNSIGNED:
            logger.error('{"event":"hmac_rejected","reason":"missing_signature"}')
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Security Violation: Missing X-Razorpay-Signature header. Unsigned webhooks are rejected by default.",
            )
        else:
            logger.warning('{"event":"hmac_notice","reason":"unsigned_payload_permitted_under_demo_flag"}')
    else:
        if not webhook_secret:
            logger.error('{"event":"hmac_rejected","reason":"server_webhook_secret_missing"}')
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="FinOps Security Policy: RAZORPAY_WEBHOOK_SECRET is not configured on server.",
            )
        verified, _ = verify_webhook_signature(raw_body, x_razorpay_signature, webhook_secret)
        if not verified:
            logger.error('{"event":"hmac_rejected","reason":"invalid_signature"}')
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="HMAC verification failed: Signature does not match payload content.",
            )

    # Parse and Validate Payload
    try:
        body_json = json.loads(raw_body.decode("utf-8")) if raw_body else {}

        # Support native Razorpay nested entity structure or flat payload
        if "payload" in body_json and "payment" in body_json["payload"]:
            entity = body_json["payload"]["payment"].get("entity", {})
            event_id = x_razorpay_event_id or body_json.get("event_id") or f"evt_{entity.get('id')}"
            payload = RazorpayWebhookPayload(
                event_id=event_id,
                event=body_json.get("event", "payment.captured"),
                payment_id=entity.get("id"),
                order_id=entity.get("order_id"),
                payout_id=entity.get("payout_id"),
                amount_paise=require_paise(int(entity.get("amount", 0))),
                fee_paise=require_paise(int(entity.get("fee", 0))),
                tax_paise=require_paise(int(entity.get("tax", 0))),
                payment_method=entity.get("method", "upi"),
            )
        else:
            if x_razorpay_event_id and "event_id" not in body_json:
                body_json["event_id"] = x_razorpay_event_id
            payload = RazorpayWebhookPayload(**body_json)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid webhook JSON structure: {exc}")

    # Compute source payload hash
    source_payload_hash = f"sha256:{hashlib.sha256(raw_body).hexdigest()}"
    net_paise = payload.amount_paise - (payload.fee_paise + payload.tax_paise)
    now_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    with get_db_cursor() as cursor:
        # Deduplication Check
        cursor.execute("SELECT event_id FROM webhook_events WHERE event_id = ?", (payload.event_id,))
        if cursor.fetchone():
            logger.info('{"event":"webhook_duplicate_ignored","event_id":"%s"}' % payload.event_id)
            return {
                "status": "duplicate_ignored",
                "event_id": payload.event_id,
                "payment_id": payload.payment_id,
                "message": "Event ID already recorded; deduplicated without state mutation.",
            }

        # Record in webhook_events
        cursor.execute(
            """
            INSERT INTO webhook_events (
                event_id, payment_id, source_payload_hash, payload_json, hmac_signature, received_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'processed');
        """,
            (
                payload.event_id,
                payload.payment_id,
                source_payload_hash,
                json.dumps(payload.model_dump()),
                x_razorpay_signature or "none",
                now_ts,
            ),
        )

        # Atomic Upsert into razorpay_settlements
        cursor.execute(
            """
            INSERT INTO razorpay_settlements (
                payment_id, business_tx_id, order_id, payout_id, amount_paise,
                fee_paise, tax_paise, net_paise, currency, payment_method,
                source_payload_hash, settled_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'INR', ?, ?, ?, 'settled')
            ON CONFLICT(payment_id) DO UPDATE SET
                order_id = excluded.order_id,
                payout_id = excluded.payout_id,
                amount_paise = excluded.amount_paise,
                fee_paise = excluded.fee_paise,
                tax_paise = excluded.tax_paise,
                net_paise = excluded.net_paise,
                status = excluded.status;
        """,
            (
                payload.payment_id,
                f"tx_live_{payload.payment_id}",
                payload.order_id,
                payload.payout_id,
                payload.amount_paise,
                payload.fee_paise,
                payload.tax_paise,
                net_paise,
                payload.payment_method or "upi",
                source_payload_hash,
                now_ts,
            ),
        )

    logger.info(
        '{"event":"webhook_ingested","event_id":"%s","payment_id":"%s","amount_paise":%d}'
        % (payload.event_id, payload.payment_id, payload.amount_paise)
    )

    return {
        "status": "ingested",
        "event_id": payload.event_id,
        "payment_id": payload.payment_id,
        "amount_paise": payload.amount_paise,
        "hmac_verified": bool(x_razorpay_signature),
    }


# Protected State-Changing FinOps Endpoints
@app.post("/reconcile/sweep", dependencies=[Depends(verify_api_token)])
def trigger_reconciliation_sweep():
    """Triggers the deterministic 3-source reconciliation pipeline."""
    try:
        summary = execute_reconciliation_pipeline()
        return {"status": "completed", "summary": summary}
    except Exception as exc:
        logger.error(f"Reconciliation sweep failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/ai/investigate", dependencies=[Depends(verify_api_token)])
def trigger_ai_investigation(req: AIInvestigateRequest):
    """Invokes AI exception investigation agent on an unresolved decision."""
    try:
        result = investigate_exception(decision_id=req.decision_id)
        return {"status": "investigated", "result": result}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error(f"AI investigation error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/approvals/decision", dependencies=[Depends(verify_api_token)])
def submit_human_approval(req: HumanApprovalRequest):
    """Records an append-only human FinOps approval or rejection."""
    try:
        approval_id = record_human_approval(
            decision_id=req.decision_id,
            action=req.action,
            reviewer=req.reviewer,
            notes=req.notes,
            investigation_id=req.investigation_id,
        )
        return {
            "status": "approval_recorded",
            "approval_id": approval_id,
            "decision_id": req.decision_id,
            "action": req.action,
            "reviewer": req.reviewer,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(f"Human approval error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# Read Endpoints for Isolated Dashboard (Zero SQLite access required)
@app.get("/metrics")
def get_metrics():
    """Separated transaction-level and payout-level metrics."""
    conn = get_db_connection()
    try:
        total_oms = conn.execute("SELECT COUNT(*) FROM oms_orders").fetchone()[0]
        total_settle = conn.execute("SELECT COUNT(*) FROM razorpay_settlements").fetchone()[0]
        total_bank_cr = conn.execute("SELECT COUNT(*) FROM bank_payout_credits").fetchone()[0]

        total_volume_paise = conn.execute("SELECT COALESCE(SUM(amount_paise), 0) FROM razorpay_settlements").fetchone()[
            0
        ]

        # Decisions from projection view
        matched_tx = conn.execute(
            "SELECT COUNT(*) FROM v_current_decisions WHERE subject_type = 'BUSINESS_TX' AND match_status IN ('MATCHED', 'RULE_OVERRIDDEN')"
        ).fetchone()[0]
        exception_tx = conn.execute(
            "SELECT COUNT(*) FROM v_current_decisions WHERE subject_type = 'BUSINESS_TX' AND match_status = 'EXCEPTION'"
        ).fetchone()[0]

        matched_payouts = conn.execute(
            "SELECT COUNT(*) FROM v_current_decisions WHERE subject_type = 'PAYOUT' AND match_status = 'MATCHED'"
        ).fetchone()[0]
        exception_payouts = conn.execute(
            "SELECT COUNT(*) FROM v_current_decisions WHERE subject_type IN ('PAYOUT', 'BANK_CREDIT') AND match_status = 'EXCEPTION'"
        ).fetchone()[0]

        unresolved_count = conn.execute(
            "SELECT COUNT(*) FROM v_current_decisions WHERE current_disposition = 'UNRESOLVED' AND match_status = 'EXCEPTION'"
        ).fetchone()[0]
        approved_count = conn.execute("SELECT COUNT(*) FROM human_approvals").fetchone()[0]

        total_tx = matched_tx + exception_tx
        total_p = matched_payouts + exception_payouts

        return {
            "counts": {
                "oms_orders": total_oms,
                "settlements": total_settle,
                "bank_credits": total_bank_cr,
                "total_volume_paise": total_volume_paise,
                "total_volume_inr": format_paise_inr(total_volume_paise),
            },
            "transaction_metrics": {
                "total_transactions": total_tx,
                "matched_count": matched_tx,
                "exception_count": exception_tx,
                "match_rate_percent": round((matched_tx / total_tx) * 100, 2) if total_tx > 0 else 0.0,
                "unresolved_count": unresolved_count,
            },
            "payout_metrics": {
                "total_payout_batches": total_p,
                "matched_count": matched_payouts,
                "exception_count": exception_payouts,
                "match_rate_percent": round((matched_payouts / total_p) * 100, 2) if total_p > 0 else 0.0,
            },
            "governance": {"unresolved_exceptions": unresolved_count, "human_approvals_recorded": approved_count},
        }
    finally:
        conn.close()


@app.get("/payouts")
def get_payouts():
    """Payout batch aggregation joined to bank credits."""
    conn = get_db_connection()
    try:
        payout_rows = conn.execute("""
            SELECT
                s.payout_id,
                COUNT(s.payment_id) as settlement_count,
                SUM(s.amount_paise) as gross_paise,
                SUM(s.fee_paise) as fee_paise,
                SUM(s.tax_paise) as tax_paise,
                SUM(s.net_paise) as net_paise,
                b.credit_id,
                b.utr_number,
                b.credit_amount_paise,
                b.status as bank_status
            FROM razorpay_settlements s
            LEFT JOIN bank_payout_credits b ON s.payout_id = b.payout_id
            WHERE s.payout_id IS NOT NULL
            GROUP BY s.payout_id
            ORDER BY s.payout_id ASC;
        """).fetchall()

        # Unmatched bank credits (direct credits with no payout_id)
        direct_credits = conn.execute("""
            SELECT credit_id, utr_number, credit_amount_paise, credited_at, account_tail, status
            FROM bank_payout_credits
            WHERE payout_id IS NULL OR payout_id NOT IN (SELECT DISTINCT payout_id FROM razorpay_settlements WHERE payout_id IS NOT NULL);
        """).fetchall()

        payouts_data = []
        for r in payout_rows:
            net_p = r["net_paise"] or 0
            bank_p = r["credit_amount_paise"]
            if bank_p is None:
                status_label = "CREDIT_DELAYED"
            elif bank_p == net_p:
                status_label = "RECONCILED"
            else:
                status_label = "BANK_MISMATCH"

            payouts_data.append(
                {
                    "payout_id": r["payout_id"],
                    "settlement_count": r["settlement_count"],
                    "gross_paise": r["gross_paise"],
                    "net_paise": net_p,
                    "net_inr": format_paise_inr(net_p),
                    "credit_id": r["credit_id"],
                    "utr_number": r["utr_number"],
                    "bank_amount_paise": bank_p,
                    "bank_amount_inr": format_paise_inr(bank_p) if bank_p is not None else "Pending",
                    "status": status_label,
                }
            )

        direct_data = []
        for d in direct_credits:
            direct_data.append(
                {
                    "credit_id": d["credit_id"],
                    "utr_number": d["utr_number"],
                    "credit_amount_paise": d["credit_amount_paise"],
                    "credit_amount_inr": format_paise_inr(d["credit_amount_paise"]),
                    "credited_at": d["credited_at"],
                    "status": "UNMATCHED_DIRECT_CREDIT",
                }
            )

        return {"payout_batches": payouts_data, "unmatched_bank_credits": direct_data}
    finally:
        conn.close()


@app.get("/exceptions")
def get_exceptions():
    """Unresolved exception queue with minimized evidence bundles."""
    conn = get_db_connection()
    try:
        rows = conn.execute("""
            SELECT
                d.decision_id,
                d.subject_type,
                d.subject_id,
                d.business_tx_id,
                d.order_id,
                d.payment_id,
                d.payout_id,
                d.credit_id,
                d.discrepancy_code,
                d.variance_paise,
                d.evidence_json,
                d.current_disposition,
                d.decision_created_at
            FROM v_current_decisions d
            WHERE d.match_status = 'EXCEPTION'
            ORDER BY d.decision_id ASC;
        """).fetchall()

        exceptions_list = []
        for r in rows:
            exceptions_list.append(
                {
                    "decision_id": r["decision_id"],
                    "subject_type": r["subject_type"],
                    "subject_id": r["subject_id"],
                    "business_tx_id": r["business_tx_id"],
                    "order_id": r["order_id"],
                    "payment_id": r["payment_id"],
                    "payout_id": r["payout_id"],
                    "credit_id": r["credit_id"],
                    "discrepancy_code": r["discrepancy_code"],
                    "variance_paise": r["variance_paise"],
                    "variance_inr": format_paise_inr(r["variance_paise"]),
                    "evidence": json.loads(r["evidence_json"]) if r["evidence_json"] else {},
                    "current_disposition": r["current_disposition"],
                    "created_at": r["decision_created_at"],
                }
            )

        return {"exceptions": exceptions_list, "total_count": len(exceptions_list)}
    finally:
        conn.close()


@app.get("/audit-events")
def get_audit_events():
    """Append-only audit timeline."""
    conn = get_db_connection()
    try:
        events = conn.execute("""
            SELECT audit_id, event_type, aggregate_type, aggregate_id, payload_json, created_at
            FROM audit_events
            ORDER BY audit_id DESC
            LIMIT 100;
        """).fetchall()

        approvals = conn.execute("""
            SELECT approval_id, decision_id, action, reviewer, notes, created_at
            FROM human_approvals
            ORDER BY approval_id DESC
            LIMIT 100;
        """).fetchall()

        investigations = conn.execute("""
            SELECT investigation_id, decision_id, root_cause, confidence, proposed_action,
                   should_abstain, policy_status, created_at
            FROM agent_investigations
            ORDER BY investigation_id DESC
            LIMIT 100;
        """).fetchall()

        return {
            "audit_events": [dict(e) for e in events],
            "human_approvals": [dict(a) for a in approvals],
            "agent_investigations": [dict(i) for i in investigations],
        }
    finally:
        conn.close()


@app.get("/evaluation-report")
def get_evaluation_report():
    """Returns the latest benchmark evaluation report from out/evaluation-report.json."""
    report_file = OUT_DIR / "evaluation-report.json"
    if not report_file.exists():
        return {
            "status": "not_generated",
            "message": "Evaluation report has not been generated yet. Run 'python eval_benchmarks.py' to generate.",
        }
    try:
        with open(report_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read evaluation report: {exc}")


@app.get("/metrics/prometheus")
def get_prometheus_metrics():
    """Prometheus-compatible plain text metrics exposition."""
    m = get_metrics()
    lines = [
        "# HELP paisaguard_oms_orders_total Total internal OMS orders",
        "# TYPE paisaguard_oms_orders_total counter",
        f"paisaguard_oms_orders_total {m['counts']['oms_orders']}",
        "# HELP paisaguard_settlements_total Total gateway settlements ingested",
        "# TYPE paisaguard_settlements_total counter",
        f"paisaguard_settlements_total {m['counts']['settlements']}",
        "# HELP paisaguard_bank_credits_total Total bank payout credits received",
        "# TYPE paisaguard_bank_credits_total counter",
        f"paisaguard_bank_credits_total {m['counts']['bank_credits']}",
        "# HELP paisaguard_tx_match_rate_percent Transaction reconciliation match percentage",
        "# TYPE paisaguard_tx_match_rate_percent gauge",
        f"paisaguard_tx_match_rate_percent {m['transaction_metrics']['match_rate_percent']}",
        "# HELP paisaguard_payout_match_rate_percent Payout batch match percentage",
        "# TYPE paisaguard_payout_match_rate_percent gauge",
        f"paisaguard_payout_match_rate_percent {m['payout_metrics']['match_rate_percent']}",
        "# HELP paisaguard_unresolved_exceptions Unresolved exceptions pending in queue",
        "# TYPE paisaguard_unresolved_exceptions gauge",
        f"paisaguard_unresolved_exceptions {m['governance']['unresolved_exceptions']}",
    ]
    return Response(content="\n".join(lines) + "\n", media_type="text/plain")
