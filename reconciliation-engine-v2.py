#!/usr/bin/env python3
"""
PaisaGuard: Reconciliation Engine v2
Track 04: AI Finance Controller - Razorpay AI Buildathon 2026

Deterministic Multi-Source Financial Reconciliation Engine featuring:
- Pass 1: Deterministic Transaction ID & Gross Value Join (OMS vs Razorpay)
- Pass 2: Aggregate Rounding Accumulator for sub-paise drift absorption
- Pass 3: Low-latency SQLite Rule Engine for pre-computed exception overrides
- Pass 4: Daily-to-Monthly GST ITC Safeguard Audit (Section 16(2)(aa) CGST Act)
"""

import sys
import argparse
from pathlib import Path
from decimal import Decimal

# Ensure local imports resolve
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from recon_engine import execute_reconciliation_pipeline
from db import DEFAULT_DB_PATH

def main():
    parser = argparse.ArgumentParser(description="PaisaGuard Reconciliation Engine v2")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB_PATH), help="Path to SQLite database")
    parser.add_argument("--quiet", action="store_true", help="Suppress verbose stdout logs")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: Database file not found at {db_path}. Please run seed_data.py first.")
        sys.exit(1)

    summary = execute_reconciliation_pipeline(db_path=db_path)

    if not args.quiet:
        print("=" * 65)
        print("  PAISAGUARD RECONCILIATION ENGINE v2: EXECUTION REPORT")
        print("=" * 65)
        print(f"  Audited Settlement Records : {summary['total_audited']}")
        print(f"  Deterministic Matches      : {summary['matched_count']}")
        print(f"  Exceptions Flagged         : {summary['exception_count']}")
        print(f"  Overall Match Rate         : {summary['match_rate']}%")
        print(f"  Aggregate Rounding Drift   : ₹{summary['sub_paise_accumulator']:.4f}")
        print(f"  Daily Gateway GST Sum      : ₹{summary['gst_daily_aggregate']:.2f}")
        print(f"  Monthly Tax Invoice GST    : ₹{summary['gst_monthly_invoice']:.2f}")
        print(f"  Identified Tax Leakage     : ₹{summary['gst_tax_leakage']:.2f} (GSTR-2B Over-Deduction)")
        print("-" * 65)
        print(f"  Matched Ledger Output      : {summary['matched_csv']}")
        print(f"  Exception Queue Output     : {summary['exception_csv']}")
        print("=" * 65)

if __name__ == "__main__":
    main()
