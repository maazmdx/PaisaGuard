# IMPLEMENTATION_PLAN.md: PaisaGuard 3-Source FinOps Rebuild Plan (Deployment Corrections)

## Track 04: AI Finance Controller - Razorpay AI Buildathon 2026

### Core Architectural Principles & Deployment Corrections

1. **Complete Database Isolation for Dashboard**:
   - The dashboard container has **zero** volume mounts to SQLite.
   - All dashboard data is retrieved via FastAPI read endpoints:
     - `GET /metrics`: Transaction-level and payout-level KPIs
     - `GET /payouts`: Payout-level aggregated batches and bank credits
     - `GET /exceptions`: Unresolved exception queue with minimized evidence
     - `GET /audit-events`: Append-only audit timeline
     - `GET /evaluation-report`: Scored evaluation metrics and analysis
2. **Volume Ownership Initialization (UID 1000)**:
   - Root-only `volume-perms` service initializes `/app/data` ownership to `1000:1000`.
   - Ensures `docker compose down -v` followed by startup succeeds cleanly on fresh volumes.
3. **Non-Destructive Standard Startup & `demo-init` Profile**:
   - One-shot seeding is isolated to `demo-init` under `profiles: ["demo"]` (`python seed_data.py --reset`).
   - Standard startup (`docker compose up`) never resets an existing database.
4. **Dashboard Graceful Degradation & Token Notice**:
   - When `PAISAGUARD_API_TOKEN` is unset, dashboard shows a prominent yellow notice:
     `⚠️ Write actions disabled: Server token not configured. Set a local PAISAGUARD_API_TOKEN in .env to enable AI investigation and human approvals.`
   - Read-only visualization remains fully accessible.
5. **Fail-Closed Token Security**:
   - State-changing endpoints (`/reconcile/sweep`, `/ai/investigate`, `/approvals/decision`) require `X-PaisaGuard-Token` and fail closed if `PAISAGUARD_API_TOKEN` is unset on the server.
6. **Unambiguous Integer Paise Typing**:
   - `parse_inr_to_paise(val: Union[str, Decimal]) -> int`: strictly parses string/Decimal INR inputs; rejects raw integers.
   - `require_paise(val: Any) -> int`: verifies `val` is strictly integer paise.
7. **Polymorphic Decision Subjects & Append-Only State**:
   - Supports `subject_type` (`BUSINESS_TX`, `PAYOUT`, `BANK_CREDIT`) and `subject_id`.
   - Zero `UPDATE` statements on `reconciliation_decisions`; current disposition is derived dynamically via `v_current_decisions`.
8. **Evaluation Output Isolation**:
   - `eval_benchmarks.py` writes strictly to `out/evaluation-report.json` and `out/evaluation-report.md`.
   - Root `EVALUATION_REPORT.md` is committed and human-reviewed.

---

### Implementation Phases

- **Phase 1: Cleanup & Docker Topology Setup**: Remove legacy files, update `requirements.txt`, configure `Dockerfile`, `docker-compose.yml`, `.env.example`, and `.gitignore`.
- **Phase 2: Canonical Money & Schema**: Update `money.py` (`parse_inr_to_paise`, `require_paise`), update `schema.sql` (integer paise, subject polymorphism, `v_current_decisions` view, append-only tables).
- **Phase 3: Ground-Truth Manifest & Fixtures**: Create `fixtures/ground_truth_manifest.json` (100 txns / 300+ records + 30 held-out) and `seed_data.py` with `--reset`.
- **Phase 4: Deterministic 3-Way Matcher**: Implement `recon_engine.py` with snapshot hashing, versioned matcher, multi-settlement payout matching, and append-only decision linking.
- **Phase 5: AI Exception Agent & Policy Gate**: Implement `exception_agent.py` (generic provider, mock + Gemini, strict Pydantic model, failure hardening), `policy_gate.py`, and `audit_service.py`.
- **Phase 6: API Gateway & Security Policy**: Update `api.py` with fail-closed token guard on state-changing endpoints, dashboard read endpoints (`/payouts`, `/exceptions`, `/audit-events`, `/evaluation-report`), mandatory HMAC, and event ID deduplication.
- **Phase 7: Test Suite & Evaluation Benchmark**: Implement pytest tests and `eval_benchmarks.py` writing to `out/evaluation-report.*`.
- **Phase 8: Streamlit Evaluator Console & Safe Demo**: Implement 5 polished evaluator surfaces in `app.py` making authenticated API calls, and safe port handling in `run_demo.sh`.
- **Phase 9: Documentation**: Write committed `EVALUATION_REPORT.md` and honest `README.md`.
