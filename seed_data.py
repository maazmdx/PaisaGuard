import random
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta
from pathlib import Path
from db import init_db, get_db_cursor, DEFAULT_DB_PATH

TWO_PLACES = Decimal("0.01")
FOUR_PLACES = Decimal("0.0001")

def round_curr(val: Decimal) -> Decimal:
    return val.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

def generate_financial_dataset(db_path: Path = DEFAULT_DB_PATH):
    """
    Seeds database with realistic financial transactions:
    - 100 OMS orders
    - Matching Razorpay settlements with standard 2% MDR + 18% GST
    - Deterministic sub-paise rounding drift (-0.03 INR total)
    - 3 Corporate card fee discrepancies (2.5% fee -> FEE_DEDUCTION exception)
    - 2 Unsettled pending orders
    - 2 Orphan settlements without OMS orders
    - 1 Amount mismatch exception
    - 1 Monthly GST tax invoice with 7.04 INR variance (tax leakage)
    """
    init_db(db_path)
    random.seed(42)

    base_time = datetime(2026, 8, 1, 9, 0, 0)
    
    orders = []
    settlements = []
    
    # Standard amounts to create realistic Indian retail cart values
    sample_amounts = [
        Decimal("499.00"), Decimal("899.00"), Decimal("1299.50"), Decimal("1499.00"),
        Decimal("2499.00"), Decimal("3999.00"), Decimal("5490.00"), Decimal("7999.00"),
        Decimal("999.00"), Decimal("1599.00"), Decimal("4500.00"), Decimal("12500.00")
    ]

    # Pre-determined exception indices
    corporate_fee_indices = {42, 77, 91}  # Charged 2.5% fee instead of 2.0%
    unsettled_indices = {98, 99}           # In OMS, not settled yet
    amount_mismatch_index = 85             # Partial payment mismatch

    sub_paise_accumulated = Decimal("0.00")

    for i in range(1, 101):
        order_id = f"ord_in_{1000 + i}"
        customer_id = f"cust_{2000 + (i % 35)}"
        txn_time = base_time + timedelta(hours=i * 2, minutes=(i * 13) % 60)
        time_str = txn_time.strftime("%Y-%m-%d %H:%M:%S")

        amount = sample_amounts[(i * 7) % len(sample_amounts)]
        
        # Insert OMS Order
        orders.append((
            order_id,
            float(amount),
            "INR",
            customer_id,
            time_str,
            "captured"
        ))

        # Skip unsettled orders
        if i in unsettled_indices:
            continue

        payment_id = f"pay_rzp_{800000 + i}"
        settlement_id = f"setl_aug_{100 + (i // 10)}"
        settled_time = (txn_time + timedelta(days=1, hours=3)).strftime("%Y-%m-%d %H:%M:%S")

        # Check special test cases
        if i in corporate_fee_indices:
            # Corporate card fee: 2.5% + 18% GST (triggers FEE_DEDUCTION exception)
            fee_rate = Decimal("0.025")
            payment_method = "corporate_card"
        else:
            # Standard contracted fee: 2.0% + 18% GST
            fee_rate = Decimal("0.020")
            payment_method = random.choice(["upi", "credit_card", "debit_card", "netbanking"])

        # Calculate MDR Fee
        raw_fee = amount * fee_rate
        exact_fee = round_curr(raw_fee)

        # Calculate 18% GST on fee
        raw_tax = exact_fee * Decimal("0.18")
        exact_tax = round_curr(raw_tax)

        # Track sub-paise rounding variance (difference between raw mathematical sum and rounded sum)
        raw_total_deduction = raw_fee + raw_tax
        rounded_total_deduction = exact_fee + exact_tax
        drift = rounded_total_deduction - raw_total_deduction
        sub_paise_accumulated += drift

        # Amount mismatch case
        settle_amount = amount
        if i == amount_mismatch_index:
            settle_amount = amount - Decimal("500.00")  # e.g., ₹500 partial refund/under-settlement

        net_amount = settle_amount - rounded_total_deduction

        settlements.append((
            payment_id,
            settlement_id,
            order_id,
            float(settle_amount),
            float(exact_fee),
            float(exact_tax),
            float(net_amount),
            "INR",
            payment_method,
            settled_time,
            "settled"
        ))

    # Add 2 orphan settlements (settlements present in Razorpay feed with no OMS order)
    settlements.append((
        "pay_rzp_orphan_901",
        "setl_aug_115",
        "ord_unrecorded_901",
        2500.00,
        50.00,
        9.00,
        2441.00,
        "INR",
        "upi",
        "2026-08-25 14:00:00",
        "settled"
    ))
    settlements.append((
        "pay_rzp_orphan_902",
        "setl_aug_115",
        "ord_unrecorded_902",
        4200.00,
        84.00,
        15.12,
        4100.88,
        "INR",
        "credit_card",
        "2026-08-26 16:30:00",
        "settled"
    ))

    # Insert data into SQLite
    with get_db_cursor(db_path) as cursor:
        cursor.execute("DELETE FROM oms_orders;")
        cursor.execute("DELETE FROM razorpay_settlements;")
        cursor.execute("DELETE FROM resolved_rules;")
        cursor.execute("DELETE FROM gst_monthly_invoices;")
        cursor.execute("DELETE FROM reconciliation_ledger;")

        cursor.executemany("""
            INSERT INTO oms_orders (order_id, gross_amount, currency, customer_id, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?);
        """, orders)

        cursor.executemany("""
            INSERT INTO razorpay_settlements (
                payment_id, settlement_id, order_id, amount, fee, tax, net_amount, currency, payment_method, settled_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, settlements)

        # Calculate exact total daily tax across all settlements
        total_daily_tax = sum(Decimal(str(s[5])) for s in settlements)
        # Seed Monthly GST Invoice from Razorpay with exact 7.04 INR variance (over-deduction)
        monthly_total_gst = total_daily_tax - Decimal("7.04")
        monthly_taxable = round_curr(monthly_total_gst / Decimal("0.18"))
        half_gst = round_curr(monthly_total_gst / Decimal("2.0"))

        cursor.execute("""
            INSERT INTO gst_monthly_invoices (
                invoice_id, month, total_taxable_value, cgst, sgst, igst, total_gst, invoice_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            "INV-RZP-2026-08-9941",
            "2026-08",
            float(monthly_taxable),
            float(half_gst),
            float(half_gst),
            0.00,
            float(monthly_total_gst),
            "2026-09-01"
        ))

    print(f"Dataset generated successfully in {db_path}!")
    print(f"  - Total OMS Orders: {len(orders)}")
    print(f"  - Total Razorpay Settlements: {len(settlements)}")
    print(f"  - Sub-paise Accumulated Drift: {float(sub_paise_accumulated):.4f} INR")

if __name__ == "__main__":
    generate_financial_dataset()
