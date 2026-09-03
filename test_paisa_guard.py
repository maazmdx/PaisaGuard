import pytest
import sqlite3
from decimal import Decimal
from pathlib import Path
from db import init_db, get_db_connection, DEFAULT_DB_PATH
from seed_data import generate_financial_dataset
from recon_engine import execute_reconciliation_pipeline
from concurrency_tester import simulate_webhook_flood

TEST_DB_PATH = Path(__file__).resolve().parent / "test_paisa_guard.db"

@pytest.fixture(scope="module")
def setup_test_environment():
    """Initializes a fresh test database with realistic synthetic financial data."""
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
    generate_financial_dataset(TEST_DB_PATH)
    yield TEST_DB_PATH
    if TEST_DB_PATH.exists():
        try:
            TEST_DB_PATH.unlink()
        except Exception:
            pass

def test_sqlite_wal_mode_active(setup_test_environment):
    """Verifies that the SQLite database operates in WAL (Write-Ahead Logging) mode."""
    conn = get_db_connection(setup_test_environment, use_wal=True)
    journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    synchronous = conn.execute("PRAGMA synchronous;").fetchone()[0]
    conn.close()
    
    assert journal_mode.lower() == "wal", f"Expected WAL mode, got {journal_mode}"
    # 1 is NORMAL, 2 is FULL
    assert synchronous in [1, "1", "NORMAL", "normal"]

def test_sub_paise_drift_and_pipeline_execution(setup_test_environment):
    """Verifies that the 4-pass reconciliation engine executes with sub-paise accuracy."""
    summary = execute_reconciliation_pipeline(setup_test_environment)
    
    assert summary["total_audited"] > 0
    assert summary["match_rate"] >= 90.0, f"Match rate {summary['match_rate']}% below target"
    assert summary["matched_count"] > 90
    assert summary["exception_count"] >= 5
    
    # Verify sub-paise accumulator drift is bounded within +/- 0.50 INR across 100 transactions
    assert abs(summary["sub_paise_accumulator"]) <= 0.50
    
    # Verify CSV files are produced
    assert Path(summary["matched_csv"]).exists()
    assert Path(summary["exception_csv"]).exists()

def test_gst_itc_leakage_detection(setup_test_environment):
    """Verifies that PaisaGuard catches the exact GST ITC over-deduction (₹7.04)."""
    summary = execute_reconciliation_pipeline(setup_test_environment)
    
    expected_leakage = 7.04
    assert round(summary["gst_tax_leakage"], 2) == expected_leakage, (
        f"Expected {expected_leakage} INR leakage, detected {summary['gst_tax_leakage']} INR"
    )

def test_1click_rule_resolution(setup_test_environment):
    """Verifies that adding a corporate fee override rule dynamically clears an exception."""
    # Find an order with FEE_DEDUCTION
    conn = get_db_connection(setup_test_environment)
    exception_row = conn.execute(
        "SELECT order_id FROM reconciliation_ledger WHERE exception_code = 'FEE_DEDUCTION' LIMIT 1"
    ).fetchone()
    conn.close()
    
    assert exception_row is not None, "Expected at least one fee discrepancy in test data"
    target_order_id = exception_row["order_id"]
    
    # Apply override rule
    conn = get_db_connection(setup_test_environment)
    conn.execute("""
        INSERT INTO resolved_rules (rule_id, pattern_key, action, exception_code, description, created_at)
        VALUES (?, ?, 'APPROVE_CORPORATE_CARD_CHARGE', 'FEE_DEDUCTION', 'Test override rule', datetime('now'));
    """, (f"rule_test_{target_order_id}", target_order_id))
    conn.commit()
    conn.close()
    
    # Re-run reconciliation pipeline
    summary_after = execute_reconciliation_pipeline(setup_test_environment)
    
    # Assert that this order is now reconciled under RULE_OVERRIDDEN
    conn = get_db_connection(setup_test_environment)
    updated_row = conn.execute(
        "SELECT reconciled_status FROM reconciliation_ledger WHERE order_id = ?",
        (target_order_id,)
    ).fetchone()
    conn.close()
    
    assert updated_row["reconciled_status"] == "RULE_OVERRIDDEN"

def test_lock_free_wal_concurrency():
    """Verifies that concurrent multi-threaded requests achieve 100% lock-free commits in WAL mode."""
    benchmark_db = Path(__file__).resolve().parent / "test_concurrency_wal.db"
    res = simulate_webhook_flood(benchmark_db, num_threads=50, use_wal=True)
    
    assert res["successful_commits"] == 50, f"Expected 50 commits, got {res['successful_commits']}"
    assert res["lock_errors"] == 0, f"Expected 0 lock errors in WAL mode, got {res['lock_errors']}"
    assert res["throughput_tps"] > 50.0

    if benchmark_db.exists():
        try:
            benchmark_db.unlink()
        except Exception:
            pass

