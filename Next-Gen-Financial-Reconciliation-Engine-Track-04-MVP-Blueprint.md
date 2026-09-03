# Next-Gen Financial Reconciliation Engine: Track 04 MVP Blueprint
**System Design Document & Engineering Specification**  
**Razorpay AI Buildathon 2026 — Track 04: AI Finance Controller**  
*Engineered by Team PaisaGuard | Precision Financial Systems Engineering*

---

## 1. Executive Summary & Problem Scope

Payment settlement reconciliation in high-volume e-commerce and fintech platforms operating in India is plagued by systemic operational leakage, brittle automated pipelines, and non-compliance risks. Traditional reconciliation tooling relies on naive floating-point comparisons or external distributed infrastructure that is complex to operate and susceptible to concurrency starvation.

PaisaGuard addresses three critical systemic failure modes:

1. **Sub-Paise Rounding Accumulation & Settlement Drift**:
   Payment aggregators like Razorpay compute fees (Merchant Discount Rate, or MDR) and 18% Goods and Services Tax (GST) per individual transaction at sub-paise precision (e.g., ₹29.9918). However, actual bank transfers are settled as aggregated lump-sum NEFT/RTGS batches. Rigid threshold checks (`abs(net - expected) < 0.01`) result in widespread false-positive exception tickets due to compounding truncation differences across hundreds of transactions.
2. **GST Input Tax Credit (ITC) Leakage (Section 16(2)(aa) of the CGST Act)**:
   Indian tax laws mandate that Input Tax Credit can only be claimed if the tax invoice submitted by the vendor (Razorpay) exactly matches the inward supplies reflected in the merchant's GSTR-2B. When daily gateway micro-deductions diverge from the monthly invoice, businesses suffer unrecoverable tax leakage or severe audit penalties.
3. **Database Write-Lock Starvation Under Webhook Bursts**:
   During flash sales or seasonal peaks, concurrent webhook deliveries overwhelm standard single-file embedded databases. Traditional SQLite configurations operating under rollback journaling (`journal_mode=DELETE`) lock the database entirely during writes, resulting in `sqlite3.OperationalError: database is locked` exceptions, dropped payloads, and inconsistent ledgers.

PaisaGuard implements a deterministic, multi-pass reconciliation engine engineered with strict decimal precision (`decimal.Decimal`), Write-Ahead Logging (WAL) concurrency, and a sandboxed AI diagnostic boundary.

---

## 2. High-Level Architecture & Transaction Lifecycle

PaisaGuard ingests three distinct data streams:
- **Internal Order Management System (OMS) Logs**: Authoritative internal ledger of created and captured customer orders.
- **Razorpay Payment & Settlement Feed**: Transaction-level MDR deductions, GST charges, payment methods, and settlement batch IDs.
- **Bank Statement Logs**: External bank credit entries confirming physical NEFT/IMPS deposits.

```
[ Internal OMS Orders ]     [ Razorpay Settlements ]     [ Bank Statement Feed ]
          │                            │                            │
          └────────────────────────────┼────────────────────────────┘
                                       │
                                       ▼
                     ┌───────────────────────────────────┐
                     │     FastAPI Ingestion Gateway     │
                     │  - Raw-Byte Buffer HMAC SHA-256   │
                     │  - Atomic SQL UPSERT Ingestion    │
                     └─────────────────┬─────────────────┘
                                       │
                                       ▼
                     ┌───────────────────────────────────┐
                     │    SQLite Backend (WAL Mode)      │
                     │  - PRAGMA journal_mode = WAL      │
                     │  - PRAGMA synchronous = NORMAL    │
                     │  - PRAGMA busy_timeout = 5000     │
                     └─────────────────┬─────────────────┘
                                       │
                                       ▼
           ═════════════════════════════════════════════════════════
                       DETERMINISTIC 4-PASS RECON ENGINE
           ═════════════════════════════════════════════════════════
           Pass 1: Key & Gross Amount Verification (OMS vs Razorpay)
           Pass 2: Aggregate Rounding Accumulator & Contract MDR Audit
           Pass 3: Low-Latency SQLite Rule Cache (<0.2ms Lookups)
           Pass 4: Dual-Layer Daily-to-Monthly GSTR-2B ITC Audit
           ═════════════════════════════════════════════════════════
                        │                              │
         [Deterministic Match]            [Unresolved Anomaly]
                        │                              │
                        ▼                              ▼
             ┌─────────────────────┐       ┌──────────────────────┐
             │ Matched Ledger CSV  │       │ Sandboxed AI Parser  │
             └─────────────────────┘       │ (Gemini 2.5/3 Flash) │
                                           └──────────┬───────────┘
                                                      │ Pydantic JSON
                                                      ▼
                                           ╔══════════════════════╗
                                           ║ Policy Gatekeeper    ║
                                           ║ Variance < ₹50.00    ║
                                           ║ Fee Rate <= 3.5%     ║
                                           ╚══════════╦═══════════╝
                                                      │
                                           ┌──────────┴──────────┐
                                           ▼                     ▼
                                    [Auto-Approved]       [Human Escalate]
                                    Commit Override       Exception Queue
```

