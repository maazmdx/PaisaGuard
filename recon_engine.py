"""
recon_engine.py — Deterministic 3-Source Financial Reconciliation Engine for PaisaGuard.

Architectural Guarantees:
1. 3-Source Reconciliation: OMS Orders -> Razorpay Settlements -> Bank Payout Credits.
2. Canonical Currency: Pure integer paise across all operations. Zero float math.
3. Polymorphic Subjects: Supports BUSINESS_TX, PAYOUT, and BANK_CREDIT subjects.
4. Input Snapshot Hash & Decision Fingerprinting:
   - Provenance snapshot hash across all input records.
   - Decision fingerprint: sha256(snapshot + matcher_version + subject_type + subject_id).
5. Append-Only Idempotency: Re-runs link to existing decisions without duplicating audit decisions.
"""

import os
import sys
import json
import sqlite3
import hashlib
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_db_connection, get_db_cursor, get_db_path, log_audit_event
from money import require_paise, calc_mdr_fee_and_tax_paise, format_paise_inr, paise_to_rupees

OUT_DIR = BASE_DIR / "out"
MATCHER_VERSION = "v2.0-three-way"


def compute_input_snapshot_hash(conn: sqlite3.Connection) -> str:
    """
    Computes a deterministic SHA-256 fingerprint across all 3 source tables.
    Uses immutable source_payload_hash values to guarantee data provenance.
    """
    hasher = hashlib.sha256()
    cursor = conn.cursor()

    # Hash sorted OMS orders
    for row in cursor.execute("SELECT order_id, source_payload_hash FROM oms_orders ORDER BY order_id ASC"):
        hasher.update(f"oms:{row[0]}:{row[1]}|".encode("utf-8"))

    # Hash sorted Razorpay settlements
    for row in cursor.execute("SELECT payment_id, source_payload_hash FROM razorpay_settlements ORDER BY payment_id ASC"):
        hasher.update(f"rzp:{row[0]}:{row[1]}|".encode("utf-8"))

    # Hash sorted Bank payout credits
    for row in cursor.execute("SELECT credit_id, source_payload_hash FROM bank_payout_credits ORDER BY credit_id ASC"):
        hasher.update(f"bnk:{row[0]}:{row[1]}|".encode("utf-8"))

    return f"sha256:{hasher.hexdigest()}"