def test_fastapi_endpoints():
    """Verifies FastAPI gateway endpoints for health, metrics, and webhook ingestion."""
    from fastapi.testclient import TestClient
    from api import app
    client = TestClient(app)
    
    # 1. Health check
    r_root = client.get("/")
    assert r_root.status_code == 200
    assert r_root.json()["wal_mode"] is True
    
    # 2. Metrics check
    r_metrics = client.get("/metrics")
    assert r_metrics.status_code == 200
    data = r_metrics.json()
    assert "matched_transactions" in data
    assert data["total_settlements"] >= 100
    
    # 3. Webhook Ingestion (Idempotent UPSERT)
    payload = {
        "event": "payment.captured",
        "payment_id": "pay_test_webhook_999",
        "order_id": "ord_in_1001",
        "amount": 2500.0,
        "fee": 50.0,
        "tax": 9.0,
        "payment_method": "upi"
    }
    r_webhook = client.post("/webhooks/razorpay", json=payload)
    assert r_webhook.status_code == 200
    assert r_webhook.json()["status"] == "success"

def test_hmac_signature_verification():
    """Verifies HMAC SHA256 signature verification on raw byte stream and rejection of tampered payloads."""
    import hmac
    import hashlib
    import json
    from fastapi.testclient import TestClient
    from api import app, RAZORPAY_WEBHOOK_SECRET
    client = TestClient(app)

    payload = {
        "event": "payment.captured",
        "payment_id": "pay_hmac_valid_001",
        "order_id": "ord_in_1002",
        "amount": 1299.50,
        "fee": 25.99,
        "tax": 4.68,
        "payment_method": "upi"
    }
    raw_bytes = json.dumps(payload).encode("utf-8")
    valid_sig = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()

    # 1. Valid Hex Signature -> 200 OK
    r_valid = client.post(
        "/webhooks/razorpay",
        content=raw_bytes,
        headers={"X-Razorpay-Signature": valid_sig, "Content-Type": "application/json"}
    )
    assert r_valid.status_code == 200
    assert r_valid.json()["hmac_verified"] is True

    # 1b. Valid Base64 Signature -> 200 OK
    import base64
    valid_b64_sig = base64.b64encode(hmac.new(RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), raw_bytes, hashlib.sha256).digest()).decode("utf-8")
    r_valid_b64 = client.post(
        "/webhooks/razorpay",
        content=raw_bytes,
        headers={"X-Razorpay-Signature": valid_b64_sig, "Content-Type": "application/json"}
    )
    assert r_valid_b64.status_code == 200
    assert r_valid_b64.json()["hmac_verified"] is True

    # 1c. Nested Razorpay Event Format -> 200 OK
    nested_payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_nested_test_99",
                    "order_id": "ord_in_1002",
                    "amount": 129950,
                    "fee": 2599,
                    "tax": 468,
                    "method": "card"
                }
            }
        }
    }
    nested_bytes = json.dumps(nested_payload).encode("utf-8")
    nested_sig = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), nested_bytes, hashlib.sha256).hexdigest()
    r_nested = client.post(
        "/webhooks/razorpay",
        content=nested_bytes,
        headers={"X-Razorpay-Signature": nested_sig, "Content-Type": "application/json"}
    )
    assert r_nested.status_code == 200
    assert r_nested.json()["payment_id"] == "pay_nested_test_99"

    # 2. Tampered Signature -> 401 Unauthorized
    r_tampered = client.post(
        "/webhooks/razorpay",
        content=raw_bytes,
        headers={"X-Razorpay-Signature": "tampered_fake_signature_hash_12345", "Content-Type": "application/json"}
    )
    assert r_tampered.status_code == 401
    assert "signature verification failed" in r_tampered.json()["detail"].lower()

def test_policy_gatekeeper_boundaries():
    """Verifies the Deterministic Policy Gatekeeper approves safe variances and rejects excessive risk."""
    from fastapi.testclient import TestClient
    from api import app
    client = TestClient(app)

    # 1. Safe Corporate Card variance (₹27.45 <= ₹50.00 ceiling, 2.5% MDR <= 3.5% cap) -> APPROVED
    safe_req = {
        "order_id": "ord_in_1042",
        "payment_id": "pay_rzp_800042",
        "gross_amount": 5490.0,
        "actual_fee": 137.25,
        "expected_fee": 109.80,
        "exception_code": "FEE_DEDUCTION"
    }
    r_safe = client.post("/ai/diagnose", json=safe_req)
    assert r_safe.status_code == 200
    res_safe = r_safe.json()
    assert res_safe["policy_approved"] is True
    assert "safe economic bounds" in res_safe["policy_reason"].lower()

    # 2. Excessive variance (₹150.00 > ₹50.00 hard economic ceiling) -> REJECTED
    risky_req = {
        "order_id": "ord_in_exploit_99",
        "payment_id": "pay_rzp_exploit_99",
        "gross_amount": 5000.0,
        "actual_fee": 250.00,
        "expected_fee": 100.00,
        "exception_code": "FEE_DEDUCTION"
    }
    r_risky = client.post("/ai/diagnose", json=risky_req)
    assert r_risky.status_code == 200
    res_risky = r_risky.json()
    assert res_risky["policy_approved"] is False
    assert "exceeds hard safety ceiling" in res_risky["policy_reason"].lower()


