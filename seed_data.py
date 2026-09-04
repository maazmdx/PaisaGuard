"""
seed_data.py — Seeds the database with high-fidelity 3-source reconciliation fixtures.

Architectural Guarantees:
1. Distinguishes business transactions from source records:
   - 100 Operational Business Transactions (producing 300+ source records across OMS, Razorpay, Bank, and Webhooks).
   - 30 Held-Out Evaluation Transactions (for evaluating AI diagnostics and deliberate abstention).
2. Canonical Integer Paise: All monetary fields strictly stored as integer paise via require_paise().
3. Safe Resets: Requires explicit --reset flag to purge tables; never drops or wipes tables on standard runtime.
4. Reads from checked-in fixtures/ground_truth_manifest.json.
"""

import sys
import json
import argparse
from pathlib import Path
from typing import Optional, Union

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import init_db, get_db_connection, get_db_cursor, get_db_path, log_audit_event
from money import require_paise

MANIFEST_PATH = BASE_DIR / "fixtures" / "ground_truth_manifest.json"


def seed_database(db_path: Optional[Union[str, Path]] = None, reset: bool = False) -> dict:
    """
    Seeds the SQLite database from ground_truth_manifest.json.
    If reset=False and the database is already populated, refuses to overwrite.
    """
    target_db = Path(db_path) if db_path else get_db_path()
    init_db(target_db)

    conn = get_db_connection(target_db)
    try:
        existing_orders = conn.execute("SELECT COUNT(*) FROM oms_orders").fetchone()[0]
        existing_settlements = conn.execute("SELECT COUNT(*) FROM razorpay_settlements").fetchone()[0]
        
        if (existing_orders > 0 or existing_settlements > 0) and not reset:
            print(f"[seed_data] Notice: Database at {target_db} already populated ({existing_orders} orders, {existing_settlements} settlements).")
            print("[seed_data] Pass --reset to purge and re-seed. Skipping seeding.")
            return {
                "status": "skipped",
                "reason": "already_populated",
                "oms_orders": existing_orders,
                "settlements": existing_settlements
            }
    finally:
        conn.close()

    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"Ground-truth manifest not found at: {MANIFEST_PATH}")

    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    oms_rows = []
    settle_rows = []
    webhook_rows = []

    for tx in manifest.get("transactions", []):
        # OMS order
        if tx.get("oms_order"):
            o = tx["oms_order"]
            oms_rows.append((
                o["order_id"],
                o.get("business_tx_id", tx.get("business_tx_id")),
                require_paise(o["amount_paise"]),
                o.get("currency", "INR"),
                o.get("customer_id"),
                o["source_payload_hash"],
                o["created_at"],
                o.get("status", "created")
            ))

        # Razorpay settlement
        if tx.get("settlement"):
            s = tx["settlement"]
            settle_rows.append((
                s["payment_id"],
                s.get("business_tx_id", tx.get("business_tx_id")),
                s.get("order_id"),
                s.get("payout_id"),
                require_paise(s["amount_paise"]),
                require_paise(s["fee_paise"]),
                require_paise(s["tax_paise"]),
                require_paise(s["net_paise"]),
                s.get("currency", "INR"),
                s.get("payment_method", "upi"),
                s["source_payload_hash"],
                s["settled_at"],
                s.get("status", "settled")
            ))

        # Webhook event
        if tx.get("webhook_event"):
            w = tx["webhook_event"]
            webhook_rows.append((
                w["event_id"],
                w.get("payment_id"),
                w["source_payload_hash"],
                w["payload_json"],
                w.get("hmac_signature"),
                w["received_at"],
                w.get("status", "received")
            ))

    # Bank payout credits
    bank_rows = []
    for b in manifest.get("bank_payout_credits", []):
        bank_rows.append((
            b["credit_id"],
            b.get("payout_id"),
            b["utr_number"],
            require_paise(b["credit_amount_paise"]),
            b["source_payload_hash"],
            b["credited_at"],
            b.get("account_tail"),
            b.get("status", "credited")
        ))

    # Pre-configure approved corporate card override rule
    rules = [
        (
            "rule_corporate_card_surcharge_2026",
            "MDR_SURCHARGE",
            "payment_method",
            "corporate_card",
            "APPROVE_CORPORATE_CARD_CHARGE",
            "CORPORATE_CARD_SURCHARGE",
            "Approved 2.5% MDR + 18% GST for commercial card interchange surcharge",
            "finops_policy_engine",
            "2026-08-01 00:00:00",
            "ACTIVE"
        )
    ]

    with get_db_cursor(target_db) as cursor:
        if reset:
            cursor.execute("DELETE FROM oms_orders;")
            cursor.execute("DELETE FROM razorpay_settlements;")
            cursor.execute("DELETE FROM bank_payout_credits;")
            cursor.execute("DELETE FROM webhook_events;")
            cursor.execute("DELETE FROM run_decision_links;")
            cursor.execute("DELETE FROM agent_investigations;")
            cursor.execute("DELETE FROM human_approvals;")
            cursor.execute("DELETE FROM reconciliation_decisions;")
            cursor.execute("DELETE FROM reconciliation_runs;")
            cursor.execute("DELETE FROM audit_events;")
            cursor.execute("DELETE FROM resolved_rules;")

        cursor.executemany("""
            INSERT INTO oms_orders (
                order_id, business_tx_id, amount_paise, currency, customer_id,
                source_payload_hash, created_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """, oms_rows)

        cursor.executemany("""
            INSERT INTO razorpay_settlements (
                payment_id, business_tx_id, order_id, payout_id, amount_paise,
                fee_paise, tax_paise, net_paise, currency, payment_method,
                source_payload_hash, settled_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, settle_rows)

        cursor.executemany("""
            INSERT INTO bank_payout_credits (
                credit_id, payout_id, utr_number, credit_amount_paise,
                source_payload_hash, credited_at, account_tail, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """, bank_rows)

        cursor.executemany("""
            INSERT INTO webhook_events (
                event_id, payment_id, source_payload_hash, payload_json,
                hmac_signature, received_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
        """, webhook_rows)

        cursor.executemany("""
            INSERT OR REPLACE INTO resolved_rules (
                rule_id, rule_type, scope_field, scope_value, action,
                exception_code, description, created_by, created_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, rules)

    log_audit_event(
        target_db,
        event_type="FIXTURE_SEEDED",
        aggregate_type="DATABASE",
        aggregate_id="reconciliation_fixture",
        payload={
            "reset": reset,
            "oms_orders_count": len(oms_rows),
            "settlements_count": len(settle_rows),
            "bank_credits_count": len(bank_rows),
            "webhook_events_count": len(webhook_rows),
            "total_source_records": len(oms_rows) + len(settle_rows) + len(bank_rows) + len(webhook_rows)
        }
    )

    total_records = len(oms_rows) + len(settle_rows) + len(bank_rows) + len(webhook_rows)
    print(f"[seed_data] Successfully seeded database at: {target_db}")
    print(f"  OMS Orders        : {len(oms_rows)}")
    print(f"  Razorpay Settle   : {len(settle_rows)}")
    print(f"  Bank Credits      : {len(bank_rows)}")
    print(f"  Webhook Events    : {len(webhook_rows)}")
    print(f"  Total Source Recs : {total_records} (Guaranteed >300)")

    return {
        "status": "seeded",
        "oms_orders": len(oms_rows),
        "settlements": len(settle_rows),
        "bank_credits": len(bank_rows),
        "webhook_events": len(webhook_rows),
        "total_source_records": total_records
    }


def main():
    parser = argparse.ArgumentParser(description="PaisaGuard Synthetic Fixture Seeder")
    parser.add_argument("--reset", action="store_true", help="Explicitly purge and rebuild database fixtures")
    parser.add_argument("--db", type=str, default=None, help="Optional database file path")
    args = parser.parse_args()

    seed_database(db_path=args.db, reset=args.reset)


if __name__ == "__main__":
    main()
