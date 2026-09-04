"""
test_paisa_guard.py — Comprehensive Pytest Test Suite for PaisaGuard FinOps Engine.

Covers:
1. Integer paise hygiene and arithmetic precision (no floats).
2. SQLite WAL mode, foreign keys, and synchronous pragma.
3. Deterministic 3-way reconciliation (OMS <-> Settlement <-> Bank Credits).
4. Multi-settlement to single payout batch matching and unmatched bank credits.
5. Re-run idempotency without duplicate decisions.
6. Fail-closed token security on state-changing endpoints.
7. Mandatory HMAC webhook enforcement and tampering rejection.
8. Webhook deduplication by event_id.
9. AI exception investigation: strict schema, cited IDs, policy gate.
10. AI deliberate abstention on genuine ambiguity.
11. AI failure hardening (malformed output, hallucinated citation, unsupported action, low confidence).
12. Append-only human approval and dynamic disposition projection view.
13. Read endpoints for isolated dashboard.
"""

import hashlib
import hmac
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import app
from audit_service import record_human_approval
from db import get_db_connection
from exception_agent import (
    BaseAIProvider,
    investigate_exception,
)
from money import calc_mdr_fee_and_tax_paise, parse_inr_to_paise, require_paise
from policy_gate import PolicyGatekeeper
from recon_engine import execute_reconciliation_pipeline
from seed_data import seed_database

TEST_DB_PATH = Path(__file__).resolve().parent / "test_paisa_guard.db"
TEST_SECRET = "rzp_sec_test_secret_2026"
TEST_TOKEN = "test_finops_token_2026"


@pytest.fixture(scope="module")
def setup_test_db():
    """Initializes a fresh test database with 3-source fixtures."""
    if TEST_DB_PATH.exists():
        try:
            TEST_DB_PATH.unlink()
        except Exception:
            pass

    os.environ["PAISAGUARD_DB_PATH"] = str(TEST_DB_PATH)
    os.environ["PAISAGUARD_API_TOKEN"] = TEST_TOKEN
    os.environ["RAZORPAY_WEBHOOK_SECRET"] = TEST_SECRET
    os.environ["PAISAGUARD_DEMO_ALLOW_UNSIGNED_WEBHOOKS"] = "false"
    os.environ["AI_PROVIDER"] = "mock"

    seed_database(db_path=TEST_DB_PATH, reset=True)
    yield TEST_DB_PATH

    if TEST_DB_PATH.exists():
        try:
            TEST_DB_PATH.unlink()
        except Exception:
            pass


# -------------------------------------------------------------
# 1. Canonical Integer Paise Hygiene Tests
# -------------------------------------------------------------
def test_integer_paise_hygiene():
    # String parsing
    assert parse_inr_to_paise("1499.50") == 149950
    assert parse_inr_to_paise(Decimal("1499.50")) == 149950
    assert parse_inr_to_paise("0.00") == 0

    # Type safety: reject raw int in parse_inr_to_paise
    with pytest.raises(TypeError):
        parse_inr_to_paise(1500)

    # require_paise guards
    assert require_paise(149950) == 149950
    with pytest.raises(TypeError):
        require_paise(1499.50)
    with pytest.raises(TypeError):
        require_paise("149950")
    with pytest.raises(TypeError):
        require_paise(True)

    # Fee and Tax integer arithmetic
    fee_p, tax_p = calc_mdr_fee_and_tax_paise(129950, mdr_bps=200, gst_bps=1800)
    assert fee_p == 2599
    assert tax_p == 468


# -------------------------------------------------------------
# 2. Database Pragmas & WAL Configuration
# -------------------------------------------------------------
def test_sqlite_wal_mode_active(setup_test_db):
    conn = get_db_connection(setup_test_db, use_wal=True)
    journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    synchronous = conn.execute("PRAGMA synchronous;").fetchone()[0]
    foreign_keys = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
    conn.close()

    assert journal_mode.lower() == "wal"
    assert synchronous in (1, "1", "NORMAL", "normal")
    assert foreign_keys == 1


