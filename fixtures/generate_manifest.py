"""
generate_manifest.py — Generates the checked-in ground_truth_manifest.json.

Creates:
- 100 Operational Business Transactions (generating 300+ source records across OMS, Razorpay, Bank, and Webhooks).
- 2 Operational Unmatched Bank Credits (subject_type: BANK_CREDIT).
- 30 Held-Out Evaluation Transactions (for testing AI root cause classification and deliberate abstention).
- Polymorphic subjects: BUSINESS_TX, PAYOUT, and BANK_CREDIT.
"""

import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from money import calc_mdr_fee_and_tax_paise, parse_inr_to_paise

MANIFEST_PATH = BASE_DIR / "fixtures" / "ground_truth_manifest.json"


def sha256_hash(data: str) -> str:
    return f"sha256:{hashlib.sha256(data.encode('utf-8')).hexdigest()}"


def build_manifest():
    manifest = {
        "manifest_version": "1.0",
        "description": "Checked-in ground-truth manifest for PaisaGuard 3-Source Reconciliation",
        "generated_at": "2026-08-01T00:00:00Z",
        "contracts": {
            "standard_mdr_bps": 200,  # 2.00%
            "standard_gst_bps": 1800,  # 18.00% on fee
            "corporate_mdr_bps": 250,  # 2.50%
            "corporate_gst_bps": 1800,
        },
        "transactions": [],
    }

    base_time = datetime(2026, 8, 1, 9, 0, 0)
    sample_amounts = [
        "499.00",
        "899.00",
        "1299.50",
        "1499.00",
        "2499.00",
        "3999.00",
        "5490.00",
        "7999.00",
        "999.00",
        "1599.00",
    ]

    payout_groups = {}  # payout_id -> list of settlement records

    # -------------------------------------------------------------
    # 1. Operational Transactions: 100 Business Transactions
    # -------------------------------------------------------------
    for i in range(1, 101):
        tx_id = f"tx_ops_{i:03d}"
        order_id = f"ord_in_{1000 + i}"
        customer_id = f"cust_{2000 + (i % 30)}"
        payment_id = f"pay_rzp_{800000 + i}"
        event_id = f"evt_rzp_{700000 + i}"
        payout_idx = ((i - 1) // 10) + 1
        payout_id = f"payout_aug_{payout_idx:02d}"

        amount_str = sample_amounts[(i * 7) % len(sample_amounts)]
        amount_paise = parse_inr_to_paise(amount_str)
        created_time = (base_time + timedelta(hours=i * 2, minutes=(i * 11) % 60)).isoformat() + "Z"
        settled_time = (base_time + timedelta(hours=i * 2 + 24, minutes=(i * 11) % 60)).isoformat() + "Z"

        # Categorize operational scenarios
        if 81 <= i <= 84:
            # Corporate card fee surcharge discrepancy (unresolved exception at baseline before rule/approval)
            expected_status = "EXCEPTION"
            expected_root_cause = "CORPORATE_CARD_SURCHARGE"
            should_abstain = False
            expected_disposition = "APPLY_CORPORATE_CARD_RULE"
            payment_method = "corporate_card"
            fee_paise, tax_paise = calc_mdr_fee_and_tax_paise(amount_paise, mdr_bps=250, gst_bps=1800)
            net_paise = amount_paise - (fee_paise + tax_paise)
            has_oms = True
            has_settle = True
            payout_rel = payout_id

        elif 85 <= i <= 88:
            # Clean transaction at business_tx level, but allocated to a delayed payout batch
            expected_status = "MATCHED"
            expected_root_cause = "CLEAN_TRANSACTION"
            should_abstain = False
            expected_disposition = "RECONCILED"
            payment_method = "upi"
            fee_paise, tax_paise = calc_mdr_fee_and_tax_paise(amount_paise, mdr_bps=200, gst_bps=1800)
            net_paise = amount_paise - (fee_paise + tax_paise)
            has_oms = True
            has_settle = True
            payout_rel = f"payout_delayed_aug_{payout_idx:02d}"

        elif 89 <= i <= 91:
            # Unsettled OMS order (missing settlement in Razorpay)
            expected_status = "EXCEPTION"
            expected_root_cause = "UNSETTLED_OMS_ORDER"
            should_abstain = False
            expected_disposition = "AWAIT_SETTLEMENT"
            payment_method = "upi"
            fee_paise, tax_paise = 0, 0
            net_paise = 0
            has_oms = True
            has_settle = False
            payout_rel = None

        elif 92 <= i <= 94:
            # Orphan settlement (present in Razorpay, missing in OMS)
            expected_status = "EXCEPTION"
            expected_root_cause = "ORPHAN_SETTLEMENT"
            should_abstain = False
            expected_disposition = "AUDIT_ORPHAN_SETTLEMENT"
            payment_method = "credit_card"
            fee_paise, tax_paise = calc_mdr_fee_and_tax_paise(amount_paise, mdr_bps=200, gst_bps=1800)
            net_paise = amount_paise - (fee_paise + tax_paise)
            has_oms = False
            has_settle = True
            payout_rel = payout_id

        elif 95 <= i <= 96:
            # Partial refund / amount mismatch
            expected_status = "EXCEPTION"
            expected_root_cause = "PARTIAL_REFUND_MISMATCH"
            should_abstain = False
            expected_disposition = "INVESTIGATE_PARTIAL_REFUND"
            payment_method = "netbanking"
            fee_paise, tax_paise = calc_mdr_fee_and_tax_paise(amount_paise, mdr_bps=200, gst_bps=1800)
            net_paise = amount_paise - (fee_paise + tax_paise)
            has_oms = True
            has_settle = True
            payout_rel = payout_id
            # Settle amount differs from OMS amount by 50000 paise (₹500.00)
            settle_amount_paise = amount_paise - 50000

        elif 97 <= i <= 98:
            # Clean at business_tx level; payout bank mismatch occurs at payout level
            expected_status = "MATCHED"
            expected_root_cause = "CLEAN_TRANSACTION"
            should_abstain = False
            expected_disposition = "RECONCILED"
            payment_method = "upi"
            fee_paise, tax_paise = calc_mdr_fee_and_tax_paise(amount_paise, mdr_bps=200, gst_bps=1800)
            net_paise = amount_paise - (fee_paise + tax_paise)
            has_oms = True
            has_settle = True
            payout_rel = "payout_mismatch_aug_10"

        else:
            # Clean match
            expected_status = "MATCHED"
            expected_root_cause = "CLEAN_TRANSACTION"
            should_abstain = False
            expected_disposition = "RECONCILED"
            payment_method = "upi" if i % 2 == 0 else "card"
            fee_paise, tax_paise = calc_mdr_fee_and_tax_paise(amount_paise, mdr_bps=200, gst_bps=1800)
            net_paise = amount_paise - (fee_paise + tax_paise)
            has_oms = True
            has_settle = True
            payout_rel = payout_id

        settle_amt = settle_amount_paise if (95 <= i <= 96) else amount_paise

        # Build records
        oms_record = None
        if has_oms:
            oms_raw = f"order_id:{order_id}:tx:{tx_id}:amt:{amount_paise}:cust:{customer_id}:ts:{created_time}"
            oms_record = {
                "order_id": order_id,
                "business_tx_id": tx_id,
                "amount_paise": amount_paise,
                "currency": "INR",
                "customer_id": customer_id,
                "source_payload_hash": sha256_hash(oms_raw),
                "created_at": created_time,
                "status": "created",
            }

        settle_record = None
        webhook_record = None
        if has_settle:
            settle_raw = f"pay_id:{payment_id}:tx:{tx_id}:ord:{order_id}:amt:{settle_amt}:fee:{fee_paise}:tax:{tax_paise}:net:{net_paise}:payout:{payout_rel}"
            settle_record = {
                "payment_id": payment_id,
                "business_tx_id": tx_id,
                "order_id": order_id if has_oms else f"unrecorded_{order_id}",
                "payout_id": payout_rel,
                "amount_paise": settle_amt,
                "fee_paise": fee_paise,
                "tax_paise": tax_paise,
                "net_paise": net_paise,
                "currency": "INR",
                "payment_method": payment_method,
                "source_payload_hash": sha256_hash(settle_raw),
                "settled_at": settled_time,
                "status": "settled",
            }
            webhook_raw = f"event_id:{event_id}:pay_id:{payment_id}:amt:{settle_amt}"
            webhook_record = {
                "event_id": event_id,
                "payment_id": payment_id,
                "source_payload_hash": sha256_hash(webhook_raw),
                "payload_json": json.dumps(settle_record),
                "hmac_signature": "sha256:valid_fixture_hmac",
                "received_at": settled_time,
                "status": "processed",
            }
            if payout_rel:
                payout_groups.setdefault(payout_rel, []).append(settle_record)

        associated_ids = []
        if oms_record:
            associated_ids.append(oms_record["order_id"])
        if settle_record:
            associated_ids.append(settle_record["payment_id"])

        manifest["transactions"].append(
            {
                "subject_type": "BUSINESS_TX",
                "subject_id": tx_id,
                "business_tx_id": tx_id,
                "is_held_out": False,
                "expected_reconciliation_status": expected_status,
                "expected_root_cause": expected_root_cause,
                "payout_relationship": payout_rel,
                "should_abstain": should_abstain,
                "expected_final_disposition": expected_disposition,
                "associated_record_ids": associated_ids,
                "oms_order": oms_record,
                "settlement": settle_record,
                "webhook_event": webhook_record,
            }
        )

    # -------------------------------------------------------------
    # 2. Operational Unmatched Bank Credits (subject_type: BANK_CREDIT)
    # -------------------------------------------------------------
    unmatched_bank_credits = [
        {
            "credit_id": "bank_cr_unmatched_01",
            "payout_id": None,
            "utr_number": "UTR_DIRECT_CREDIT_9901",
            "credit_amount_paise": 5000000,  # ₹50,000.00
            "credited_at": "2026-08-10T11:00:00Z",
            "account_tail": "9421",
            "status": "credited",
            "expected_root_cause": "UNIDENTIFIED_DIRECT_CREDIT",
            "expected_final_disposition": "ESCALATE_TO_BANK_OPS",
        },
        {
            "credit_id": "bank_cr_unmatched_02",
            "payout_id": None,
            "utr_number": "UTR_DIRECT_CREDIT_9902",
            "credit_amount_paise": 7500000,  # ₹75,000.00
            "credited_at": "2026-08-15T15:30:00Z",
            "account_tail": "9421",
            "status": "credited",
            "expected_root_cause": "UNIDENTIFIED_DIRECT_CREDIT",
            "expected_final_disposition": "ESCALATE_TO_BANK_OPS",
        },
    ]
    for ubc in unmatched_bank_credits:
        credit_raw = f"credit_id:{ubc['credit_id']}:amt:{ubc['credit_amount_paise']}:utr:{ubc['utr_number']}"
        ubc_record = {
            "credit_id": ubc["credit_id"],
            "payout_id": None,
            "utr_number": ubc["utr_number"],
            "credit_amount_paise": ubc["credit_amount_paise"],
            "source_payload_hash": sha256_hash(credit_raw),
            "credited_at": ubc["credited_at"],
            "account_tail": ubc["account_tail"],
            "status": ubc["status"],
        }
        manifest["transactions"].append(
            {
                "subject_type": "BANK_CREDIT",
                "subject_id": ubc["credit_id"],
                "business_tx_id": None,
                "is_held_out": False,
                "expected_reconciliation_status": "UNMATCHED_BANK_CREDIT",
                "expected_root_cause": ubc["expected_root_cause"],
                "payout_relationship": None,
                "should_abstain": False,
                "expected_final_disposition": ubc["expected_final_disposition"],
                "associated_record_ids": [ubc["credit_id"]],
                "bank_credit": ubc_record,
            }
        )

    # -------------------------------------------------------------
    # 3. Operational Bank Payout Credits (matched to payout_groups)
    # -------------------------------------------------------------
    manifest["bank_payout_credits"] = []
    for pid, s_list in payout_groups.items():
        total_net = sum(s["net_paise"] for s in s_list)
        if "delayed" in pid:
            continue
        elif "mismatch" in pid:
            bank_amt = total_net - 10000
        else:
            bank_amt = total_net

        cr_id = f"bank_cr_{pid}"
        utr_no = f"UTR_SETL_{pid.upper()}"
        cr_time = (base_time + timedelta(days=2)).isoformat() + "Z"
        cr_raw = f"credit_id:{cr_id}:payout:{pid}:amt:{bank_amt}:utr:{utr_no}"
        cr_rec = {
            "credit_id": cr_id,
            "payout_id": pid,
            "utr_number": utr_no,
            "credit_amount_paise": bank_amt,
            "source_payload_hash": sha256_hash(cr_raw),
            "credited_at": cr_time,
            "account_tail": "9421",
            "status": "credited",
        }
        manifest["bank_payout_credits"].append(cr_rec)

    for ubc in unmatched_bank_credits:
        credit_raw = f"credit_id:{ubc['credit_id']}:amt:{ubc['credit_amount_paise']}:utr:{ubc['utr_number']}"
        manifest["bank_payout_credits"].append(
            {
                "credit_id": ubc["credit_id"],
                "payout_id": None,
                "utr_number": ubc["utr_number"],
                "credit_amount_paise": ubc["credit_amount_paise"],
                "source_payload_hash": sha256_hash(credit_raw),
                "credited_at": ubc["credited_at"],
                "account_tail": ubc["account_tail"],
                "status": ubc["status"],
            }
        )

    # -------------------------------------------------------------
    # 4. Held-Out Evaluation Transactions: 30 Business Transactions
    # -------------------------------------------------------------
    for j in range(1, 31):
        tx_id = f"tx_eval_{j:03d}"
        order_id = f"ord_eval_{2000 + j}"
        customer_id = f"cust_eval_{3000 + j}"
        payment_id = f"pay_eval_{900000 + j}"
        event_id = f"evt_eval_{800000 + j}"
        payout_id = f"payout_eval_{((j - 1) // 10) + 1:02d}"

        amount_str = sample_amounts[j % len(sample_amounts)]
        amount_paise = parse_inr_to_paise(amount_str)
        created_time = (base_time + timedelta(days=15, hours=j)).isoformat() + "Z"
        settled_time = (base_time + timedelta(days=16, hours=j)).isoformat() + "Z"

        if 1 <= j <= 10:
            # Clean match
            exp_status = "MATCHED"
            exp_cause = "CLEAN_TRANSACTION"
            abstain = False
            exp_disp = "RECONCILED"
            method = "upi"
            fee_p, tax_p = calc_mdr_fee_and_tax_paise(amount_paise, 200, 1800)
            net_p = amount_paise - (fee_p + tax_p)
            has_o = True
            has_s = True
            settle_p = amount_paise

        elif 11 <= j <= 15:
            # Corporate card fee surcharge
            exp_status = "EXCEPTION"
            exp_cause = "CORPORATE_CARD_SURCHARGE"
            abstain = False
            exp_disp = "APPLY_CORPORATE_CARD_RULE"
            method = "corporate_card"
            fee_p, tax_p = calc_mdr_fee_and_tax_paise(amount_paise, 250, 1800)
            net_p = amount_paise - (fee_p + tax_p)
            has_o = True
            has_s = True
            settle_p = amount_paise

        elif 16 <= j <= 18:
            # Unsettled OMS order
            exp_status = "EXCEPTION"
            exp_cause = "UNSETTLED_OMS_ORDER"
            abstain = False
            exp_disp = "AWAIT_SETTLEMENT"
            method = "upi"
            fee_p, tax_p = 0, 0
            net_p = 0
            has_o = True
            has_s = False
            settle_p = 0
            payout_id = None

        elif 19 <= j <= 21:
            # Orphan settlement
            exp_status = "EXCEPTION"
            exp_cause = "ORPHAN_SETTLEMENT"
            abstain = False
            exp_disp = "AUDIT_ORPHAN_SETTLEMENT"
            method = "credit_card"
            fee_p, tax_p = calc_mdr_fee_and_tax_paise(amount_paise, 200, 1800)
            net_p = amount_paise - (fee_p + tax_p)
            has_o = False
            has_s = True
            settle_p = amount_paise

        elif 22 <= j <= 24:
            # Partial refund
            exp_status = "EXCEPTION"
            exp_cause = "PARTIAL_REFUND_MISMATCH"
            abstain = False
            exp_disp = "INVESTIGATE_PARTIAL_REFUND"
            method = "card"
            fee_p, tax_p = calc_mdr_fee_and_tax_paise(amount_paise, 200, 1800)
            net_p = amount_paise - (fee_p + tax_p)
            has_o = True
            has_s = True
            settle_p = amount_paise - 25000  # ₹250 refund

        elif 25 <= j <= 27:
            # Deliberate Genuine Ambiguity: agent MUST abstain!
            exp_status = "EXCEPTION"
            exp_cause = "GENUINE_AMBIGUITY_INSUFFICIENT_DATA"
            abstain = True
            exp_disp = "ESCALATE_TO_CFO"
            method = "incoherent_pricing_tier"
            # Gross amounts match, but fee has conflicting unmapped deduction
            settle_p = amount_paise
            fee_p = 1234  # Non-standard fee without contract formula
            tax_p = 0
            net_p = amount_paise - fee_p
            has_o = True
            has_s = True

        else:
            # Delayed payout batch allocation
            exp_status = "MATCHED"  # matched at business_tx level
            exp_cause = "CLEAN_TRANSACTION"
            abstain = False
            exp_disp = "RECONCILED"
            method = "upi"
            fee_p, tax_p = calc_mdr_fee_and_tax_paise(amount_paise, 200, 1800)
            net_p = amount_paise - (fee_p + tax_p)
            has_o = True
            has_s = True
            settle_p = amount_paise
            payout_id = f"payout_eval_delayed_{j}"

        oms_rec = None
        if has_o:
            oms_raw = f"order_id:{order_id}:tx:{tx_id}:amt:{amount_paise}"
            oms_rec = {
                "order_id": order_id,
                "business_tx_id": tx_id,
                "amount_paise": amount_paise,
                "currency": "INR",
                "customer_id": customer_id,
                "source_payload_hash": sha256_hash(oms_raw),
                "created_at": created_time,
                "status": "created",
            }

        settle_rec = None
        webhook_rec = None
        if has_s:
            settle_raw = f"pay_id:{payment_id}:tx:{tx_id}:amt:{settle_p}"
            settle_rec = {
                "payment_id": payment_id,
                "business_tx_id": tx_id,
                "order_id": order_id if has_o else f"unrecorded_{order_id}",
                "payout_id": payout_id,
                "amount_paise": settle_p,
                "fee_paise": fee_p,
                "tax_paise": tax_p,
                "net_paise": net_p,
                "currency": "INR",
                "payment_method": method,
                "source_payload_hash": sha256_hash(settle_raw),
                "settled_at": settled_time,
                "status": "settled",
            }
            webhook_raw = f"event_id:{event_id}:pay_id:{payment_id}:amt:{settle_p}"
            webhook_rec = {
                "event_id": event_id,
                "payment_id": payment_id,
                "source_payload_hash": sha256_hash(webhook_raw),
                "payload_json": json.dumps(settle_rec),
                "hmac_signature": "sha256:valid_eval_hmac",
                "received_at": settled_time,
                "status": "processed",
            }

        assoc_ids = []
        if oms_rec:
            assoc_ids.append(oms_rec["order_id"])
        if settle_rec:
            assoc_ids.append(settle_rec["payment_id"])

        manifest["transactions"].append(
            {
                "subject_type": "BUSINESS_TX",
                "subject_id": tx_id,
                "business_tx_id": tx_id,
                "is_held_out": True,
                "expected_reconciliation_status": exp_status,
                "expected_root_cause": exp_cause,
                "payout_relationship": payout_id,
                "should_abstain": abstain,
                "expected_final_disposition": exp_disp,
                "associated_record_ids": assoc_ids,
                "oms_order": oms_rec,
                "settlement": settle_rec,
                "webhook_event": webhook_rec,
            }
        )

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Ground-truth manifest successfully written to: {MANIFEST_PATH}")
    print(f"Total transactions: {len(manifest['transactions'])}")


if __name__ == "__main__":
    build_manifest()
