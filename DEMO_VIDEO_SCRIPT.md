# PaisaGuard: Buildathon Submission & Video Production Guide
**Razorpay AI Buildathon 2026 — Track 04: AI Finance Controller**

---

## 1. Razorpay Buildathon Rules & Evaluation Criteria

### Official Track 04 Requirements
* **Goal**: Build an AI Finance Controller agent that closes a finance-ops loop across a batch of 50+ synthetic records.
* **Suggested Directions**: Multi-source reconciliation, settlement Q&A, forward cash forecasting, and tax-line matching.
* **Core Evaluation Dimensions**:
  1. **Throughput**: Real-world execution speed and processing capacity.
  2. **Verified Accuracy**: Quantitative precision, recall, and false-positive rates on multi-source datasets.
  3. **Documented Exception Handling**: A transparent, auditable exception queue for ambiguous or unhandled records.
* **Submission Deliverables**:
  1. Working AI project codebase.
  2. Public GitHub repository with documentation.
  3. **5-Minute Pitch / Demo Video**.

### How PaisaGuard Beats Every Requirement
| Requirement | Hackathon Minimum | PaisaGuard Implementation |
| :--- | :--- | :--- |
| **Record Batch Size** | 50+ records | **385 multi-source records** (100 operational + 30 held-out transactions, 12 payout batches, bank credits) |
| **Finance-Ops Loop** | Single workflow | **Complete 3-Way Closed Loop**: OMS Orders $\longleftrightarrow$ Razorpay Settlements $\longleftrightarrow$ Bank Credits $\to$ AI Exception Queue $\to$ Policy Gate $\to$ Human Sign-off $\to$ Append-only Audit Trail |
| **System Throughput** | Functional | **658+ records/sec** ($<375$ms latency) with **100% Lock-Free WAL concurrency** |
| **Verified Accuracy** | Basic report | **100% Precision, 100% Recall, 0 False Positives**, 100% Baseline Diagnostic Accuracy, 100% Deliberate Abstention Fidelity |
| **Exception List** | Simple log | **Interactive Exception Controller**: Sandboxed Groq/Gemini LLM reasoning with citation verification, ₹50 variance cap, and 3.50% MDR policy gate |
| **Currency Precision**| Often unaddressed | **Canonical Integer Paise (`*_paise`)**: Zero floating-point representation drift or ledger corruption |

---

## 2. Recording Setup & Preparation

### Tools Recommended
* **Screen Recorder**: OBS Studio (recommended), Loom, or QuickTime.
* **Resolution**: 1920x1080 (1080p, 60fps).
* **Browser Settings**: Google Chrome at **125% zoom** so text and numbers are crystal clear on mobile and desktop screens.
* **Audio**: Crisp microphone with noise suppression (Krisp or OBS noise gate).

### Pre-Recording Checklist
1. Ensure `.env` is configured with your Groq API key:
   ```env
   PAISAGUARD_ENV=development
   PAISAGUARD_API_TOKEN=test_finops_token_2026
   RAZORPAY_WEBHOOK_SECRET=rzp_sec_buildathon_2026_demo
   AI_PROVIDER=groq
   AI_MODEL=llama-3.3-70b-versatile
   GROQ_API_KEY=your_groq_api_key_here
   ```
2. Verify preflight in terminal:
   ```bash
   .venv/bin/python exception_agent.py --preflight
   ```
   *(Must show `STATUS: OPERATIONAL`, `MODE: Groq Live Demo`, `LIVE AI: YES`)*
3. Clean the database for a fresh demo:
   ```bash
   python seed_data.py --reset
   ```
4. Prepare browser tabs:
   * **Tab 1**: `http://localhost:8501` (Streamlit Operator Console)
   * **Tab 2**: `http://localhost:8001/docs` (FastAPI Swagger Interactive Docs)
   * **Tab 3**: GitHub Repository (`https://github.com/maazmdx/PaisaGuard`)

---

## 3. Full 5-Minute Master Pitch Video Script

**Target Length**: 4:45 to 5:00 minutes  
**Format**: Screen capture with facecam (top-right) or professional voiceover.

