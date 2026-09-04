# PaisaGuard

**AI-assisted three-way payment reconciliation for the Razorpay AI Buildathon 2026 — Track 04: AI Finance Controller.**

PaisaGuard reconciles an internal order ledger, Razorpay settlement data, and bank payout credits. It automatically matches the records it can prove, routes the rest to an exception queue, and lets a live LLM explain an exception without authority to change financial data.

> AI recommends. Deterministic controls and a human operator decide.

## The finance-ops loop

1. Match OMS orders, Razorpay settlements, and bank credits using deterministic integer-paise rules.
2. Route unproven records into a focused exception queue.
3. Ask a live model for a structured, cited explanation from factual evidence only.
4. Validate schema, cited IDs, confidence, allowed actions, MDR, and variance limits.
5. Require a human approval, rejection, escalation, or override.
6. Preserve each action in an append-only audit trail.

## What is built

| Layer | What it does |
| --- | --- |
| Reconciliation core | Matches orders, settlements, and payout credits using canonical integer paise. |
| Exception queue | Captures missing settlements, fee discrepancies, refund mismatches, payout delays, and unmatched bank credits. |
| AI investigator | Uses Groq or Gemini for structured, cited recommendations from minimized factual evidence. |
| Safety controls | Enforces schema and citation validation, confidence thresholds, action allow-lists, and MDR/variance limits. |
| Human control | Appends approvals, rejections, escalations, and overrides without mutating reconciliation decisions. |
| Demo console | Streamlit dashboard backed only by FastAPI; it never mounts the SQLite database. |

## Measured fixture baseline

The fixture contains 100 operational business transactions and 30 held-out cases. It is evaluated as a batch, not as hand-picked examples.

| Measure | Result |
| --- | --- |
| Automatic operational matches | 88 / 100 (88%) |
| Matcher precision | 100% (88 / 88) |
| Matcher recall | 100% (88 / 88 expected matches) |
| False-positive matches | 0 |
| Offline deterministic diagnostic baseline | 14 / 14 non-abstained held-out exceptions |
| Deliberate abstentions | 3 / 3 ambiguous cases |

Throughput and latency depend on the machine. Generate the current, authoritative report with:

```bash
AI_PROVIDER=mock python eval_benchmarks.py
```

Results are written to `out/evaluation-report.json` and `out/evaluation-report.md`. Live Groq and Gemini results are reported separately from the offline mock baseline; no live-model accuracy is claimed unless a live provider is run.

## Quick start

Requires Python 3.12 and a POSIX shell.

```bash
git clone https://github.com/maazmdx/PaisaGuard.git
cd PaisaGuard
cp .env.example .env
./run_demo.sh
```

The launcher installs pinned dependencies, seeds the synthetic ledger, runs the deterministic test/evaluation path, starts FastAPI at `http://localhost:8001`, and starts Streamlit at `http://localhost:8501`.

To run only the signed-webhook proof and exit:

```bash
./run_demo.sh --exit-after-webhooks
```

## Configure live AI for the demo

`mock` is the default provider for deterministic local tests and CI. Use a live provider for the evaluator demo.

```env
# .env — Groq
AI_PROVIDER=groq
AI_MODEL=llama-3.3-70b-versatile
GROQ_API_KEY=your_key_here
```

Or:

```env
# .env — Gemini
AI_PROVIDER=gemini
AI_MODEL=gemini-2.5-flash
AI_API_KEY=your_key_here
```

Verify configuration without printing a key:

```bash
.venv/bin/python exception_agent.py --preflight
# or, once the API is running
curl http://localhost:8001/ai/preflight
```

The app fails closed when a requested live provider has no key. Keep `.env` private and out of screen recordings.

## Five-minute evaluator demo

1. Start with `./run_demo.sh`; open `http://localhost:8501`.
2. Show the batch KPI board and reconciliation outcomes.
3. Open an unresolved entry in **Exceptions & AI Investigator**.
4. Run an investigation and show the `GROQ LIVE` or `GEMINI LIVE` provider badge.
5. Show citations, confidence, and the policy-gate result.
6. Approve or escalate it as a named reviewer.
7. Open the audit log and show the appended human decision.

The launcher also proves hexadecimal and Base64 HMAC verification using signed Razorpay-style payloads. It does not need a live Razorpay account for that proof.

## Safety model

```text
OMS orders + settlements + bank credits
                 |
                 v
    deterministic reconciliation (integer paise)
                 |
      matched    |    unresolved exception
         |       |             |
         v       |             v
     audit record |       live AI explanation
                 |             |
                 |             v
                 |  schema + citations + policy gate
                 |             |
                 +------> human disposition
                               |
                               v
                       append-only audit event
```

- Monetary values are represented as integer paise across the matcher and financial storage.
- Live AI receives factual evidence and candidate record IDs, not the ground-truth diagnosis label.
- Invalid citations, malformed output, low confidence, or policy violations become an audited abstention.
- The AI has no database-write or money-moving capability.

## API surface

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/webhooks/razorpay` | Verify and ingest a signed Razorpay-style payment webhook. |
| `POST` | `/reconcile/sweep` | Run deterministic reconciliation. |
| `POST` | `/ai/investigate` | Generate a guarded exception recommendation. |
| `POST` | `/approvals/decision` | Append a human disposition. |
| `GET` | `/ai/preflight` | Safely report provider readiness. |
| `GET` | `/metrics`, `/payouts`, `/exceptions`, `/audit-events` | Read-only dashboard data. |
| `GET` | `/evaluation-report` | Return the latest generated report. |

Write endpoints require `X-PaisaGuard-Token`. Webhooks validate `X-Razorpay-Signature` against raw bytes and deduplicate event IDs.

## Local validation

```bash
source .venv/bin/activate
AI_PROVIDER=mock pytest -q
AI_PROVIDER=mock python eval_benchmarks.py
ruff check .
```

For a real Razorpay webhook, use Test Mode and configure the same secret in the dashboard and in `RAZORPAY_WEBHOOK_SECRET`. The adapter supports payment-style nested payloads and the flat demo payload used by the local replay.

## Repository guide

```text
api.py                FastAPI gateway, webhooks, and read APIs
app.py                Streamlit evaluator console
recon_engine.py       deterministic three-way reconciliation
exception_agent.py    mock, Groq, and Gemini investigation providers
policy_gate.py        deterministic recommendation safety gate
audit_service.py      append-only human decision service
eval_benchmarks.py    reproducible fixture evaluation
seed_data.py          synthetic ledger seeding
run_demo.sh           local demo launcher and signed-webhook proof
```

## Submission statement

PaisaGuard is an AI-assisted verification controller, not an autonomous finance agent. Deterministic reconciliation establishes the financial facts; a live model assists with explanation; policy and human review retain control of every financial outcome.

Licensed under the [MIT License](LICENSE).
