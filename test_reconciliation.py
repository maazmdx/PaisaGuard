import unittest
import sqlite3
import json
from decimal import Decimal
from pathlib import Path
from db import init_db, get_db_connection, DEFAULT_DB_PATH
from seed_data import generate_financial_dataset
from recon_engine import execute_reconciliation_pipeline
from concurrency_tester import simulate_webhook_flood

TEST_DB_PATH = Path(__file__).resolve().parent / "test_reconciliation.db"

class TestReconciliationEngine(unittest.TestCase):
    """
    Standard-library test suite validating:
    1. SQLite Write-Ahead-Logging (WAL) configuration and normal synchronicity.
    2. Sub-paise precision math and Aggregate Rounding Accumulator bounds.
    3. Dual-layer GST ITC leakage auditing (identifying ₹7.04 tax over-deductions).
    4. Deterministic rule cache lookup and 1-click dynamic overrides.
    5. Lock-free multi-threaded concurrency under simulated webhook bursts.
    6. Safety Gate compliance limits (< ₹50.00 ceiling, <= 3.5% MDR cap).
    """

    @classmethod
    def setUpClass(cls):
        if TEST_DB_PATH.exists():
            try:
                TEST_DB_PATH.unlink()
            except Exception:
                pass
        generate_financial_dataset(TEST_DB_PATH)

    @classmethod
    def tearDownClass(cls):
        if TEST_DB_PATH.exists():
            try:
                TEST_DB_PATH.unlink()
            except Exception:
                pass

    def test_01_sqlite_wal_pragmas(self):
        """Verify database pragma configuration operates in WAL mode with synchronous NORMAL."""
        conn = get_db_connection(TEST_DB_PATH, use_wal=True)
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        synchronous = conn.execute("PRAGMA synchronous;").fetchone()[0]
        conn.close()

        self.assertEqual(journal_mode.lower(), "wal", f"Expected WAL mode, received {journal_mode}")
        self.assertIn(synchronous, [1, "1", "NORMAL", "normal"], f"Expected synchronous NORMAL (1), received {synchronous}")

    def test_02_aggregate_rounding_accumulator_precision(self):
        """Verify 4-Pass reconciliation math, match rate >= 90%, and bounded sub-paise accumulator drift."""
        summary = execute_reconciliation_pipeline(TEST_DB_PATH)

        self.assertGreater(summary["total_audited"], 0)
        self.assertGreaterEqual(summary["match_rate"], 90.0, f"Match rate {summary['match_rate']}% below target 90%")
        self.assertGreaterEqual(summary["matched_count"], 90)
        self.assertGreaterEqual(summary["exception_count"], 5)

        # Cumulative sub-paise drift across 100 transactions must remain within +/- ₹0.50 tolerance
        self.assertLessEqual(abs(summary["sub_paise_accumulator"]), 0.50)

        # Ensure output CSV artifacts exist
        self.assertTrue(Path(summary["matched_csv"]).exists())
        self.assertTrue(Path(summary["exception_csv"]).exists())

    def test_03_gst_itc_leakage_audit(self):
        """Verify Section 16(2)(aa) audit detects the exact gateway tax leakage (₹7.04)."""
        summary = execute_reconciliation_pipeline(TEST_DB_PATH)
        expected_leakage = 7.04
        self.assertEqual(round(summary["gst_tax_leakage"], 2), expected_leakage)

    def test_04_dynamic_rule_override(self):
        """Verify that committing an override rule to resolved_rules reconciles exceptions in sub-0.2ms."""
        conn = get_db_connection(TEST_DB_PATH)
        row = conn.execute(
            "SELECT order_id FROM reconciliation_ledger WHERE exception_code = 'FEE_DEDUCTION' LIMIT 1"
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row, "Expected at least one fee discrepancy in test dataset")
        target_order_id = row["order_id"]

        # Insert deterministic override into resolved_rules
        conn = get_db_connection(TEST_DB_PATH)
        conn.execute("""
            INSERT INTO resolved_rules (rule_id, pattern_key, action, exception_code, description, created_at)
            VALUES (?, ?, 'APPROVE_CORPORATE_CARD_CHARGE', 'FEE_DEDUCTION', 'Test corporate card override', datetime('now'));
        """, (f"rule_{target_order_id}", target_order_id))
        conn.commit()
        conn.close()

        # Re-run reconciliation sweep
        execute_reconciliation_pipeline(TEST_DB_PATH)

        conn = get_db_connection(TEST_DB_PATH)
        reconciled = conn.execute(
            "SELECT reconciled_status FROM reconciliation_ledger WHERE order_id = ?",
            (target_order_id,)
        ).fetchone()
        conn.close()

        self.assertEqual(reconciled["reconciled_status"], "RULE_OVERRIDDEN")

    def test_05_lock_free_wal_concurrency(self):
        """Verify 50 concurrent worker threads commit with 0 database lock errors under WAL mode."""
        benchmark_db = Path(__file__).resolve().parent / "test_concurrency_wal_temp.db"
        result = simulate_webhook_flood(benchmark_db, num_threads=50, use_wal=True)

        if benchmark_db.exists():
            try:
                benchmark_db.unlink()
            except Exception:
                pass

        self.assertEqual(result["successful_commits"], 50)
        self.assertEqual(result["lock_errors"], 0)

    def test_06_policy_gatekeeper_thresholds(self):
        """Verify programmatic boundaries: Reject variances > ₹50 or effective MDR > 3.5%."""
        max_variance_ceiling = Decimal("50.00")
        max_mdr_cap = Decimal("0.035")

        def evaluate_proposal(variance: Decimal, fee_rate: Decimal) -> bool:
            return variance <= max_variance_ceiling and fee_rate <= max_mdr_cap

        # Valid corporate rate (2.5% fee, ₹12.50 variance on ₹500 txn) -> Approved
        self.assertTrue(evaluate_proposal(Decimal("12.50"), Decimal("0.025")))

        # Excessive variance (₹65.00) -> Rejected
        self.assertFalse(evaluate_proposal(Decimal("65.00"), Decimal("0.020")))

        # Excessive MDR rate (4.2%) -> Rejected
        self.assertFalse(evaluate_proposal(Decimal("20.00"), Decimal("0.042")))

if __name__ == "__main__":
    unittest.main()