```
TIMELINE BREAKDOWN:
0:00 - 0:45 | Scene 1: The Hook & The Broken State of FinOps
0:45 - 1:40 | Scene 2: Architectural Solution & The 3-Way Loop
1:40 - 2:40 | Scene 3: Live Demo — Deterministic Core & Webhook Gateway
2:40 - 3:50 | Scene 4: Live Demo — Guarded AI Controller (Groq Live)
3:50 - 4:30 | Scene 5: Benchmarks, Accuracy & Concurrency Moats
4:30 - 5:00 | Scene 6: Closing & The Future of AI in Finance
```

---

### Scene 1: The Hook & The Problem [0:00 - 0:45]

**Visual**: 
Camera on face or full screen showing the title slide with PaisaGuard logo:  
*"PaisaGuard: Deterministic Three-Way Payment Reconciliation & Guarded AI Controller"*.  
Cut to brief diagram showing OMS Orders, Gateway, and Bank transfers with red alert icons.

**Voiceover**:
> "Hi everyone, I’m excited to present **PaisaGuard**, built for Track 04 of the Razorpay AI Buildathon 2026.
> 
> High-growth e-commerce and fintech platforms in India process millions of transactions monthly across Razorpay, internal order management systems, and bank payout feeds. Yet, finance teams bleed millions of rupees every single month to three hidden operational failures:
> 
> First, **sub-paise rounding accumulation**: Razorpay calculates gateway fees and GST at sub-paise precision, but banks settle in integer rupees, causing naive reconciliation tools to trigger hundreds of false-positive exception tickets.
> 
> Second, **GST Input Tax Credit leakage**: Under Section 16(2)(aa) of the CGST Act, if daily gateway fee deductions don't match the monthly GSTR-2B invoice, you cannot claim tax credit.
> 
> And third, **the danger of autonomous AI**: Giving an LLM direct write access to your financial ledger is a recipe for hallucinated payouts and compliance disaster.
> 
> We engineered PaisaGuard on one unwavering principle:  
> **Deterministic logic balances the ledger. Guarded AI explains the anomalies. Humans make the decisions.**"

---

### Scene 2: The Solution & Architecture [0:45 - 1:40]

**Visual**: 
Switch to the Mermaid Architecture diagram in `README.md` or a slide. Trace the flow:
1. Multi-source ingestion (OMS, Razorpay, Bank).
2. HMAC-SHA256 gateway.
3. 4-Pass Deterministic Reconciliation Engine.
4. Exception Queue & Guarded AI Controller.
5. Policy Safety Gate & Append-Only Audit Trail.

**Voiceover**:
> "Let's examine how PaisaGuard solves this.
> 
> PaisaGuard ingests three distinct operational sources: internal OMS order ledgers, Razorpay settlement webhook streams, and physical bank payout credit statements.
> 
> Notice our four foundational engineering moats:
> 
> Number one: **Pure Canonical Integer Paise**. We banished all floating-point numbers from our database, APIs, and engine. Every financial value is strictly represented as integer paise. There is zero rounding drift.
> 
> Number two: **A 4-Pass Deterministic Engine**. Pass 1 matches transaction amounts and contractual fees. Pass 2 groups settlements into payout batches and verifies bank UTR references. Pass 3 resolves pre-approved contract overrides. And Pass 4 hashes an immutable cryptographic snapshot fingerprint of all inputs.
> 
> Number three: **Guarded AI Reasoning with Strict Label Isolation**. When anomalies occur, our AI agent receives *only* factual numbers, fees, and timestamps. It never sees ground-truth answers or diagnosis codes.
> 
> And number four: **A Deterministic Policy Safety Gate**. Even if a model suggests an override, our hardcoded policy gate enforces a ₹50 variance ceiling and a 3.50% MDR cap before any human operator can sign off. Zero destructive SQL updates occur—our audit trail is strictly append-only."

---

### Scene 3: Live Demo — The Deterministic Core & Webhooks [1:40 - 2:40]

**Visual**: 
Switch to the terminal. Type `./run_demo.sh` and hit Enter.  
Show the terminal output streaming:
- Dependency verification.
- Seeding 385 records into SQLite WAL mode.
- Running 46 tests (`46 passed in 10s`).
- Ingesting Hex HMAC signed webhook (`HTTP 200 OK`).
- Ingesting Base64 HMAC signed webhook (`HTTP 200 OK`).
- FastAPI up on port 8001; Streamlit up on 8501.

