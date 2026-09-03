# Contributing to PaisaGuard

Thank you for your interest in contributing to **PaisaGuard: Deterministic Multi-Source Financial Reconciliation Engine**!

PaisaGuard is built with extreme engineering rigor. Any contributions to core accounting, webhook ingestion, or concurrency control must adhere to the following standards.

---

## 1. Core Engineering Principles

1. **Deterministic Boundaries**: Financial ledgers and mathematical audits must never be mutated by probabilistic AI models or non-deterministic embeddings.
2. **Exact Monetary Math**: Always use Python's `decimal.Decimal` with explicit rounding modes (`ROUND_HALF_UP`) and sub-paise accumulators for currency calculation. Never use raw floating-point types (`float`) for ledger arithmetic.
3. **Lock-Free Concurrency**: All SQLite access must adhere to Write-Ahead Logging (`PRAGMA journal_mode = WAL`), synchronous `NORMAL`, and atomic relational upserts (`ON CONFLICT DO UPDATE`).
4. **Idempotency**: All webhook ingestion paths must enforce deduplication on `payment_id` to guarantee At-Most-Once transaction processing.

---

## 2. Local Development Setup

```bash
# 1. Clone repository
git clone https://github.com/maazmdx/PaisaGuard.git
cd PaisaGuard

# 2. Initialize virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies from PyPI
pip install --upgrade pip
pip install -r requirements.txt

# 4. Seed SQLite database in WAL mode
python seed_data.py
```

---

## 3. Running Verification Suites

Before submitting any Pull Request, ensure that all automated verification suites pass with zero warnings:

```bash
# Run full Pytest harness (18+ tests)
pytest -v

# Run deterministic unit tests
python -m unittest test_reconciliation.py

# Run adversarial multi-threaded concurrency benchmark
python concurrency_tester.py

# Verify end-to-end reconciliation report generation
python recon_engine.py
```

---

## 4. Pull Request Checklist

- [ ] All unit and integration tests pass cleanly (`pytest -v`).
- [ ] No local wheels or unverified dependencies added to `requirements.txt`.
- [ ] Any modifications to schema are reflected in `schema.sql` and `db.py`.
- [ ] All new webhook endpoints support both Hex and Base64 HMAC verification.
- [ ] Code compiles without syntax warnings (`python -m py_compile *.py`).
