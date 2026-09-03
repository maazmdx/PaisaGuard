#!/usr/bin/env python3
"""
PaisaGuard: Automated Concurrency & Precision Test Suite
Track 04: AI Finance Controller - Razorpay AI Buildathon 2026

Evaluates:
1. Multi-threaded SQLite write lock concurrency (100 parallel workers in WAL vs Rollback)
2. Sub-paise decimal precision & Aggregate Rounding Accumulator math
3. 1-click dynamic exception override mechanics
4. GSTR-2B compliance & GST ITC audit verification
"""

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from test_reconciliation import TestReconciliationEngine
from concurrency_tester import simulate_webhook_flood

def run_precision_unit_tests() -> bool:
    print("=" * 65)
    print("  PHASE 1: DETERMINISTIC PRECISION & COMPLIANCE UNIT TESTS")
    print("=" * 65)
    suite = unittest.TestLoader().loadTestsFromTestCase(TestReconciliationEngine)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()

def run_adversarial_concurrency_stress():
    print("\n" + "=" * 65)
    print("  PHASE 2: ADVERSARIAL MULTI-THREADED CONCURRENCY BENCHMARK")
    print("  Simulating 100 concurrent webhook workers targeting SQLite...")
    print("=" * 65)

    benchmark_db = BASE_DIR / "concurrency_benchmark.db"

    print("\n[Step 1] Traditional Rollback Journaling Mode (DELETE)...")
    res_rollback = simulate_webhook_flood(benchmark_db, num_threads=100, use_wal=False)
    print(f"  Mode                : {res_rollback['mode']}")
    print(f"  Successful Commits  : {res_rollback['successful_commits']} / 100")
    print(f"  Database Locks      : {res_rollback['lock_errors']} contention errors")
    print(f"  Throughput          : {res_rollback['throughput_tps']} txns/sec")

    print("\n[Step 2] Upgraded SQLite WAL Mode (Write-Ahead Logging)...")
    res_wal = simulate_webhook_flood(benchmark_db, num_threads=100, use_wal=True)
    print(f"  Mode                : {res_wal['mode']}")
    print(f"  Successful Commits  : {res_wal['successful_commits']} / 100")
    print(f"  Database Locks      : {res_wal['lock_errors']} contention errors")
    print(f"  Throughput          : {res_wal['throughput_tps']} txns/sec")

    print("-" * 65)
    if res_wal['lock_errors'] == 0 and res_wal['successful_commits'] == 100:
        print("  VERIFICATION RESULT: 100% LOCK-FREE CONCURRENCY CONFIRMED (0 DEADLOCKS)")
    else:
        print("  VERIFICATION RESULT: CONCURRENCY LOCK DETECTED")
    print("=" * 65)

def main():
    success = run_precision_unit_tests()
    if not success:
        print("\nUnit tests failed! Aborting concurrency benchmark.")
        sys.exit(1)
    run_adversarial_concurrency_stress()

if __name__ == "__main__":
    main()