def execute_reconciliation_pipeline(
    db_path: Optional[Path] = None,
    include_held_out: bool = False
) -> Dict[str, Any]:
    """
    Executes the deterministic 3-Source Reconciliation Pipeline:
      Pass 1: OMS Orders <-> Razorpay Settlements (Transaction Level)
      Pass 2: Razorpay Settlements <-> Bank Payout Credits (Payout Batch Level)
      Pass 3: Scoped Rule Evaluation (Dynamic Pre-approved Overrides)
      Pass 4: Append-Only Idempotent Persistence & Output Generation
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target_db = db_path if db_path else get_db_path()

    conn = get_db_connection(target_db)
    input_snapshot_hash = compute_input_snapshot_hash(conn)
    git_sha = os.environ.get("GIT_SHA", os.environ.get("GITHUB_SHA", "local"))[:40]
    run_timestamp = datetime.now(timezone.utc).isoformat()

    # Load operational datasets
    if include_held_out:
        df_oms = pd.read_sql("SELECT * FROM oms_orders ORDER BY order_id ASC", conn)
        df_settle = pd.read_sql("SELECT * FROM razorpay_settlements ORDER BY payment_id ASC", conn)
    else:
        # Exclude held-out evaluation transactions from standard operational runs
        df_oms = pd.read_sql("SELECT * FROM oms_orders WHERE business_tx_id NOT LIKE 'tx_eval_%' ORDER BY order_id ASC", conn)
        df_settle = pd.read_sql("SELECT * FROM razorpay_settlements WHERE business_tx_id NOT LIKE 'tx_eval_%' ORDER BY payment_id ASC", conn)

    df_bank = pd.read_sql("SELECT * FROM bank_payout_credits ORDER BY credit_id ASC", conn)
    df_rules = pd.read_sql("SELECT * FROM resolved_rules WHERE status = 'ACTIVE' ORDER BY rule_id ASC", conn)
    conn.close()

    # Index rules by (scope_field, scope_value)
    active_rules = []
    for _, r in df_rules.iterrows():
        active_rules.append(r.to_dict())

    # Map records for O(1) matching
    oms_by_tx = {row["business_tx_id"]: row for _, row in df_oms.iterrows() if row["business_tx_id"]}
    oms_by_order = {row["order_id"]: row for _, row in df_oms.iterrows() if row["order_id"]}
    settle_by_tx = {row["business_tx_id"]: row for _, row in df_settle.iterrows() if row["business_tx_id"]}
    settle_by_order = {row["order_id"]: row for _, row in df_settle.iterrows() if row["order_id"]}
    bank_by_payout = {row["payout_id"]: row for _, row in df_bank.iterrows() if row["payout_id"]}

    all_tx_ids = sorted(list(set(list(oms_by_tx.keys()) + list(settle_by_tx.keys()))))

    decisions_to_record = []
    matched_records_export = []
    exception_records_export = []

    # -------------------------------------------------------------
    # PASS 1 & 3: OMS <-> Settlement (Transaction Level)
    # -------------------------------------------------------------
    tx_matched_count = 0
    tx_exception_count = 0

    for tx_id in all_tx_ids:
        oms_row = oms_by_tx.get(tx_id)
        settle_row = settle_by_tx.get(tx_id)

        # 1. Unsettled OMS order
        if settle_row is None:
            tx_exception_count += 1
            order_id = oms_row["order_id"]
            amt_p = require_paise(int(oms_row["amount_paise"]))
            evidence = {
                "business_tx_id": tx_id,
                "order_id": order_id,
                "amount_paise": amt_p,
                "created_at": oms_row["created_at"],
                "variance_paise": amt_p,
                "reason": f"OMS order {order_id} captured but no settlement recorded in payment gateway feed."
            }
            decisions_to_record.append({
                "subject_type": "BUSINESS_TX",
                "subject_id": tx_id,
                "business_tx_id": tx_id,
                "order_id": order_id,
                "payment_id": None,
                "payout_id": None,
                "credit_id": None,
                "match_status": "EXCEPTION",
                "discrepancy_code": "UNSETTLED_OMS_ORDER",
                "variance_paise": amt_p,
                "evidence": evidence
            })
            exception_records_export.append({
                "subject_type": "BUSINESS_TX",
                "subject_id": tx_id,
                "discrepancy_code": "UNSETTLED_OMS_ORDER",
                "variance_paise": amt_p,
                "variance_inr": format_paise_inr(amt_p),
                "summary": evidence["reason"]
            })
            continue

        # 2. Orphan settlement (in Razorpay, missing in OMS)
        if oms_row is None:
            tx_exception_count += 1
            pay_id = settle_row["payment_id"]
            s_amt_p = require_paise(int(settle_row["amount_paise"]))
            evidence = {
                "business_tx_id": tx_id,
                "payment_id": pay_id,
                "payout_id": settle_row.get("payout_id"),
                "amount_paise": s_amt_p,
                "settled_at": settle_row["settled_at"],
                "variance_paise": s_amt_p,
                "reason": f"Payment {pay_id} settled by gateway without corresponding internal OMS order."
            }
            decisions_to_record.append({
                "subject_type": "BUSINESS_TX",
                "subject_id": tx_id,
                "business_tx_id": tx_id,
                "order_id": settle_row.get("order_id"),
                "payment_id": pay_id,
                "payout_id": settle_row.get("payout_id"),
                "credit_id": None,
                "match_status": "EXCEPTION",
                "discrepancy_code": "ORPHAN_SETTLEMENT",
                "variance_paise": s_amt_p,
                "evidence": evidence
            })
            exception_records_export.append({
                "subject_type": "BUSINESS_TX",
                "subject_id": tx_id,
                "discrepancy_code": "ORPHAN_SETTLEMENT",
                "variance_paise": s_amt_p,
                "variance_inr": format_paise_inr(s_amt_p),
                "summary": evidence["reason"]
            })
            continue

        # Both OMS and Settlement exist
        order_id = oms_row["order_id"]
        pay_id = settle_row["payment_id"]
        payout_id = settle_row.get("payout_id")
        oms_amt_p = require_paise(int(oms_row["amount_paise"]))
        settle_amt_p = require_paise(int(settle_row["amount_paise"]))
        actual_fee_p = require_paise(int(settle_row["fee_paise"]))
        actual_tax_p = require_paise(int(settle_row["tax_paise"]))
        actual_net_p = require_paise(int(settle_row["net_paise"]))
        method = settle_row.get("payment_method", "upi")

        # Check gross amount mismatch (partial refund or under-settlement)
        if oms_amt_p != settle_amt_p:
            tx_exception_count += 1
            variance_p = abs(oms_amt_p - settle_amt_p)
            evidence = {
                "business_tx_id": tx_id,
                "order_id": order_id,
                "payment_id": pay_id,
                "oms_amount_paise": oms_amt_p,
                "settle_amount_paise": settle_amt_p,
                "variance_paise": variance_p,
                "reason": f"Gross amount mismatch: OMS ₹{paise_to_rupees(oms_amt_p)} vs Settlement ₹{paise_to_rupees(settle_amt_p)}."
            }
            decisions_to_record.append({
                "subject_type": "BUSINESS_TX",
                "subject_id": tx_id,
                "business_tx_id": tx_id,
                "order_id": order_id,
                "payment_id": pay_id,
                "payout_id": payout_id,
                "credit_id": None,
                "match_status": "EXCEPTION",
                "discrepancy_code": "PARTIAL_REFUND_MISMATCH",
                "variance_paise": variance_p,
                "evidence": evidence
            })
            exception_records_export.append({
                "subject_type": "BUSINESS_TX",
                "subject_id": tx_id,
                "discrepancy_code": "PARTIAL_REFUND_MISMATCH",
                "variance_paise": variance_p,
                "variance_inr": format_paise_inr(variance_p),
                "summary": evidence["reason"]
            })
            continue

        # Mathematical contract validation (Standard 2.0% MDR + 18% GST)
        expected_fee_p, expected_tax_p = calc_mdr_fee_and_tax_paise(oms_amt_p, mdr_bps=200, gst_bps=1800)
        fee_variance_p = actual_fee_p - expected_fee_p
        tax_variance_p = actual_tax_p - expected_tax_p

        # PASS 3: Fee Discrepancy & Scoped Rule Evaluation
        if fee_variance_p != 0:
            # Check for scoped rule match (no fuzzy string matching)
            matched_rule = None
            for rule in active_rules:
                if rule["scope_field"] == "payment_method" and rule["scope_value"] == method:
                    matched_rule = rule
                    break

            if matched_rule:
                tx_matched_count += 1
                evidence = {
                    "business_tx_id": tx_id,
                    "order_id": order_id,
                    "payment_id": pay_id,
                    "payout_id": payout_id,
                    "amount_paise": oms_amt_p,
                    "fee_paise": actual_fee_p,
                    "expected_fee_paise": expected_fee_p,
                    "fee_variance_paise": fee_variance_p,
                    "rule_id": matched_rule["rule_id"],
                    "rule_action": matched_rule["action"],
                    "reason": f"Contract fee discrepancy resolved by pre-approved rule {matched_rule['rule_id']} ({matched_rule['description']})."
                }
                decisions_to_record.append({
                    "subject_type": "BUSINESS_TX",
                    "subject_id": tx_id,
                    "business_tx_id": tx_id,
                    "order_id": order_id,
                    "payment_id": pay_id,
                    "payout_id": payout_id,
                    "credit_id": None,
                    "match_status": "RULE_OVERRIDDEN",
                    "discrepancy_code": "CORPORATE_CARD_SURCHARGE",
                    "variance_paise": fee_variance_p,
                    "evidence": evidence
                })
                matched_records_export.append({
                    "business_tx_id": tx_id,
                    "order_id": order_id,
                    "payment_id": pay_id,
                    "payout_id": payout_id,
                    "amount_paise": oms_amt_p,
                    "amount_inr": format_paise_inr(oms_amt_p),
                    "match_status": "RULE_OVERRIDDEN",
                    "rule_applied": matched_rule["rule_id"]
                })
            else:
                tx_exception_count += 1
                evidence = {
                    "business_tx_id": tx_id,
                    "order_id": order_id,
                    "payment_id": pay_id,
                    "payout_id": payout_id,
                    "amount_paise": oms_amt_p,
                    "actual_fee_paise": actual_fee_p,
                    "expected_fee_paise": expected_fee_p,
                    "fee_variance_paise": fee_variance_p,
                    "reason": f"Fee discrepancy: gateway deducted ₹{paise_to_rupees(actual_fee_p)} fee (expected ₹{paise_to_rupees(expected_fee_p)} @ 2.0% MDR)."
                }
                decisions_to_record.append({
                    "subject_type": "BUSINESS_TX",
                    "subject_id": tx_id,
                    "business_tx_id": tx_id,
                    "order_id": order_id,
                    "payment_id": pay_id,
                    "payout_id": payout_id,
                    "credit_id": None,
                    "match_status": "EXCEPTION",
                    "discrepancy_code": "FEE_TAX_DISCREPANCY",
                    "variance_paise": fee_variance_p,
                    "evidence": evidence
                })
                exception_records_export.append({
                    "subject_type": "BUSINESS_TX",
                    "subject_id": tx_id,
                    "discrepancy_code": "FEE_TAX_DISCREPANCY",
                    "variance_paise": fee_variance_p,
                    "variance_inr": format_paise_inr(fee_variance_p),
                    "summary": evidence["reason"]
                })
            continue

        # Transaction cleanly matches OMS <-> Settlement
        tx_matched_count += 1
        evidence = {
            "business_tx_id": tx_id,
            "order_id": order_id,
            "payment_id": pay_id,
            "payout_id": payout_id,
            "amount_paise": oms_amt_p,
            "fee_paise": actual_fee_p,
            "tax_paise": actual_tax_p,
            "net_paise": actual_net_p,
            "reason": "Exact verified 2-way OMS <-> Razorpay match in canonical integer paise."
        }
        decisions_to_record.append({
            "subject_type": "BUSINESS_TX",
            "subject_id": tx_id,
            "business_tx_id": tx_id,
            "order_id": order_id,
            "payment_id": pay_id,
            "payout_id": payout_id,
            "credit_id": None,
            "match_status": "MATCHED",
            "discrepancy_code": None,
            "variance_paise": 0,
            "evidence": evidence
        })
        matched_records_export.append({
            "business_tx_id": tx_id,
            "order_id": order_id,
            "payment_id": pay_id,
            "payout_id": payout_id,
            "amount_paise": oms_amt_p,
            "amount_inr": format_paise_inr(oms_amt_p),
            "match_status": "MATCHED",
            "rule_applied": None
        })

    # -------------------------------------------------------------
    # PASS 2: Settlements <-> Bank Payout Credits (Payout Batch Level)
    # -------------------------------------------------------------
    payout_groups = {}
    for _, s in df_settle.iterrows():
        p_id = s.get("payout_id")
        if p_id:
            payout_groups.setdefault(p_id, []).append(s.to_dict())

    payout_matched_count = 0
    payout_exception_count = 0

    for payout_id, s_list in payout_groups.items():
        batch_net_paise = sum(require_paise(int(s["net_paise"])) for s in s_list)
        settlement_count = len(s_list)
        bank_credit_row = bank_by_payout.get(payout_id)

        if bank_credit_row is None:
            # Delayed bank payout credit
            payout_exception_count += 1
            evidence = {
                "payout_id": payout_id,
                "settlement_count": settlement_count,
                "batch_net_paise": batch_net_paise,
                "reason": f"Payout batch {payout_id} ({settlement_count} settlements totaling ₹{paise_to_rupees(batch_net_paise)}) has not yet arrived in bank account credit feed."
            }
            decisions_to_record.append({
                "subject_type": "PAYOUT",
                "subject_id": payout_id,
                "business_tx_id": None,
                "order_id": None,
                "payment_id": None,
                "payout_id": payout_id,
                "credit_id": None,
                "match_status": "EXCEPTION",
                "discrepancy_code": "DELAYED_BANK_CREDIT",
                "variance_paise": batch_net_paise,
                "evidence": evidence
            })
            exception_records_export.append({
                "subject_type": "PAYOUT",
                "subject_id": payout_id,
                "discrepancy_code": "DELAYED_BANK_CREDIT",
                "variance_paise": batch_net_paise,
                "variance_inr": format_paise_inr(batch_net_paise),
                "summary": evidence["reason"]
            })
            continue

        # Bank credit received
        credit_id = bank_credit_row["credit_id"]
        bank_amount_paise = require_paise(int(bank_credit_row["credit_amount_paise"]))

        if bank_amount_paise != batch_net_paise:
            # Bank payout amount mismatch
            payout_exception_count += 1
            variance_p = abs(bank_amount_paise - batch_net_paise)
            evidence = {
                "payout_id": payout_id,
                "credit_id": credit_id,
                "utr_number": bank_credit_row["utr_number"],
                "settlement_count": settlement_count,
                "batch_net_paise": batch_net_paise,
                "bank_amount_paise": bank_amount_paise,
                "variance_paise": variance_p,
                "reason": f"Bank credit mismatch on {payout_id}: expected ₹{paise_to_rupees(batch_net_paise)} vs bank credited ₹{paise_to_rupees(bank_amount_paise)}."
            }
            decisions_to_record.append({
                "subject_type": "PAYOUT",
                "subject_id": payout_id,
                "business_tx_id": None,
                "order_id": None,
                "payment_id": None,
                "payout_id": payout_id,
                "credit_id": credit_id,
                "match_status": "EXCEPTION",
                "discrepancy_code": "BANK_AMOUNT_MISMATCH",
                "variance_paise": variance_p,
                "evidence": evidence
            })
            exception_records_export.append({
                "subject_type": "PAYOUT",
                "subject_id": payout_id,
                "discrepancy_code": "BANK_AMOUNT_MISMATCH",
                "variance_paise": variance_p,
                "variance_inr": format_paise_inr(variance_p),
                "summary": evidence["reason"]
            })
            continue

        # Exact Payout Match
        payout_matched_count += 1
        evidence = {
            "payout_id": payout_id,
            "credit_id": credit_id,
            "utr_number": bank_credit_row["utr_number"],
            "settlement_count": settlement_count,
            "batch_net_paise": batch_net_paise,
            "bank_amount_paise": bank_amount_paise,
            "reason": f"Exact 3-way verified bank payout match across {settlement_count} settlements."
        }
        decisions_to_record.append({
            "subject_type": "PAYOUT",
            "subject_id": payout_id,
            "business_tx_id": None,
            "order_id": None,
            "payment_id": None,
            "payout_id": payout_id,
            "credit_id": credit_id,
            "match_status": "MATCHED",
            "discrepancy_code": None,
            "variance_paise": 0,
            "evidence": evidence
        })
        matched_records_export.append({
            "business_tx_id": None,
            "order_id": None,
            "payment_id": None,
            "payout_id": payout_id,
            "amount_paise": batch_net_paise,
            "amount_inr": format_paise_inr(batch_net_paise),
            "match_status": "MATCHED",
            "rule_applied": None
        })

    # Unmatched Bank Credits (Direct credits received without known payout ID)
    for _, b in df_bank.iterrows():
        b_payout = b.get("payout_id")
        if not b_payout or b_payout not in payout_groups:
            payout_exception_count += 1
            cr_id = b["credit_id"]
            cr_amt_p = require_paise(int(b["credit_amount_paise"]))
            evidence = {
                "credit_id": cr_id,
                "utr_number": b["utr_number"],
                "credit_amount_paise": cr_amt_p,
                "credited_at": b["credited_at"],
                "account_tail": b.get("account_tail"),
                "reason": f"Direct bank credit of ₹{paise_to_rupees(cr_amt_p)} with UTR {b['utr_number']} received with no corresponding gateway payout batch."
            }
            decisions_to_record.append({
                "subject_type": "BANK_CREDIT",
                "subject_id": cr_id,
                "business_tx_id": None,
                "order_id": None,
                "payment_id": None,
                "payout_id": None,
                "credit_id": cr_id,
                "match_status": "EXCEPTION",
                "discrepancy_code": "UNIDENTIFIED_DIRECT_CREDIT",
                "variance_paise": cr_amt_p,
                "evidence": evidence
            })
            exception_records_export.append({
                "subject_type": "BANK_CREDIT",
                "subject_id": cr_id,
                "discrepancy_code": "UNIDENTIFIED_DIRECT_CREDIT",
                "variance_paise": cr_amt_p,
                "variance_inr": format_paise_inr(cr_amt_p),
                "summary": evidence["reason"]
            })

    # -------------------------------------------------------------
    # PASS 4: Append-Only Idempotent Persistence
    # -------------------------------------------------------------
    total_business_tx = len(all_tx_ids)
    total_payout_batches = len(payout_groups) + len([b for _, b in df_bank.iterrows() if not b.get("payout_id") or b.get("payout_id") not in payout_groups])
    total_source_records = len(df_oms) + len(df_settle) + len(df_bank)
    total_matched = tx_matched_count + payout_matched_count
    total_exceptions = tx_exception_count + payout_exception_count

    tx_match_rate = round((tx_matched_count / total_business_tx) * 100, 2) if total_business_tx > 0 else 0.0
    payout_match_rate = round((payout_matched_count / total_payout_batches) * 100, 2) if total_payout_batches > 0 else 0.0

    metrics_payload = {
        "transaction_metrics": {
            "total_business_transactions": total_business_tx,
            "matched_count": tx_matched_count,
            "exception_count": tx_exception_count,
            "match_rate_percent": tx_match_rate,
            "unresolved_rate_percent": round((tx_exception_count / total_business_tx) * 100, 2) if total_business_tx > 0 else 0.0
        },
        "payout_metrics": {
            "total_payout_batches": total_payout_batches,
            "matched_count": payout_matched_count,
            "exception_count": payout_exception_count,
            "match_rate_percent": payout_match_rate
        },
        "total_source_records": total_source_records,
        "input_snapshot_hash": input_snapshot_hash,
        "matcher_version": MATCHER_VERSION
    }

    with get_db_cursor(target_db) as cursor:
        # 1. Insert append-only reconciliation run
        cursor.execute("""
            INSERT INTO reconciliation_runs (
                run_timestamp, matcher_version, input_snapshot_hash, git_sha,
                total_business_tx, total_source_records, matched_count,
                exception_count, metrics_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'COMPLETED');
        """, (
            run_timestamp,
            MATCHER_VERSION,
            input_snapshot_hash,
            git_sha,
            total_business_tx,
            total_source_records,
            total_matched,
            total_exceptions,
            json.dumps(metrics_payload)
        ))
        run_id = cursor.lastrowid

        # 2. Persist decisions with idempotency check
        links_to_insert = []
        for dec in decisions_to_record:
            # Deterministic decision fingerprint: hash of snapshot + matcher version + subject
            fp_raw = f"{input_snapshot_hash}:{MATCHER_VERSION}:{dec['subject_type']}:{dec['subject_id']}"
            fingerprint = f"sha256:{hashlib.sha256(fp_raw.encode('utf-8')).hexdigest()}"

            # Check if decision already exists for this exact input snapshot and matcher version
            cursor.execute("SELECT decision_id FROM reconciliation_decisions WHERE decision_fingerprint = ?", (fingerprint,))
            existing = cursor.fetchone()

            if existing:
                decision_id = existing[0]
            else:
                cursor.execute("""
                    INSERT INTO reconciliation_decisions (
                        decision_fingerprint, input_snapshot_hash, matcher_version,
                        subject_type, subject_id, business_tx_id, order_id, payment_id,
                        payout_id, credit_id, match_status, discrepancy_code,
                        variance_paise, evidence_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    fingerprint,
                    input_snapshot_hash,
                    MATCHER_VERSION,
                    dec["subject_type"],
                    dec["subject_id"],
                    dec["business_tx_id"],
                    dec["order_id"],
                    dec["payment_id"],
                    dec["payout_id"],
                    dec["credit_id"],
                    dec["match_status"],
                    dec["discrepancy_code"],
                    dec["variance_paise"],
                    json.dumps(dec["evidence"], sort_keys=True),
                    run_timestamp
                ))
                decision_id = cursor.lastrowid

            links_to_insert.append((run_id, decision_id))

        # 3. Link run to decisions (idempotent link)
        cursor.executemany("""
            INSERT OR IGNORE INTO run_decision_links (run_id, decision_id) VALUES (?, ?);
        """, links_to_insert)

    # Log immutable audit event
    log_audit_event(
        target_db,
        event_type="RECONCILIATION_SWEEP_COMPLETED",
        aggregate_type="RECON_RUN",
        aggregate_id=str(run_id),
        payload={
            "run_id": run_id,
            "input_snapshot_hash": input_snapshot_hash,
            "matcher_version": MATCHER_VERSION,
            "total_business_tx": total_business_tx,
            "tx_match_rate": tx_match_rate,
            "payout_match_rate": payout_match_rate,
            "total_decisions_linked": len(links_to_insert)
        }
    )

    # Export CSV and JSON reports
    df_matched = pd.DataFrame(matched_records_export) if matched_records_export else pd.DataFrame(columns=["business_tx_id", "match_status"])
    df_exceptions = pd.DataFrame(exception_records_export) if exception_records_export else pd.DataFrame(columns=["subject_type", "subject_id", "discrepancy_code"])

    matched_path = OUT_DIR / "final-matched-ledger.csv"
    exception_path = OUT_DIR / "final-exception-queue.csv"
    summary_path = OUT_DIR / "recon-run-summary.json"

    df_matched.to_csv(matched_path, index=False)
    df_exceptions.to_csv(exception_path, index=False)

    summary = {
        "run_id": run_id,
        "run_timestamp": run_timestamp,
        "input_snapshot_hash": input_snapshot_hash,
        "matcher_version": MATCHER_VERSION,
        "git_sha": git_sha,
        "transaction_metrics": metrics_payload["transaction_metrics"],
        "payout_metrics": metrics_payload["payout_metrics"],
        "total_source_records": total_source_records,
        "matched_csv": str(matched_path),
        "exception_csv": str(exception_path)
    }

    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    return summary


if __name__ == "__main__":
    res = execute_reconciliation_pipeline()
    print("=" * 65)
    print("  PAISAGUARD DETERMINISTIC 3-SOURCE RECONCILIATION PIPELINE")
    print("=" * 65)
    print(f"  Run ID                  : {res['run_id']}")
    print(f"  Input Snapshot Hash     : {res['input_snapshot_hash'][:24]}...")
    print(f"  Matcher Version         : {res['matcher_version']}")
    print(f"  Business Transactions   : {res['transaction_metrics']['total_business_transactions']}")
    print(f"  Transaction Match Rate  : {res['transaction_metrics']['match_rate_percent']}%")
    print(f"  Transaction Exceptions  : {res['transaction_metrics']['exception_count']}")
    print(f"  Payout Batches Audited  : {res['payout_metrics']['total_payout_batches']}")
    print(f"  Payout Match Rate       : {res['payout_metrics']['match_rate_percent']}%")
    print(f"  Total Source Records    : {res['total_source_records']}")
    print("=" * 65)