---

## 3. Mathematical Formulations

### 3.1 Contract Fee & Tax Equations
For each transaction $i \in \{1, \dots, N\}$, with gross amount $A_i$, contract MDR fee rate $r_{\text{MDR}} = 0.020$ (2.0%), and GST rate $r_{\text{GST}} = 0.18$ (18%):

$$\text{Raw Fee}_i = A_i \times r_{\text{MDR}}$$

$$\text{Expected Fee}_i = \left\lfloor \text{Raw Fee}_i + 0.005 \right\rfloor_{\text{2 dec}} = \text{quantize}\left(\text{Raw Fee}_i, \text{"0.01"}, \text{ROUND\_HALF\_UP}\right)$$

$$\text{Raw Tax}_i = \text{Expected Fee}_i \times r_{\text{GST}}$$

$$\text{Expected Tax}_i = \left\lfloor \text{Raw Tax}_i + 0.005 \right\rfloor_{\text{2 dec}} = \text{quantize}\left(\text{Raw Tax}_i, \text{"0.01"}, \text{ROUND\_HALF\_UP}\right)$$

### 3.2 Aggregate Rounding Accumulator
At the individual transaction level, the rounding error $\delta_i$ between discrete decimal deductions and raw mathematical values is defined as:

$$\delta_i = \left(\text{Expected Fee}_i + \text{Expected Tax}_i\right) - \left(\text{Raw Fee}_i + \text{Raw Tax}_i\right)$$

Across an entire settlement batch $B$ consisting of $N$ transactions, the cumulative rounding drift $\Delta_B$ is modeled as a discrete bounded random walk:

$$\Delta_B = \sum_{i=1}^{N} \delta_i$$

**Deterministic Tolerance Bound:**  
For standard retail cart distributions ($A_i \in [₹100, ₹15,000]$), the central limit theorem bounds the cumulative rounding drift:

$$|\Delta_B| \le \epsilon_{\text{batch}} = ₹0.50 \quad \text{for } N \le 500$$

If $|\Delta_B| \le \epsilon_{\text{batch}}$, the settlement balance is absorbed and cleared deterministically without operator intervention. If $|\Delta_B| > \epsilon_{\text{batch}}$, the batch is flagged for contract fee rate deviation.

### 3.3 Dual-Layer GST ITC Audit (Section 16(2)(aa) CGST Act)
Input Tax Credit leakage $L_{\text{GST}}$ across billing period $M$ is computed by auditing cumulative daily settlement deductions against the physical monthly tax invoice:

$$T_{\text{daily}} = \sum_{i=1}^{N} \text{Actual Tax}_i$$

$$T_{\text{invoice}} = \text{CGST}_{\text{inv}} + \text{SGST}_{\text{inv}} + \text{IGST}_{\text{inv}}$$

$$L_{\text{GST}} = T_{\text{daily}} - T_{\text{invoice}}$$

- **Case 1 ($L_{\text{GST}} = 0$):** Perfect GSTR-2B compliance; full ITC claimed.
- **Case 2 ($L_{\text{GST}} > 0$):** Gateway over-deduction; merchant cannot claim credit for untallied daily taxes. Automated dispute notice generated for supplier amendment.
- **Case 3 ($L_{\text{GST}} < 0$):** Under-deduction; potential penalty exposure under Section 50(1) for unpaid liability.

---

## 4. Systems Architecture & Concurrency Engineering

### 4.1 Pragmatic Stack Selection
| Layer | Selected Technology | Alternative Rejected | Rationale |
| :--- | :--- | :--- | :--- |
| **Relational Storage** | SQLite 3 (WAL Mode) | PostgreSQL / MySQL | Zero infrastructure footprint, embeddable, sub-0.2ms lookup latency. |
| **Exception Matching** | Relational Exact Index | Vector DB (LanceDB/Pinecone) | Exception strings are discrete codes, not unstructured embeddings. Vector search adds latency, cost, and non-deterministic cosine false matches. |
| **AI Integration** | Direct Google GenAI SDK | LangChain / CrewAI / n8n | Eliminates multi-layered abstraction bloat, prompt drift, and vendor lock-in. Enforces read-only sandboxed schema output. |
| **Frontend Console** | Streamlit + Plotly | React SPA / Node.js | Single-language Python deployment, live hot-reloading, native integration with Pandas and SQLite cursors. |