# -------------------------------------------------------------
# 3. Deterministic 3-Way Reconciliation
# -------------------------------------------------------------
def test_3way_reconciliation_coverage(setup_test_db):
    summary = execute_reconciliation_pipeline(db_path=setup_test_db, include_held_out=False)

    assert summary["transaction_metrics"]["total_business_transactions"] == 100
    assert summary["transaction_metrics"]["match_rate_percent"] >= 85.0
    assert summary["transaction_metrics"]["matched_count"] >= 80
    assert summary["transaction_metrics"]["exception_count"] >= 8
    assert summary["payout_metrics"]["total_payout_batches"] >= 10
    assert summary["payout_metrics"]["match_rate_percent"] >= 70.0

    # Ensure output CSV artifacts exist
    assert Path(summary["matched_csv"]).exists()
    assert Path(summary["exception_csv"]).exists()


# -------------------------------------------------------------
# 4. Reconciliation Re-Run Idempotency
# -------------------------------------------------------------
def test_reconciliation_rerun_idempotency(setup_test_db):
    conn = get_db_connection(setup_test_db)
    decisions_before = conn.execute("SELECT COUNT(*) FROM reconciliation_decisions").fetchone()[0]
    links_before = conn.execute("SELECT COUNT(*) FROM run_decision_links").fetchone()[0]
    conn.close()

    # Re-run pipeline over unchanged inputs
    execute_reconciliation_pipeline(db_path=setup_test_db, include_held_out=False)

    conn = get_db_connection(setup_test_db)
    decisions_after = conn.execute("SELECT COUNT(*) FROM reconciliation_decisions").fetchone()[0]
    links_after = conn.execute("SELECT COUNT(*) FROM run_decision_links").fetchone()[0]
    runs_count = conn.execute("SELECT COUNT(*) FROM reconciliation_runs").fetchone()[0]
    conn.close()

    # Must NOT duplicate decisions
    assert decisions_after == decisions_before
    # Must link decisions to the new run
    assert links_after == links_before * 2
    assert runs_count >= 2


# -------------------------------------------------------------
# 5. Fail-Closed Token Security on Protected Endpoints
# -------------------------------------------------------------
def test_token_guard_fail_closed(setup_test_db, monkeypatch):
    client = TestClient(app)

    # 1. When server PAISAGUARD_API_TOKEN is unset -> fails closed with HTTP 503
    monkeypatch.setenv("PAISAGUARD_API_TOKEN", "")
    res = client.post("/reconcile/sweep")
    assert res.status_code == 503

    res_ai = client.post("/ai/investigate", json={"decision_id": 1})
    assert res_ai.status_code == 503

    res_app = client.post("/approvals/decision", json={"decision_id": 1, "action": "APPROVE", "reviewer": "cfo"})
    assert res_app.status_code == 503

    # 2. When server token is configured but client does not provide token -> 401
    monkeypatch.setenv("PAISAGUARD_API_TOKEN", TEST_TOKEN)
    res_unauth = client.post("/reconcile/sweep")
    assert res_unauth.status_code == 401

    # 3. When client provides wrong token -> 401
    res_wrong = client.post("/reconcile/sweep", headers={"X-PaisaGuard-Token": "wrong_token"})
    assert res_wrong.status_code == 401

    # 4. When client provides correct token -> 200
    res_ok = client.post("/reconcile/sweep", headers={"X-PaisaGuard-Token": TEST_TOKEN})
    assert res_ok.status_code == 200


