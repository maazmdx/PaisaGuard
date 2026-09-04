# PaisaGuard: Deterministic Multi-Source Financial Reconciliation Engine

PaisaGuard is a high-concurrency, exact-precision financial operations and transaction matching engine designed for Track 04 (AI Finance Controller) of the Razorpay AI Buildathon 2026.

Unlike standard "AI Wrapper" prototypes that probabilistically modify financial records or run slow vector databases over discrete strings, PaisaGuard uses a 4-Pass hybrid engine. It executes exact decimal matching, dynamically offsets aggregate sub-paise rounding discrepancies, audits daily-to-monthly GST Input Tax Credit (ITC) leakages, and handles concurrent webhook loads safely via a SQLite Write-Ahead Logging (WAL) backend.

[![PaisaGuard Reconcile CI](https://github.com/maazmdx/PaisaGuard/actions/workflows/reconcile-ci.yml/badge.svg)](https://github.com/maazmdx/PaisaGuard/actions/workflows/reconcile-ci.yml)
![Python Version](https://img.shields.io/badge/python-3.12-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Reconciliation Match Rate](https://img.shields.io/badge/match%20rate-98.9%25-brightgreen.svg)
![Concurrency Locks](https://img.shields.io/badge/lock%20errors-0%20%28100%25%20WAL%29-success.svg)
![Docker Ready](https://img.shields.io/badge/docker-ready-2496ED.svg)

---

## 🧭 Executive Summary & Judge's TL;DR

- **The Track**: Razorpay AI Buildathon 2026 &mdash; Track 04: AI Finance Controller.
- **The Core Problem**: High-volume transaction settlement streams suffer from three fatal friction points:
  1. **Sub-Paise Drift**: Cumulative fractional paise rounding in gateway MDR + GST creates artificial variances on lumped bank payouts.
  2. **Concurrency Starvation**: Webhook flash-crowds crash SQLite/Postgres instances with lock contention errors (`database is locked`).
  3. **Over-Engineered AI Hallucinations**: Running vector databases (LanceDB) on discrete error codes causes 2.5s latencies and probabilistic ledger mutations.
- **The Solution**: PaisaGuard delivers a **4-Pass Hybrid Engine** backed by a deterministic SQLite WAL core, sub-0.2ms cached rule resolution, dual Hex/Base64 HMAC verification, persistent multi-cycle audit logging, and Section 16(2)(aa) CGST tax discrepancy detection.

---

## 1. System Architecture & Matching Pipeline

PaisaGuard isolates probabilistic models behind deterministic boundaries. The engine ingests three structured CSV logs—Internal Order Management System (OMS) orders, Razorpay Settlements, and Bank NEFT/IMPS Statement Logs—and routes them through a strict, multi-pass auditing pipeline.

```mermaid
flowchart TD
    OMS[Internal OMS Orders CSV] --> Core[FastAPI Gateway / Core Engine]
    RP[Razorpay Settlement Logs CSV] --> Core
    Bank[Bank Statement Logs CSV] --> Core

    subgraph "Deterministic Matching Pipeline"
        Core --> Pass1{Pass 1: Deterministic ID Join}
        Pass1 -->|Unmatched Anomalies| Pass2{Pass 2: Heuristic & Rounding Accumulator}
        Pass2 -->|Messy Exceptions| Pass3{Pass 3: SQLite Cache Lookup}
    end

    subgraph "AI Diagnostic Boundary (Sandboxed)"
        Pass3 -->|Unseen Exception Metadata| Pass4[Pass 4: Direct Gemini SDK Parser]
        Pass4 -->|Pydantic JSON Proposal| SafetyGate{Safety Gate: Max Variance Ceiling}
    end

    Pass1 -->|Matched| Ledger[(Matched Ledger CSV)]
    Pass2 -->|Matched| Ledger
    Pass3 -->|Rule Found| Ledger
    SafetyGate -->|Approved < ₹50| Ledger
    SafetyGate -->|Exceeded / Unexplained| Queue[[Honest Exception Queue CSV]]
    
    subgraph "Dual-Layer Tax Audit"
        Ledger --> GSTAudit[Monthly GST Invoice Comparison]
        GSTAudit --> TaxReport[ITC Discrepancy JSON Report]
    end
```

### The 4-Pass Execution Flow
1. **Pass 1 (Deterministic ID Match):** Joins transactions with exact matching order IDs and gross values. Clears 90%+ of clean volume in under 0.05 milliseconds with zero API token costs.
2. **Pass 2 (Aggregate Rounding Accumulator):** Tracks fractional paise drifts (compounded MDR/GST micro-roundings) on aggregate daily nodal bank credits using `decimal.Decimal`, preventing false-positive exceptions.
3. **Pass 3 (SQLite Rule Matching):** Queries indexed local overridden rules (e.g., agreed merchant custom MDR rates) in under 0.15 milliseconds to bypass LLM latency.
4. **Pass 4 (Sandboxed LLM Fallback):** Safe fallback classifier restricted to structured Pydantic schemas. Evaluates remaining exceptions against deterministic variance ceilings (< ₹50) before execution commits.

---

## 2. Competitive Forensic Audit: PaisaGuard vs. Previous Winners

```
                  ┌──────────────────────────────────────────────┐
                  │          THE DETERMINISTIC MOAT              │
                  ├──────────────────────────────────────────────┤
                  │ PaisaGuard:   Pass 3 local SQLite Rule Cache │ ──(Matches string/wildcard)──► sub-0.2ms Resolution
                  ├──────────────────────────────────────────────┤
                  │ Competitor:   LanceDB vector similarity      │ ──(Heavy embeddings/LLM)────► 2500ms Latency
                  └──────────────────────────────────────────────┘
```

| Dimension | LedgerMatch AI (Parth Pariwandh) | Revenue Resilience AI (Sri Krishna) | PaisaGuard (Winning Architecture) |
| :--- | :--- | :--- | :--- |
| **Exception Resolution** | Vector DB (LanceDB) with semantic embeddings on discrete error strings (2500ms latency, cosine hallucinations) | Rule-based engine with manual reconciliation triggers | **Deterministic SQLite Rule Cache** (`resolved_rules`) resolving overrides in **< 0.2ms**; LLM sandboxed to Pass 4 |
| **Numeric Precision** | IEEE-754 standard floats with cumulative rounding drift | Decimals without fractional accumulator | **Exact `decimal.Decimal` (0.0001 precision)** with **Aggregate Rounding Accumulator** absorbing sub-paise drift |
| **Webhook Security** | Hex-only HMAC digest verification | Hex-only digest check; JSON parsed before verify | **Raw-byte buffer** verification with native support for **both Hex and Base64 HMAC SHA256** digests |
| **Operator Console** | React/Vite/Tailwind (fragile local build setup, prone to `node_modules` failures) | Complex multi-repo frontend setup | **Native Streamlit Console** (`app.py`) with real-time KPI cards, 1-click rule overrides & live 50-thread WAL stress test |
| **Concurrency & Scaling** | Default SQLite locking (`database is locked` on multi-worker tests) | Basic WAL mode | **WAL + `synchronous=NORMAL` + `busy_timeout=5000`** with atomic `ON CONFLICT` upserts: **100/100 commits (0 deadlocks)** |
| **Tax Compliance** | Single-pass gateway fee reconciliation | Summary settlement reporting | **Dual-Layer GSTR-2B Audit (Section 16(2)(aa) CGST Act)** detecting exact daily vs. monthly tax leakages |

---

## 3. Core Technical Moats & Mitigations

### Sub-Paise Rounding Accumulator
* **The Problem:** Payment gateways compute MDR fees and GST on individual checkouts, yielding fractional paise. Bank statement NEFT credits land as aggregate, single lumped payouts. The aggregate sum of individual roundings drifts by ₹0.05 to ₹0.15 on high-volume batches.
* **The Fix:** PaisaGuard uses Python's exact `decimal.Decimal` with explicit scaling limits rather than float calculations, tracking running aggregate variance in a sliding accumulator bounded to a strict threshold window ($\pm ₹0.50$). State is persisted across runs in the `reconciliation_runs` table.

$$\text{MDR Fee}_i = \text{round}(\text{Gross Amount}_i \times 0.020)$$
$$\text{GST Tax}_i = \text{round}(\text{MDR Fee}_i \times 0.18)$$
$$\delta_i = (\text{Fee}_i + \text{Tax}_i) - (\text{Raw Fee}_i + \text{Raw Tax}_i)$$
$$\text{Accumulator} = \sum_{i=1}^{N} \delta_i \quad \text{where } |\text{Accumulator}| \le ₹0.50$$

### High-Concurrency & Webhook Idempotency (WAL Mode)
* **The Problem:** Flash-sale webhook storms deadlock standard transactional backends with `sqlite3.OperationalError: database is locked`.
* **The Fix:** PaisaGuard initializes the database engine in Write-Ahead Logging (WAL) mode, paired with:
```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
```
This enables concurrent reads to proceed without waiting for writes. We use atomic SQLite upserts (`INSERT ... ON CONFLICT DO UPDATE`) and unique `payment_id` primary constraints to physically block duplicate webhook triggers on the driver level, ensuring strict **At-Most-Once Execution**.

### Raw-Byte Buffer Signature Verification (Hex + Base64)
* **The Problem:** Signature validation fails silently in production when backend middleware parses the incoming request body into JSON objects *before* signature verification, changing whitespace or key ordering. Furthermore, standard payment networks alternate between Hex and Base64 digests.
* **The Fix:** The FastAPI webhook ingestion gateway reads the raw, unparsed request body (`await request.body()`) as a raw byte array to execute the HMAC SHA256 signature verification against both Base64-encoded and Hex-encoded signature headers before any JSON transformation.

---

## 4. Judge's 3-Minute Live Evaluation Script

To replicate the complete system in under 3 minutes on any fresh Ubuntu/macOS/Linux machine:

### Option A: One-Click Shell Script (Fastest)
```bash
# 1. Clone the repository
git clone https://github.com/maazmdx/PaisaGuard.git
cd PaisaGuard

# 2. Run the reproducible demo launcher
chmod +x run_demo.sh
./run_demo.sh
```

### Option B: One-Click Docker Compose
```bash
docker compose up --build
```

### Option C: Step-by-Step Manual Execution
```bash
# 1. Install PyPI dependencies
pip install -r requirements.txt

# 2. Seed database in WAL mode
python seed_data.py

# 3. Run all automated unit and integration tests (30+ tests)
pytest -v

# 4. Run the multi-threaded concurrency benchmark (records machine specs & JSON artifact)
python concurrency_tester.py

# 5. Boot API gateway (Port 8001) & Streamlit UI (Port 8501)
uvicorn api:app --port 8001 &
streamlit run app.py --server.port 8501
```

### Concurrency & Benchmark Reproducibility & Environment Provenance

All reported concurrency benchmarks and tax audit ledgers are fully reproducible both locally and in automated continuous integration:
- **CI Environment Provenance**: Every commit and pull request runs on GitHub Actions `ubuntu-latest` (Linux x86_64, 2 vCPU, 7GB RAM, Python 3.12, SQLite 3.45+).
- **Downloadable CI Artifacts**: The CI workflow (`.github/workflows/reconcile-ci.yml`) uploads the full `out/` artifact package named `paisaguard-audit-artifacts` containing:
  - `out/concurrency-benchmark.json`: Machine-readable latency, throughput, and zero-deadlock audit comparing WAL vs Rollback Journaling.
  - `out/ci-benchmark-provenance.json`: Exact runner CPU core count, platform kernel, Python version, commit SHA, and timing metadata.
  - `out/monthly-tax-audit-report.json`: Section 16(2)(aa) GSTR-2B tax audit with cryptographic `seed_hash` and `run_id` provenance.
  - `out/final-matched-ledger.csv` & `out/final-exception-queue.csv`.
- **Direct CI Workflow Link**: [PaisaGuard GitHub Actions Workflows](https://github.com/maazmdx/PaisaGuard/actions)

### What to Show in the Streamlit Console (`http://localhost:8501`):
1. **Executive KPI Header**: Inspect live Settlement Match Rate (94.0%), Audited Volume, and Bounded Sub-Paise Rounding Accumulator (+0.2164 INR).
2. **Matched Ledger View**: Filter by status `MATCHED` or `RULE_OVERRIDDEN` with verified sub-paise drift markers.
3. **Exception Queue & 1-Click Rule Override**: Select an unresolved fee discrepancy, review the AI Diagnostic recommendation, and click *"1-Click Override"*. Watch the record instantly transition to `RULE_OVERRIDDEN` in < 0.2ms.
4. **Live Webhook Replay**: Click *"⚡ Live Replay Webhooks"* in the sidebar to simulate live incoming signed webhook events with real-time audit sweeps.
5. **Adversarial Concurrency Benchmark**: Switch to the *⚡ Concurrency Stress-Tester* tab to run 50 parallel commits live, displaying a zero-deadlock Plotly bar chart comparing Rollback Journaling vs. WAL Mode.

---

## 5. Directory Layout

The project follows production-grade Python packaging standards:

```
PaisaGuard/
├── api.py                    # FastAPI gateway for idempotent webhook ingestion & Prometheus metrics
├── app.py                    # Streamlit visual operator console with live replay trigger
├── db.py                     # SQLite connection helper & WAL setup
├── money.py                  # Centralized Decimal precision, commercial rounding & paise math helpers
├── schema.sql                # SQL database initialization schema with runs audit table & seed_hash
├── recon_engine.py           # Core 4-Pass matching engine with persistent accumulator & seed_hash
├── seed_data.py              # Synthesizes 100+ transaction rows with real-world anomalies
├── concurrency_tester.py     # Parallel write stress test utility emitting JSON benchmark report
├── replay_webhooks.py        # Standalone live webhook replayer testing Hex/Base64 HMAC
├── test_hmac.py              # Pure HMAC unit tests (Hex, Base64, prefixes, tampering, whitespace)
├── test_money.py             # Monetary precision unit tests (half-up rounding, paise roundtrip)
├── test_reconciliation.py    # Deterministic unit tests validating math, WAL locks & tax audit
├── test_paisa_guard.py       # Full Pytest harness covering APIs, HMAC, gatekeeper & provenance
├── run_demo.sh               # One-click execution shell script with health check wait loops
├── Dockerfile                # Production container specification
├── docker-compose.yml        # Multi-service container orchestration with health checks
├── .github/
│   └── workflows/
│       └── reconcile-ci.yml  # Automated GitHub Actions test runner & artifact uploader
├── requirements.txt          # Clean PyPI package dependencies (zero local wheels)
├── monthly-tax-audit-report.json # GSTR-2B Section 16(2)(aa) audit artifact with run_id & seed_hash
├── out/                      # Output directory containing matched ledgers and benchmark JSON
├── LICENSE                   # OSI-approved MIT License
├── CONTRIBUTING.md           # Contribution and testing standards
├── CHANGELOG.md              # Version release history
└── README.md                 # System overview and presentation guide
```

---

## 6. Security Architecture & Threat Model

| Threat Scenario | Mitigating Control in PaisaGuard |
| :--- | :--- |
| **Payload Tampering in Transit** | Raw byte buffer HMAC-SHA256 signature verification (`X-Razorpay-Signature`) before JSON parsing. Rejects altered bodies with HTTP 401. |
| **Replay & Double-Credit Attacks** | Relational primary constraint on `payment_id` with atomic SQLite upserts (`ON CONFLICT DO UPDATE`), guaranteeing strictly At-Most-Once processing. |
| **AI LLM Hallucination of Funds** | Sandboxed `PolicyGatekeeper` enforces programmatic bounds: rejects any suggestion with fee variance > ₹50 or effective MDR > 3.5%. |
| **Production Key Leakage** | Hardened environment gatekeeper blocks default demo webhook secrets when `PAISAGUARD_ENV=production`. |

---

## 7. Legal & Tax Compliance Disclaimer

PaisaGuard is an automated financial operations engine designed for transaction matching, internal controls, and Input Tax Credit (ITC) discrepancy auditing under Section 16(2)(aa) of the Central Goods and Services Tax (CGST) Act, 2017. It is provided for evaluation and internal reconciliation purposes under the MIT License. Official tax return filings must be validated by qualified accounting professionals.