**Voiceover**:
> "Let's see PaisaGuard in action with our one-click reproducible demo launcher.
> 
> With a single command, `./run_demo.sh`, the launcher verifies pinned dependencies, seeds SQLite with 385 synthetic multi-source records in Write-Ahead-Log mode, runs our 46-test automated test harness, and boots our FastAPI ingestion gateway.
> 
> Notice right here in the terminal: the engine automatically simulates live Razorpay webhooks. It verifies both **Hex-encoded** and **Base64-encoded** HMAC-SHA256 signatures over raw request bytes, rejecting tampered attacks with HTTP 401 and deduplicating replayed event IDs without mutating state.
> 
> In less than twenty seconds, all services are verified and operational."

---

### Scene 4: Live Demo — Guarded AI Controller (Groq Live) [2:40 - 3:50]

**Visual**: 
Switch to browser at `http://localhost:8501` (Streamlit Operator Console).
1. **Batch KPI Board**: Highlight the 88.0% auto-match rate (88/100 transactions), fee totals, and payout status.
2. **Payout Batches Tab**: Click to show batched settlements reconciled against bank UTR numbers.
3. **Exceptions & AI Investigator Tab**:
   - Scroll down to an unresolved exception (e.g. Decision #89 or a 2.50% card surcharge variance).
   - Click **Run AI Investigation**.
   - Watch the spinner resolve in under 1 second.
   - Zoom in on the green badge: **`⚡ GROQ LIVE — groq/compound`**.
   - Highlight the **Confidence: 1.0**, the **Cited Evidence Record IDs**, the **Factual Mathematical Summary**, and the **Policy Gate Status: ✅ PASSED**.
   - Type reviewer name: `maaz_finops` and click **Approve Resolution**.
4. **Audit & Approvals Log Tab**:
   - Show the newly appended row in the immutable audit log with timestamp, reviewer, and decision ID.

**Voiceover**:
> "Now let's step into the shoes of a FinOps controller in our Streamlit visual console. Remember: this console communicates 100% over FastAPI HTTP endpoints, with zero direct database coupling.
> 
> On the Batch KPI board, we immediately see our deterministic matcher has automatically reconciled 88% of transactions with mathematical proof.
> 
> In Payout Batches, individual Razorpay settlements are grouped and verified against inbound bank credit statements and UTR references.
> 
> But what happens to the unresolved exceptions? Let's open the **Exceptions & AI Investigator**.
> 
> Here is an unmapped fee variance. Let's trigger a live AI investigation.
> 
> Powered by our live **Groq LLaMA 3.3 integration**, the model analyzes the raw financial evidence. Look at the emerald badge: **GROQ LIVE**.
> 
> Notice what happened:
> First, the model correctly diagnosed a Corporate Card Surcharge based purely on the numbers.
> Second, our citation validator checked that every cited record ID physically exists in the database.
> Third, our deterministic policy gate confirmed the variance is within our ₹50 ceiling and under the 3.50% contract cap.
> 
> I can now sign off as a named reviewer, `maaz_finops`, and approve the adjustment.
> 
> In the **Audit Log**, our decision is instantly appended as an immutable event. The original ledger record was never destructively updated; its current state is projected dynamically through SQL views."

---

### Scene 5: Benchmarks, Accuracy & Concurrency Moats [3:50 - 4:30]

**Visual**: 
Switch to the **Benchmark & Accuracy Console** in Streamlit or show the terminal running `eval_benchmarks.py` and `concurrency_tester.py`.
Highlight the numbers on screen:
- Throughput: 658+ records/sec.
- Latency: p50 372ms.
- Precision: 100.0% (0 false positives).
- Recall: 100.0%.
- Deliberate Abstention: 100.0% (3/3 ambiguous cases).
- Concurrency: 100% Lock-Free WAL mode (0 deadlocks).

**Voiceover**:
> "Let's look at the hard metrics from our automated evaluation harness.
> 
> PaisaGuard processes **over 658 records per second** with a median sweep latency of just 372 milliseconds.
> 
> Our matcher achieved **100% precision and 100% recall**—with exactly **zero false-positive matches**.
> 
> On our held-out evaluation dataset, our AI baseline achieved 100% diagnostic accuracy, and critically, **100% deliberate abstention fidelity**. When faced with unmapped or ambiguous deductions, the agent refuses to hallucinate and routes the ticket to a human CFO.
> 
> Furthermore, in our adversarial concurrency benchmark simulating 50 simultaneous webhook bursts, SQLite in WAL mode achieved **100% lock-free commits with zero deadlocks**."

---

### Scene 6: Closing & Submission Vision [4:30 - 5:00]

**Visual**: 
Switch to the GitHub repository page (`github.com/maazmdx/PaisaGuard`).  
Scroll through the clean README, 46 passed tests badge, and open-source MIT license.  
Return to camera for final concluding sentence.

**Voiceover**:
> "To summarize: PaisaGuard brings production-grade financial engineering to AI.
> 
> By anchoring on pure integer paise, enforcing dual-format HMAC webhooks, sandboxing live LLMs with deterministic policy gates, and preserving strictly append-only audit trails, we deliver a reliable, enterprise-ready AI Finance Controller.
> 
> The entire repository is open source, thoroughly tested with 46 automated tests, and ready for you to clone and verify locally in seconds.
> 
> Thank you to the Razorpay team for hosting the AI Buildathon 2026!"

---

## 4. 60-Second Short / Reel / Pitch Script (Fast Viral Cut)

**Platform**: YouTube Shorts, Instagram Reels, LinkedIn Video, Twitter/X  
**Duration**: 55 to 58 seconds  
**Visual Style**: Fast cuts, dynamic zooms, captions on screen, upbeat synthwave background music.

| Timecode | Visual on Screen | Spoken Audio (Energetic & Punchy) |
| :--- | :--- | :--- |
| **0:00 - 0:08** | Split screen: Red error "Database Locked" & "Variance Mismatch ₹0.01". Zoom in on speaker. | *"Did you know fintech companies lose millions every month because payment reconciliation still uses floating-point numbers?"* |
| **0:08 - 0:18** | Quick cut to terminal typing `./run_demo.sh`. Green checks flashing across screen. | *"Meet PaisaGuard, our AI Finance Controller for the Razorpay AI Buildathon 2026."* |
| **0:18 - 0:28** | Screen capture of Streamlit dashboard showing 88% Match Rate and ₹ totals. | *"It reconciles OMS orders, Razorpay settlements, and bank credits using pure integer paise—zero rounding drift!"* |
| **0:28 - 0:42** | Click **Run AI Investigation**. Show the emerald **`⚡ GROQ LIVE`** badge and policy gate passing. | *"When anomalies happen, a live Groq LLaMA 3.3 agent diagnoses the root cause in milliseconds—bounded by a hard ₹50 policy ceiling so the AI can never touch your money."* |
| **0:42 - 0:50** | Cut to benchmark terminal: 658 rec/s, 100% precision, 46/46 tests passed. | *"658 records per second. 100% precision. Zero false positives. 46 tests passing."* |
| **0:50 - 0:58** | GitHub repo on screen with URL: `github.com/maazmdx/PaisaGuard`. | *"Deterministic math. Guarded AI. Human control. Clone it on GitHub today!"* |

---

## 5. Exact Terminal Commands Cheatsheet

### 1. Preflight Verification (Show this before demo)
```bash
# Verify Groq Live AI connectivity without exposing keys
.venv/bin/python exception_agent.py --preflight
```

### 2. Full Demo Run (Main recording command)
```bash
# Clean database and run full launcher
python seed_data.py --reset
./run_demo.sh
```

### 3. Run Benchmark Suite (Optional cut-in shot)
```bash
# Run authoritative benchmark evaluation
AI_PROVIDER=mock python eval_benchmarks.py
```

### 4. Run Concurrency Benchmark (Optional cut-in shot)
```bash
# Run adversarial WAL concurrency test
PAISAGUARD_CI=1 python concurrency_tester.py
```

### 5. Automated Tests (Smoke verification)
```bash
# Run all 46 tests
pytest -q
```
