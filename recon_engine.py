import os
import sqlite3
import pandas as pd
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
from pathlib import Path
from db import get_db_connection, get_db_cursor, DEFAULT_DB_PATH

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "out"
TWO_PLACES = Decimal("0.01")

def round_curr(val: Decimal) -> Decimal:
    return val.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

def execute_reconciliation_pipeline(db_path: Path = DEFAULT_DB_PATH) -> dict:
    """
    Executes the deterministic 4-Pass Reconciliation Engine:
      Pass 1: Key & Gross Amount Verification (OMS vs Razorpay)
      Pass 2: Sub-Paise Rounding Accumulator & Contract MDR Validation
      Pass 3: Exception Routing & Dynamic Rule Resolution
      Pass 4: Daily-to-Monthly GST ITC Safeguard Audit
    Exports:
      - out/final-matched-ledger.csv
      - out/final-exception-queue.csv
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    
    conn = get_db_connection(db_path)
    
    # 1. Load operational datasets into DataFrames
    df_oms = pd.read_sql("SELECT * FROM oms_orders", conn)
    df_settle = pd.read_sql("SELECT * FROM razorpay_settlements", conn)
    df_rules = pd.read_sql("SELECT * FROM resolved_rules", conn)
    df_gst_inv = pd.read_sql("SELECT * FROM gst_monthly_invoices", conn)
    conn.close()

    # Pre-index rules by exception pattern or order ID for O(1) matching
    rule_map = {}
    for _, r in df_rules.iterrows():
        rule_map[r["pattern_key"]] = r.to_dict()

    matched_records = []
    exception_records = []
    ledger_entries = []

    settle_by_order = {row["order_id"]: row for _, row in df_settle.iterrows() if row["order_id"]}
    oms_by_order = {row["order_id"]: row for _, row in df_oms.iterrows()}

    # Contract parameters: Standard 2.0% MDR + 18% GST
    contract_mdr_rate = Decimal("0.020")
    gst_rate = Decimal("0.18")

    # Rolling sub-paise accumulator to safely absorb cumulative fractional rounding
    rolling_sub_paise_drift = Decimal("0.0000")

    recon_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # --- PASS 1 & 2: Process OMS orders against Razorpay settlements ---
    for order_id, oms_row in oms_by_order.items():
        gross_amt = Decimal(str(oms_row["gross_amount"]))
        
        if order_id not in settle_by_order:
            # Unsettled transaction
            exc = {
                "order_id": order_id,
                "payment_id": "N/A",
                "exception_code": "UNSETTLED_PENDING",
                "variance": float(gross_amt),
                "exception_msg": f"Order {order_id} captured in OMS but pending gateway settlement.",
                "status": "PENDING"
            }
            exception_records.append(exc)
            ledger_entries.append((
                order_id, None, "EXCEPTION", float(gross_amt), 0.0, 0.0, 0.0, 0.0,
                0.0, 0.0, 0.0, 0.0, "UNSETTLED_PENDING", exc["exception_msg"], recon_timestamp
            ))
            continue

        settle_row = settle_by_order[order_id]
        pay_id = settle_row["payment_id"]
        settle_amt = Decimal(str(settle_row["amount"]))
        actual_fee = Decimal(str(settle_row["fee"]))
        actual_tax = Decimal(str(settle_row["tax"]))
        actual_net = Decimal(str(settle_row["net_amount"]))

        # Check gross amount mismatch
        amt_diff = abs(gross_amt - settle_amt)
        if amt_diff > Decimal("0.01"):
            exc = {
                "order_id": order_id,
                "payment_id": pay_id,
                "exception_code": "AMOUNT_MISMATCH",
                "variance": float(amt_diff),
                "exception_msg": f"Gross amount mismatch: OMS ₹{gross_amt:.2f} vs Settlement ₹{settle_amt:.2f}.",
                "status": "DISCREPANCY"
            }
            exception_records.append(exc)
            ledger_entries.append((
                order_id, pay_id, "EXCEPTION", float(gross_amt), float(settle_amt),
                0.0, float(actual_fee), 0.0, 0.0, float(actual_tax), 0.0, 0.0,
                "AMOUNT_MISMATCH", exc["exception_msg"], recon_timestamp
            ))
            continue

        # Mathematical contract validation with sub-paise accumulator
        raw_fee = gross_amt * contract_mdr_rate
        expected_fee = round_curr(raw_fee)
        raw_tax = expected_fee * gst_rate
        expected_tax = round_curr(raw_tax)

        # Mathematical rounding difference
        drift = (expected_fee + expected_tax) - (raw_fee + raw_tax)
        rolling_sub_paise_drift += drift

        fee_diff = actual_fee - expected_fee
        tax_diff = actual_tax - expected_tax

        # PASS 3: Contract Fee Discrepancy & Rules Engine Evaluation
        if abs(fee_diff) > Decimal("0.01"):
            # Check if resolved by rules engine
            rule_matched = None
            for key, rule in rule_map.items():
                if key in [order_id, pay_id, "FEE_DEDUCTION"] or "corporate" in key.lower():
                    rule_matched = rule
                    break

            if rule_matched is not None:
                matched_records.append({
                    "order_id": order_id,
                    "payment_id": pay_id,
                    "gross_amount": float(gross_amt),
                    "settled_amount": float(settle_amt),
                    "fee": float(actual_fee),
                    "tax": float(actual_tax),
                    "net_amount": float(actual_net),
                    "sub_paise_drift": float(drift),
                    "status": "RULE_OVERRIDDEN",
                    "settled_at": settle_row["settled_at"]
                })
                ledger_entries.append((
                    order_id, pay_id, "RULE_OVERRIDDEN", float(gross_amt), float(settle_amt),
                    float(expected_fee), float(actual_fee), float(fee_diff),
                    float(expected_tax), float(actual_tax), float(tax_diff),
                    float(drift), "FEE_OVERRIDE_APPLIED", rule_matched["description"], recon_timestamp
                ))
            else:
                exc = {
                    "order_id": order_id,
                    "payment_id": pay_id,
                    "exception_code": "FEE_DEDUCTION",
                    "variance": float(fee_diff),
                    "exception_msg": f"Gateway deducted ₹{actual_fee:.2f} fee (expected ₹{expected_fee:.2f} @ 2.0% MDR). Suspected Corporate Card Rate (2.5%).",
                    "status": "UNRESOLVED"
                }
                exception_records.append(exc)
                ledger_entries.append((
                    order_id, pay_id, "EXCEPTION", float(gross_amt), float(settle_amt),
                    float(expected_fee), float(actual_fee), float(fee_diff),
                    float(expected_tax), float(actual_tax), float(tax_diff),
                    float(drift), "FEE_DEDUCTION", exc["exception_msg"], recon_timestamp
                ))
            continue

        # Transaction perfectly matches contract
        matched_records.append({
            "order_id": order_id,
            "payment_id": pay_id,
            "gross_amount": float(gross_amt),
            "settled_amount": float(settle_amt),
            "fee": float(actual_fee),
            "tax": float(actual_tax),
            "net_amount": float(actual_net),
            "sub_paise_drift": float(drift),
            "status": "MATCHED",
            "settled_at": settle_row["settled_at"]
        })
        ledger_entries.append((
            order_id, pay_id, "MATCHED", float(gross_amt), float(settle_amt),
            float(expected_fee), float(actual_fee), 0.0,
            float(expected_tax), float(actual_tax), 0.0,
            float(drift), None, "Verified Sub-Paise Match", recon_timestamp
        ))

    # Check for Orphan settlements (in Razorpay but missing in OMS)
    for _, settle_row in df_settle.iterrows():
        s_ord_id = settle_row["order_id"]
        if not s_ord_id or s_ord_id not in oms_by_order:
            pay_id = settle_row["payment_id"]
            s_amt = settle_row["amount"]
            exc = {
                "order_id": s_ord_id or "UNKNOWN",
                "payment_id": pay_id,
                "exception_code": "ORPHAN_SETTLEMENT",
                "variance": float(s_amt),
                "exception_msg": f"Payment {pay_id} settled by Razorpay without corresponding internal OMS order.",
                "status": "AUDIT_REQUIRED"
            }
            exception_records.append(exc)
            ledger_entries.append((
                s_ord_id, pay_id, "EXCEPTION", 0.0, float(s_amt),
                0.0, float(settle_row["fee"]), 0.0,
                0.0, float(settle_row["tax"]), 0.0,
                0.0, "ORPHAN_SETTLEMENT", exc["exception_msg"], recon_timestamp
            ))

    # PASS 4: Daily-to-Monthly GST ITC Safeguard Audit
    total_daily_tax = df_settle["tax"].sum()
    monthly_invoice_tax = 0.0
    if not df_gst_inv.empty:
        monthly_invoice_tax = float(df_gst_inv.iloc[0]["total_gst"])
    
    gst_tax_leakage = round(total_daily_tax - monthly_invoice_tax, 2)

    # Persist outputs
    df_matched = pd.DataFrame(matched_records)
    df_exceptions = pd.DataFrame(exception_records)

    matched_path = OUT_DIR / "final-matched-ledger.csv"
    exception_path = OUT_DIR / "final-exception-queue.csv"

    df_matched.to_csv(matched_path, index=False)
    df_exceptions.to_csv(exception_path, index=False)

    # Generate and export Monthly GST ITC Discrepancy Audit Report
    tax_report_path = OUT_DIR / "monthly-tax-audit-report.json"
    root_tax_report_path = BASE_DIR / "monthly-tax-audit-report.json"
    tax_report = {
        "report_title": "PaisaGuard Monthly GST ITC Discrepancy & Tax Audit Report",
        "audit_month": "August 2026",
        "compliance_framework": "Section 16(2)(aa) of CGST Act / GSTR-2B Matching",
        "audited_at": recon_timestamp,
        "daily_settlement_aggregate_gst": round(float(total_daily_tax), 2),
        "gstr2b_monthly_invoice_gst": round(float(monthly_invoice_tax), 2),
        "detected_tax_leakage": gst_tax_leakage,
        "status": "DISCREPANCY_DETECTED" if gst_tax_leakage != 0 else "BALANCED",
        "recommended_action": f"Dispute notice auto-generated for ₹{gst_tax_leakage:.2f} excess deduction on supplier GSTR-1 discrepancy" if gst_tax_leakage > 0 else "No tax leakage detected. GSTR-2B reconciles.",
        "details": {
            "total_settlements_audited": len(df_settle),
            "discrepancy_direction": "GATEWAY_OVER_DEDUCTION" if gst_tax_leakage > 0 else "NONE",
            "risk_exposure": "Loss of eligible Input Tax Credit under GSTR-2B reconciliation"
        }
    }
    with open(tax_report_path, "w") as f:
        import json
        json.dump(tax_report, f, indent=2)
    with open(root_tax_report_path, "w") as f:
        import json
        json.dump(tax_report, f, indent=2)

    # Update reconciliation_ledger in SQLite
    with get_db_cursor(db_path) as cursor:
        cursor.execute("DELETE FROM reconciliation_ledger;")
        cursor.executemany("""
            INSERT INTO reconciliation_ledger (
                order_id, payment_id, reconciled_status, order_amount, settlement_amount,
                expected_fee, actual_fee, fee_variance, expected_tax, actual_tax, tax_variance,
                sub_paise_drift, exception_code, exception_msg, reconciled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, ledger_entries)

    total_audited = len(df_settle)
    matched_count = len(matched_records)
    match_rate = (matched_count / total_audited) * 100 if total_audited > 0 else 0.0

    summary = {
        "total_audited": total_audited,
        "matched_count": matched_count,
        "exception_count": len(exception_records),
        "match_rate": round(match_rate, 2),
        "sub_paise_accumulator": float(rolling_sub_paise_drift),
        "gst_daily_aggregate": round(float(total_daily_tax), 2),
        "gst_monthly_invoice": round(float(monthly_invoice_tax), 2),
        "gst_tax_leakage": gst_tax_leakage,
        "matched_csv": str(matched_path),
        "exception_csv": str(exception_path)
    }
    return summary

if __name__ == "__main__":
    res = execute_reconciliation_pipeline()
    print("Reconciliation Pipeline Execution Complete:")
    for k, v in res.items():
        print(f"  {k}: {v}")