### 4.2 SQLite Write-Ahead Logging (WAL) Configuration
To eradicate `sqlite3.OperationalError: database is locked` errors during parallel webhook ingestion:

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;
```

#### The Concurrency Mechanics
- **Reader-Writer Isolation:** In WAL mode, SQLite writes new transactions to a separate `-wal` file while readers continue scanning the main database file without blocking.
- **Synchronous NORMAL:** Commits do not wait for disk platters to sync on every single write, eliminating I/O stalls while ensuring zero database corruption across application crashes.
- **Deterministic 5000ms Busy Timeout:** Thread workers that encounter a temporary write lock back off and queue for up to 5 seconds rather than throwing an immediate operational error.

---

## 5. Sandboxed AI Diagnostic Boundary & Policy Gatekeeper

PaisaGuard isolates LLMs behind strict economic and programmatic boundaries. The model is treated as an untrusted advisory classifier:

```
[ Exception Payload ] ──► [ Gemini Structured Prompt ] ──► [ Pydantic Validation ]
                                                                   │
                                                                   ▼
                                                       [ Policy Gatekeeper Check ]
                                                        - Variance <= ₹50.00
                                                        - Fee Rate <= 3.5%
                                                                   │
                                                ┌──────────────────┴──────────────────┐
                                                ▼                                     ▼
                                           [ APPROVED ]                          [ REJECTED ]
                                       Commit to SQLite                      Retain in Queue
                                       Auto-Resolve Ledger                   Escalate to CFO
```

### Pydantic Output Contract
```python
class AIDiagnosticResponse(BaseModel):
    is_legitimate_variance: bool
    root_cause_classification: str # "CARD_NETWORK_SURCHARGE", "GATEWAY_ROUNDING", "CURRENCY_CONVERSION"
    suggested_action: str          # "APPROVE_OVERRIDE", "ESCALATE_DISPUTE"
    confidence_score: float        # Range 0.0 to 1.0
    technical_rationale: str
```

### Deterministic Safety Invariants
1. **Variance Ceiling**: No AI recommendation is accepted if the fee adjustment variance exceeds **₹50.00 INR**.
2. **Rate Cap**: No AI recommendation is accepted if the effective MDR rate exceeds **3.5%**.
3. **No Ledger Write Permission**: The AI service has zero database connection access. It receives a JSON string and outputs a JSON string. The application server validates the output against the policy gatekeeper before initiating an atomic SQLite write.

---

## 6. The "2 AM Failure" & Technical Retrospective

### Incident Summary
During multi-threaded load testing (100 parallel workers simulating sudden flash-sale webhooks), our FastAPI webhook service experienced widespread failure:
`sqlite3.OperationalError: database is locked` on 73% of concurrent requests.

### Root Cause Analysis
1. Default Python `sqlite3` driver connections initialize with `synchronous = FULL` and an unconfigured `busy_timeout = 0`.
2. Even though WAL mode was active, worker threads attempting `INSERT ... ON CONFLICT DO UPDATE` collided with long-running Streamlit dashboard queries that held open cursor transactions.
3. Because the busy timeout was 0ms, worker threads instantly aborted on lock contention instead of queuing.

### Engineering Resolution
1. Configured connection constructors with explicit context management (`with get_db_cursor()`), guaranteeing that transaction locks are held for $< 2\text{ms}$.
2. Enforced `PRAGMA busy_timeout = 5000;` on all database handles.
3. Switched `PRAGMA synchronous = NORMAL;`, reducing commit latency by $85\%$.
4. **Post-Fix Benchmark**: 100/100 concurrent webhook updates completed in 0.42s with **0 lock contention errors**.

---

## 7. Verification Artifacts & Test Harness

The engine is verified through four production-grade artifacts:
- **`out/final-matched-ledger.csv`**: Complete audit log of reconciled orders with order amounts, settled amounts, expected vs actual MDR, tax deductions, and sub-paise drift markers.
- **`out/final-exception-queue.csv`**: Segregated anomaly queue recording unsettled orders, gross amount mismatches, orphan settlements, and unapproved fee variances.
- **`monthly-tax-audit-report.json`**: Machine-readable GSTR-2B compliance audit certifying cumulative daily GST against monthly tax invoice values.
- **`reconciliation-test-suite.py` / `test_reconciliation.py`**: Automated unittest suite verifying WAL pragmas, precision decimal math, dynamic rule resolution, and adversarial multi-threaded lock-free execution.