# -------------------------------------------------------------
# 6. Webhook Ingestion, Mandatory HMAC & Tampering Rejection
# -------------------------------------------------------------
def test_webhook_hmac_enforcement_and_tampering(setup_test_db, monkeypatch):
    client = TestClient(app)
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", TEST_SECRET)
    monkeypatch.setenv("PAISAGUARD_DEMO_ALLOW_UNSIGNED_WEBHOOKS", "false")

    payload = {
        "event_id": "evt_test_hmac_01",
        "event": "payment.captured",
        "payment_id": "pay_test_hmac_01",
        "amount_paise": 149900,
        "fee_paise": 2998,
        "tax_paise": 540,
        "payment_method": "upi",
    }
    payload_bytes = json.dumps(payload).encode("utf-8")

    # 1. Unsigned request must be rejected with 401
    res_unsigned = client.post(
        "/webhooks/razorpay", content=payload_bytes, headers={"Content-Type": "application/json"}
    )
    assert res_unsigned.status_code == 401
    assert "Missing X-Razorpay-Signature" in res_unsigned.json()["detail"]

    # 2. Valid Hex signature -> 200
    hex_sig = hmac.new(TEST_SECRET.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    res_hex = client.post(
        "/webhooks/razorpay",
        content=payload_bytes,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": hex_sig},
    )
    assert res_hex.status_code == 200
    assert res_hex.json()["status"] == "ingested"

    # 3. Tampered payload with old signature -> 401
    tampered_bytes = json.dumps({**payload, "amount_paise": 999900}).encode("utf-8")
    res_tampered = client.post(
        "/webhooks/razorpay",
        content=tampered_bytes,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": hex_sig},
    )
    assert res_tampered.status_code == 401


# -------------------------------------------------------------
# 7. Webhook Idempotency & Deduplication
# -------------------------------------------------------------
def test_webhook_deduplication(setup_test_db, monkeypatch):
    client = TestClient(app)
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", TEST_SECRET)
    monkeypatch.setenv("PAISAGUARD_DEMO_ALLOW_UNSIGNED_WEBHOOKS", "false")

    payload = {
        "event_id": "evt_unique_dedup_99",
        "event": "payment.captured",
        "payment_id": "pay_unique_dedup_99",
        "amount_paise": 249900,
        "fee_paise": 4998,
        "tax_paise": 900,
    }
    raw = json.dumps(payload).encode("utf-8")
    sig = hmac.new(TEST_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()

    # First delivery: ingested
    r1 = client.post("/webhooks/razorpay", content=raw, headers={"X-Razorpay-Signature": sig})
    assert r1.status_code == 200
    assert r1.json()["status"] == "ingested"

    # Second delivery (replay): duplicate ignored
    r2 = client.post("/webhooks/razorpay", content=raw, headers={"X-Razorpay-Signature": sig})
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate_ignored"

    # Verify exactly 1 record in webhook_events
    conn = get_db_connection(setup_test_db)
    count = conn.execute("SELECT COUNT(*) FROM webhook_events WHERE event_id = 'evt_unique_dedup_99'").fetchone()[0]
    conn.close()
    assert count == 1


# -------------------------------------------------------------
# 8. AI Exception Investigation: Citations & Schema
# -------------------------------------------------------------
def test_ai_investigation_citations_and_schema(setup_test_db):
    conn = get_db_connection(setup_test_db)
    row = conn.execute(
        "SELECT decision_id FROM reconciliation_decisions WHERE match_status = 'EXCEPTION' LIMIT 1"
    ).fetchone()
    conn.close()

    assert row is not None
    dec_id = row["decision_id"]

    res = investigate_exception(dec_id, db_path=setup_test_db)

    assert res["decision_id"] == dec_id
    assert res["root_cause"] != ""
    assert 0.0 <= res["confidence"] <= 1.0
    assert len(res["evidence_record_ids"]) > 0
    assert res["proposed_action"] != ""
    assert res["policy_status"] in ("POLICY_APPROVED", "POLICY_REJECTED")


# -------------------------------------------------------------
# 9. AI Deliberate Abstention
# -------------------------------------------------------------
def test_ai_deliberate_abstention(setup_test_db):
    # Insert a synthetic ambiguous decision to test deliberate abstention
    conn = get_db_connection(setup_test_db)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO reconciliation_decisions (
            decision_fingerprint, input_snapshot_hash, matcher_version,
            subject_type, subject_id, match_status, discrepancy_code,
            variance_paise, evidence_json, created_at
        ) VALUES (
            'sha256:test_ambig_fp', 'sha256:test_snap', 'v2.0-three-way',
            'BUSINESS_TX', 'tx_test_ambig', 'EXCEPTION',
            'GENUINE_AMBIGUITY_INSUFFICIENT_DATA', 9999,
            '{"reason": "Incoherent pricing deduction with contradictory historical records", "amount_paise": 100000, "actual_fee_paise": 1234}',
            datetime('now')
        );
    """)
    ambig_dec_id = cursor.lastrowid
    conn.commit()
    conn.close()

    res = investigate_exception(ambig_dec_id, db_path=setup_test_db)

    assert res["should_abstain"] is True
    assert res["proposed_action"] == "ABSTAIN"
    assert res["abstention_reason"] is not None
    assert len(res["abstention_reason"]) > 5


# -------------------------------------------------------------
# 10. AI Agent Failure Hardening
# -------------------------------------------------------------
class MalformedProvider(BaseAIProvider):
    def investigate(self, prompt: str, evidence: dict) -> str:
        return "Not a valid JSON {{"


class HallucinatingProvider(BaseAIProvider):
    def investigate(self, prompt: str, evidence: dict) -> str:
        return json.dumps(
            {
                "root_cause": "FAKE_DIAGNOSIS",
                "confidence": 0.95,
                "evidence_record_ids": ["hallucinated_record_id_not_in_evidence"],
                "evidence_summary": "Made up evidence",
                "proposed_action": "ACCEPT_SURCHARGE_ADJUSTMENT",
                "requires_human_approval": True,
                "should_abstain": False,
            }
        )


class UnsupportedActionProvider(BaseAIProvider):
    def investigate(self, prompt: str, evidence: dict) -> str:
        return json.dumps(
            {
                "root_cause": "TEST_CAUSE",
                "confidence": 0.95,
                "evidence_record_ids": evidence.get("available_record_ids", []),
                "evidence_summary": "Summary",
                "proposed_action": "DELETE_ALL_RECORDS",  # Prohibited action!
                "requires_human_approval": True,
                "should_abstain": False,
            }
        )


class LowConfidenceProvider(BaseAIProvider):
    def investigate(self, prompt: str, evidence: dict) -> str:
        return json.dumps(
            {
                "root_cause": "UNCERTAIN_CAUSE",
                "confidence": 0.45,  # Low confidence
                "evidence_record_ids": evidence.get("available_record_ids", []),
                "evidence_summary": "Uncertain summary",
                "proposed_action": "REQUEST_MERCHANT_CLARIFICATION",
                "requires_human_approval": True,
                "should_abstain": False,
            }
        )


def test_ai_agent_failure_modes(setup_test_db):
    conn = get_db_connection(setup_test_db)
    row = conn.execute(
        "SELECT decision_id FROM reconciliation_decisions WHERE match_status = 'EXCEPTION' LIMIT 1"
    ).fetchone()
    conn.close()
    dec_id = row["decision_id"]

    # 1. Malformed JSON fallback -> Audited Abstention
    res_malformed = investigate_exception(dec_id, db_path=setup_test_db, provider=MalformedProvider())
    assert res_malformed["should_abstain"] is True
    assert res_malformed["root_cause"] == "DIAGNOSTIC_FAILURE"

    # 2. Hallucinated Citation -> Verification guardrail forces abstention
    res_hallucinated = investigate_exception(dec_id, db_path=setup_test_db, provider=HallucinatingProvider())
    assert res_hallucinated["should_abstain"] is True
    assert "Verification failure" in res_hallucinated["abstention_reason"]

    # 3. Unsupported Action -> Verification guardrail forces abstention
    res_action = investigate_exception(dec_id, db_path=setup_test_db, provider=UnsupportedActionProvider())
    assert res_action["should_abstain"] is True
    assert "not permitted" in res_action["abstention_reason"]

    # 4. Low Confidence (< 0.70) -> Forced abstention
    res_low = investigate_exception(dec_id, db_path=setup_test_db, provider=LowConfidenceProvider())
    assert res_low["should_abstain"] is True
    assert "below safety threshold" in res_low["abstention_reason"]


def test_policy_gate_rejection():
    # Variance > 5,000 paise (₹50.00 ceiling)
    approved, reason = PolicyGatekeeper.evaluate(
        gross_amount_paise=100000,
        actual_fee_paise=6000,
        expected_fee_paise=2000,
        variance_paise=7000,  # 7,000 paise > 5,000 ceiling!
        proposed_action="ACCEPT_SURCHARGE_ADJUSTMENT",
    )
    assert approved is False
    assert "exceeds hard safety ceiling" in reason

    # Effective fee > 3.50% MDR cap
    approved_cap, reason_cap = PolicyGatekeeper.evaluate(
        gross_amount_paise=100000,
        actual_fee_paise=4000,  # 4.0% > 3.50% cap
        expected_fee_paise=2000,
        variance_paise=2000,
        proposed_action="ACCEPT_SURCHARGE_ADJUSTMENT",
    )
    assert approved_cap is False
    assert "breaches statutory merchant contract cap" in reason_cap


# -------------------------------------------------------------
# 11. Strictly Append-Only Human Approval & State Projection
# -------------------------------------------------------------
def test_human_approval_append_only(setup_test_db):
    conn = get_db_connection(setup_test_db)
    row = conn.execute(
        "SELECT decision_id FROM reconciliation_decisions WHERE match_status = 'EXCEPTION' LIMIT 1"
    ).fetchone()
    conn.close()
    dec_id = row["decision_id"]

    # Check projection before approval
    conn = get_db_connection(setup_test_db)
    before_view = conn.execute(
        "SELECT current_disposition FROM v_current_decisions WHERE decision_id = ?", (dec_id,)
    ).fetchone()
    conn.close()
    assert before_view["current_disposition"] == "UNRESOLVED"

    # Record approval
    app_id = record_human_approval(
        decision_id=dec_id,
        action="APPROVE",
        reviewer="auditor_jane",
        notes="Validated against merchant contract",
        db_path=setup_test_db,
    )
    assert app_id > 0

    # Verify append-only entries
    conn = get_db_connection(setup_test_db)
    app_row = conn.execute("SELECT * FROM human_approvals WHERE approval_id = ?", (app_id,)).fetchone()
    assert app_row["reviewer"] == "auditor_jane"
    assert app_row["action"] == "APPROVE"

    # Verify projection view shows latest disposition
    after_view = conn.execute(
        "SELECT current_disposition, resolved_by FROM v_current_decisions WHERE decision_id = ?", (dec_id,)
    ).fetchone()
    assert after_view["current_disposition"] == "APPROVE"
    assert after_view["resolved_by"] == "auditor_jane"

    # Verify zero updates executed on reconciliation_decisions table (reconciliation_decisions does not even have disposition column)
    dec_cols = [c[1] for c in conn.execute("PRAGMA table_info(reconciliation_decisions)").fetchall()]
    assert "disposition" not in dec_cols
    conn.close()


# -------------------------------------------------------------
# 12. Isolated Dashboard Read Endpoints
# -------------------------------------------------------------
def test_dashboard_read_endpoints(setup_test_db):
    client = TestClient(app)

    # 1. GET /metrics
    r_m = client.get("/metrics")
    assert r_m.status_code == 200
    data_m = r_m.json()
    assert "counts" in data_m
    assert "transaction_metrics" in data_m
    assert "payout_metrics" in data_m

    # 2. GET /payouts
    r_p = client.get("/payouts")
    assert r_p.status_code == 200
    data_p = r_p.json()
    assert "payout_batches" in data_p
    assert "unmatched_bank_credits" in data_p

    # 3. GET /exceptions
    r_e = client.get("/exceptions")
    assert r_e.status_code == 200
    data_e = r_e.json()
    assert "exceptions" in data_e

    # 4. GET /audit-events
    r_a = client.get("/audit-events")
    assert r_a.status_code == 200
    data_a = r_a.json()
    assert "audit_events" in data_a
    assert "human_approvals" in data_a

    # 5. GET /evaluation-report
    r_r = client.get("/evaluation-report")
    assert r_r.status_code == 200

    # 6. GET /metrics/prometheus
    r_prom = client.get("/metrics/prometheus")
    assert r_prom.status_code == 200
    assert "paisaguard_tx_match_rate_percent" in r_prom.text
