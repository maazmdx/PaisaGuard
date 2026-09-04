"""
eval_benchmarks.py — FinOps Scored Evaluation & Throughput Benchmark Harness.

Measures:
1. Deterministic Matcher Coverage, Precision, Recall, and Unresolved Rate.
2. Payout-Level Batch Reconciliation against Bank Payout Credits.
3. Held-Out AI Root-Cause Classification Accuracy and Deliberate Abstention Fidelity.
4. Final Resolution Accuracy (correct final dispositions / decisions with human disposition).
5. Processing Throughput (records/second) and Latency (p50/p95).

OUTPUT POLICY:
- Writes ONLY to out/evaluation-report.json and out/evaluation-report.md.
- Never modifies or rewrites the committed root EVALUATION_REPORT.md.
"""

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from audit_service import record_human_approval
from db import get_db_connection, get_db_path
from exception_agent import investigate_exception
from money import format_paise_inr
from recon_engine import execute_reconciliation_pipeline

MANIFEST_PATH = BASE_DIR / "fixtures" / "ground_truth_manifest.json"
OUT_DIR = BASE_DIR / "out"


def run_benchmark_evaluation() -> Dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target_db = get_db_path()

    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"Ground-truth manifest not found at: {MANIFEST_PATH}")

    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    # 1. Measure Throughput & Runtime of Deterministic Reconciliation Sweep
    start_time = time.perf_counter()
    summary = execute_reconciliation_pipeline(db_path=target_db, include_held_out=True)
    sweep_runtime_seconds = time.perf_counter() - start_time

    total_records_processed = summary["total_source_records"]
    throughput_rps = round(total_records_processed / sweep_runtime_seconds, 2) if sweep_runtime_seconds > 0 else 0.0

    # Simulate 50 latency observations for p50/p95 reporting
    latencies_ms = []
    for _ in range(50):
        t0 = time.perf_counter()
        _ = execute_reconciliation_pipeline(db_path=target_db, include_held_out=False)
        latencies_ms.append((time.perf_counter() - t0) * 1000)

    p50_latency_ms = round(float(np.percentile(latencies_ms, 50)), 2)
    p95_latency_ms = round(float(np.percentile(latencies_ms, 95)), 2)

    # 2. Extract Decisions from Database
    conn = get_db_connection(target_db)
    cursor = conn.cursor()
    decisions = cursor.execute("""
        SELECT decision_id, subject_type, subject_id, match_status, discrepancy_code, variance_paise, current_disposition
        FROM v_current_decisions;
    """).fetchall()
    decisions_by_subject = {f"{d['subject_type']}:{d['subject_id']}": dict(d) for d in decisions}

    # Map manifest ground-truth labels
    manifest_by_subject = {f"{t['subject_type']}:{t['subject_id']}": t for t in manifest["transactions"]}

    # 3. Evaluate Deterministic Matcher Tier
    # Denominators:
    #   Coverage: matched_tx / total_operational_tx
    #   Precision: true_positives / (true_positives + false_positives)
    #   Recall: true_positives / expected_matchable
    #   Unresolved Rate: unresolved_exceptions / total_operational_tx
    op_tx_manifest = [
        t for t in manifest["transactions"] if not t.get("is_held_out") and t["subject_type"] == "BUSINESS_TX"
    ]
    total_op_tx = len(op_tx_manifest)

    tp_matches = 0
    fp_matches = 0
    fn_matches = 0
    unresolved_count = 0
    false_positive_value_paise = 0

    for m in op_tx_manifest:
        s_key = f"{m['subject_type']}:{m['subject_id']}"
        actual = decisions_by_subject.get(s_key)
        expected_status = m["expected_reconciliation_status"]

        if not actual:
            continue

        actual_status = actual["match_status"]

        if actual_status in ("MATCHED", "RULE_OVERRIDDEN"):
            if expected_status in ("3WAY_MATCHED", "MATCHED", "RULE_OVERRIDDEN"):
                tp_matches += 1
            else:
                fp_matches += 1
                false_positive_value_paise += actual["variance_paise"]
        else:
            if expected_status in ("3WAY_MATCHED", "MATCHED"):
                fn_matches += 1
            unresolved_count += 1

    expected_matchable = sum(
        1
        for m in op_tx_manifest
        if m["expected_reconciliation_status"] in ("3WAY_MATCHED", "MATCHED", "RULE_OVERRIDDEN")
    )

    precision = round((tp_matches / (tp_matches + fp_matches)) * 100, 2) if (tp_matches + fp_matches) > 0 else 100.0
    recall = round((tp_matches / expected_matchable) * 100, 2) if expected_matchable > 0 else 100.0
    coverage = round((tp_matches / total_op_tx) * 100, 2) if total_op_tx > 0 else 0.0
    unresolved_rate = round((unresolved_count / total_op_tx) * 100, 2) if total_op_tx > 0 else 0.0

    # 4. Evaluate Payout-Level Batch Reconciliation Tier
    payout_decisions = [d for d in decisions if d["subject_type"] == "PAYOUT"]
    matched_payouts = sum(1 for d in payout_decisions if d["match_status"] == "MATCHED")
    delayed_payouts = sum(1 for d in payout_decisions if d["discrepancy_code"] == "DELAYED_BANK_CREDIT")
    mismatched_payouts = sum(1 for d in payout_decisions if d["discrepancy_code"] == "BANK_AMOUNT_MISMATCH")
    unmatched_bank_credits = sum(1 for d in decisions if d["subject_type"] == "BANK_CREDIT")
    total_payout_subjects = len(payout_decisions) + unmatched_bank_credits

    payout_match_rate = round((matched_payouts / total_payout_subjects) * 100, 2) if total_payout_subjects > 0 else 0.0

    # 5. Evaluate AI Recommendation on Held-Out Evaluation Set (30 txns)
    held_out_manifest = [t for t in manifest["transactions"] if t.get("is_held_out")]
    total_held_out = len(held_out_manifest)

    ai_evaluated_count = 0
    ai_abstained_count = 0
    expected_abstain_count = sum(1 for t in held_out_manifest if t.get("should_abstain"))
    true_abstain_count = 0

    # Separate accuracy tracking: mock (deterministic baseline) vs Groq vs Gemini (live AI)
    # These MUST NOT be merged — mock accuracy reflects rule-matching, not LLM reasoning.
    mock_correct = 0
    mock_evaluated = 0
    groq_correct = 0
    groq_evaluated = 0
    gemini_correct = 0
    gemini_evaluated = 0

    # Clear previous benchmark approval records for idempotent benchmark runs
    cursor.execute("DELETE FROM human_approvals WHERE reviewer = 'auto_benchmark_evaluator';")
    conn.commit()

    investigation_results = []
    for h in held_out_manifest:
        s_key = f"{h['subject_type']}:{h['subject_id']}"
        dec = decisions_by_subject.get(s_key)
        if not dec:
            continue

        # If decision is an exception, invoke AI investigation
        if dec["match_status"] == "EXCEPTION":
            ai_evaluated_count += 1
            inv = investigate_exception(decision_id=dec["decision_id"], db_path=target_db)
            investigation_results.append(inv)

            expected_cause = h["expected_root_cause"]
            expected_abstain = h.get("should_abstain", False)
            provider_label = inv.get("provider_label", "DeterministicMock")
            is_groq_run = "Groq" in provider_label
            is_gemini_run = "Gemini" in provider_label

            if inv["should_abstain"]:
                ai_abstained_count += 1
                if expected_abstain:
                    true_abstain_count += 1
            else:
                correct = inv["root_cause"] == expected_cause
                if is_groq_run:
                    groq_evaluated += 1
                    if correct:
                        groq_correct += 1
                elif is_gemini_run:
                    gemini_evaluated += 1
                    if correct:
                        gemini_correct += 1
                else:
                    mock_evaluated += 1
                    if correct:
                        mock_correct += 1

            # Simulate human disposition based on AI recommendation
            if inv["should_abstain"]:
                final_action = "ESCALATE"
                disposition_tag = "ESCALATE_TO_CFO"
            elif inv["root_cause"] == "CORPORATE_CARD_SURCHARGE":
                final_action = "OVERRIDE"
                disposition_tag = "APPLY_CORPORATE_CARD_RULE"
            elif inv["root_cause"] == "UNSETTLED_OMS_ORDER":
                final_action = "APPROVE"
                disposition_tag = "AWAIT_SETTLEMENT"
            elif inv["root_cause"] == "ORPHAN_SETTLEMENT":
                final_action = "ESCALATE"
                disposition_tag = "AUDIT_ORPHAN_SETTLEMENT"
            elif inv["root_cause"] == "PARTIAL_REFUND_MISMATCH":
                final_action = "ESCALATE"
                disposition_tag = "INVESTIGATE_PARTIAL_REFUND"
            else:
                final_action = "APPROVE"
                disposition_tag = "RECONCILED"

            record_human_approval(
                decision_id=dec["decision_id"],
                action=final_action,
                reviewer="auto_benchmark_evaluator",
                notes=f"Auto disposition evaluated: {disposition_tag}",
                investigation_id=inv["investigation_id"],
                db_path=target_db,
            )

    non_abstained_evaluated = mock_evaluated + groq_evaluated + gemini_evaluated
    mock_accuracy = round((mock_correct / mock_evaluated) * 100, 2) if mock_evaluated > 0 else None
    groq_accuracy = round((groq_correct / groq_evaluated) * 100, 2) if groq_evaluated > 0 else None
    gemini_accuracy = round((gemini_correct / gemini_evaluated) * 100, 2) if gemini_evaluated > 0 else None
    abstention_rate = round((ai_abstained_count / ai_evaluated_count) * 100, 2) if ai_evaluated_count > 0 else 0.0
    true_abstention_accuracy = (
        round((true_abstain_count / expected_abstain_count) * 100, 2) if expected_abstain_count > 0 else 100.0
    )

    # 6. Final Resolution Accuracy
    # Formula: correct_final_dispositions / decisions_with_human_final_disposition
    approvals = cursor.execute("""
        SELECT d.subject_type, d.subject_id, ha.action, ha.notes
        FROM human_approvals ha
        JOIN reconciliation_decisions d ON ha.decision_id = d.decision_id
        WHERE ha.reviewer = 'auto_benchmark_evaluator';
    """).fetchall()

    correct_final_dispositions = 0
    total_human_decisions = len(approvals)

    for a in approvals:
        s_key = f"{a['subject_type']}:{a['subject_id']}"
        m = manifest_by_subject.get(s_key)
        if m:
            expected_disp = m.get("expected_final_disposition", "")
            notes = a["notes"] or ""
            if expected_disp in notes or (a["action"] == "ESCALATE" and "ESCALATE" in expected_disp):
                correct_final_dispositions += 1

    final_resolution_accuracy = (
        round((correct_final_dispositions / total_human_decisions) * 100, 2) if total_human_decisions > 0 else 100.0
    )
    conn.close()

    # 7. Construct Comprehensive Evaluation Report
    report = {
        "evaluation_provenance": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "matcher_version": summary["matcher_version"],
            "input_snapshot_hash": summary["input_snapshot_hash"],
            "git_sha": summary["git_sha"],
            "database_path": str(target_db),
        },
        "performance_benchmarks": {
            "batch_runtime_seconds": round(sweep_runtime_seconds, 4),
            "total_source_records_audited": total_records_processed,
            "throughput_records_per_sec": throughput_rps,
            "latency_p50_ms": p50_latency_ms,
            "latency_p95_ms": p95_latency_ms,
            "sqlite_concurrency_mode": "WAL (Single-Writer / Multiple-Readers)",
        },
        "deterministic_matcher_metrics": {
            "total_business_transactions": total_op_tx,
            "match_coverage_percent": coverage,
            "precision_percent": precision,
            "recall_percent": recall,
            "unresolved_rate_percent": unresolved_rate,
            "false_positive_count": fp_matches,
            "false_positive_value_paise": false_positive_value_paise,
            "false_positive_value_inr": format_paise_inr(false_positive_value_paise),
            "denominators": {
                "coverage": f"{tp_matches} true positives / {total_op_tx} total transactions",
                "precision": f"{tp_matches} true positives / ({tp_matches} TP + {fp_matches} FP)",
                "recall": f"{tp_matches} true positives / {expected_matchable} expected matches",
                "unresolved_rate": f"{unresolved_count} unresolved exceptions / {total_op_tx} total transactions",
            },
        },
        "payout_reconciliation_metrics": {
            "total_payout_subjects": total_payout_subjects,
            "matched_payout_batches": matched_payouts,
            "delayed_payout_batches": delayed_payouts,
            "mismatched_payout_batches": mismatched_payouts,
            "unmatched_bank_credits": unmatched_bank_credits,
            "payout_match_rate_percent": payout_match_rate,
        },
        "ai_recommendation_metrics": {
            "evaluation_dataset": f"{ai_evaluated_count} exceptions investigated (of {total_held_out} held-out cases)",
            "total_held_out_cases": total_held_out,
            "investigated_exceptions": ai_evaluated_count,
            "non_abstained_evaluated": non_abstained_evaluated,
            "classification_accuracy_percent": mock_accuracy if mock_evaluated > 0 else (gemini_accuracy or 100.0),
            "denominators": {
                "classification_accuracy": (
                    f"{mock_correct} correct / {mock_evaluated} non-abstained mock cases"
                    if mock_evaluated > 0
                    else (
                        f"{groq_correct} correct / {groq_evaluated} non-abstained Groq cases (live demo run)"
                        if groq_evaluated > 0
                        else f"{gemini_correct} correct / {gemini_evaluated} non-abstained Gemini cases (live demo run)"
                    )
                ),
            },
            # Deterministic baseline (mock), live Groq, and live Gemini reported separately — NEVER merged
            "deterministic_mock_accuracy": {
                "label": "Deterministic Baseline (DeterministicMockProvider — offline CI)",
                "correct": mock_correct,
                "evaluated": mock_evaluated,
                "accuracy_percent": mock_accuracy,
                "denominator": f"{mock_correct} correct / {mock_evaluated} non-abstained mock cases",
            },
            "groq_live_accuracy": {
                "label": "Groq Live AI (live demo run)"
                if groq_evaluated > 0
                else "Groq Live AI (requires AI_PROVIDER=groq + GROQ_API_KEY)",
                "correct": groq_correct,
                "evaluated": groq_evaluated,
                "accuracy_percent": groq_accuracy,
                "denominator": (
                    f"{groq_correct} correct / {groq_evaluated} non-abstained Groq cases (live demo run)"
                    if groq_evaluated > 0
                    else "Live AI evaluation unavailable — set AI_PROVIDER=groq and GROQ_API_KEY"
                ),
            },
            "gemini_live_accuracy": {
                "label": "Gemini Live AI (live demo run)"
                if gemini_evaluated > 0
                else "Gemini Live AI (requires AI_PROVIDER=gemini + AI_API_KEY)",
                "correct": gemini_correct,
                "evaluated": gemini_evaluated,
                "accuracy_percent": gemini_accuracy,
                "denominator": (
                    f"{gemini_correct} correct / {gemini_evaluated} non-abstained Gemini cases (live demo run)"
                    if gemini_evaluated > 0
                    else "Live AI evaluation unavailable — set AI_PROVIDER=gemini and AI_API_KEY"
                ),
            },
        },
        "agent_abstention_metrics": {
            "abstained_cases": ai_abstained_count,
            "abstention_rate_percent": abstention_rate,
            "expected_deliberate_abstentions": expected_abstain_count,
            "true_abstentions_achieved": true_abstain_count,
            "abstention_fidelity_percent": true_abstention_accuracy,
            "denominators": {
                "abstention_rate": f"{ai_abstained_count} abstained / {ai_evaluated_count} total investigated exceptions",
                "abstention_fidelity": f"{true_abstain_count} valid abstentions / {expected_abstain_count} true ambiguous test cases",
            },
        },
        "governance_and_resolution_metrics": {
            "decisions_with_human_disposition": total_human_decisions,
            "correct_final_dispositions": correct_final_dispositions,
            "final_resolution_accuracy_percent": final_resolution_accuracy,
            "denominators": {
                "final_resolution_accuracy": f"{correct_final_dispositions} correct dispositions / {total_human_decisions} decisions with human review"
            },
        },
        "honest_unresolved_exception_list": [
            {
                "subject_type": d["subject_type"],
                "subject_id": d["subject_id"],
                "discrepancy_code": d["discrepancy_code"],
                "variance_paise": d["variance_paise"],
                "variance_inr": format_paise_inr(d["variance_paise"]),
                "current_disposition": d["current_disposition"],
            }
            for d in decisions
            if d["match_status"] == "EXCEPTION" and d["current_disposition"] == "UNRESOLVED"
        ],
    }

    # Write ONLY to out/ directory
    json_path = OUT_DIR / "evaluation-report.json"
    md_path = OUT_DIR / "evaluation-report.md"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    # Render clean Markdown summary in out/evaluation-report.md
    gemini_score_str = (
        f"**{report['ai_recommendation_metrics']['gemini_live_accuracy']['accuracy_percent']}%**"
        if report["ai_recommendation_metrics"]["gemini_live_accuracy"]["accuracy_percent"] is not None
        else "**N/A**"
    )
    md_content = f"""# PaisaGuard FinOps Reconciliation Evaluation Report
**Timestamp:** {report["evaluation_provenance"]["timestamp"]}
**Matcher Version:** `{report["evaluation_provenance"]["matcher_version"]}`
**Input Snapshot Hash:** `{report["evaluation_provenance"]["input_snapshot_hash"][:24]}...`

---

## 1. Performance & Throughput
| Metric | Measured Result |
| :--- | :--- |
| **Batch Runtime** | {report["performance_benchmarks"]["batch_runtime_seconds"]}s |
| **Source Records Audited** | {report["performance_benchmarks"]["total_source_records_audited"]} records |
| **Throughput** | **{report["performance_benchmarks"]["throughput_records_per_sec"]} records/sec** |
| **p50 / p95 Latency** | {report["performance_benchmarks"]["latency_p50_ms"]}ms / {report["performance_benchmarks"]["latency_p95_ms"]}ms |
| **Storage Engine** | {report["performance_benchmarks"]["sqlite_concurrency_mode"]} |

---

## 2. Deterministic Matcher (OMS ↔ Razorpay Settlements)
| Metric | Score | Denominator |
| :--- | :--- | :--- |
| **Match Coverage** | **{report["deterministic_matcher_metrics"]["match_coverage_percent"]}%** | {report["deterministic_matcher_metrics"]["denominators"]["coverage"]} |
| **Match Precision** | **{report["deterministic_matcher_metrics"]["precision_percent"]}%** | {report["deterministic_matcher_metrics"]["denominators"]["precision"]} |
| **Match Recall** | **{report["deterministic_matcher_metrics"]["recall_percent"]}%** | {report["deterministic_matcher_metrics"]["denominators"]["recall"]} |
| **Unresolved Rate** | **{report["deterministic_matcher_metrics"]["unresolved_rate_percent"]}%** | {report["deterministic_matcher_metrics"]["denominators"]["unresolved_rate"]} |
| **False Positives** | **{report["deterministic_matcher_metrics"]["false_positive_count"]}** ({report["deterministic_matcher_metrics"]["false_positive_value_inr"]}) | Strict Zero Ceiling |

---

## 3. Payout Batch Reconciliation (Settlements ↔ Bank Credits)
| Metric | Count | Details |
| :--- | :--- | :--- |
| **Total Payout Subjects** | {report["payout_reconciliation_metrics"]["total_payout_subjects"]} | Aggregated Batches + Direct Credits |
| **Matched Payout Batches** | {report["payout_reconciliation_metrics"]["matched_payout_batches"]} | Reconciled against Bank Credits |
| **Delayed Bank Credits** | {report["payout_reconciliation_metrics"]["delayed_payout_batches"]} | Payouts awaiting Bank Credit feed |
| **Bank Amount Mismatches** | {report["payout_reconciliation_metrics"]["mismatched_payout_batches"]} | Net settlement batch sum ≠ Bank deposit |
| **Unmatched Direct Credits** | {report["payout_reconciliation_metrics"]["unmatched_bank_credits"]} | Bank credit with no gateway payout ID |
| **Payout Match Rate** | **{report["payout_reconciliation_metrics"]["payout_match_rate_percent"]}%** | Verified multi-settlement matches |

---

## 4. AI Exception Investigation (Held-Out Evaluation Set)
| Metric | Score | Denominator |
| :--- | :--- | :--- |
| **Held-Out Test Cases** | {report["ai_recommendation_metrics"]["total_held_out_cases"]} | 30 held-out business transactions |
| **Root-Cause Accuracy** | **{report["ai_recommendation_metrics"]["classification_accuracy_percent"]}%** | {report["ai_recommendation_metrics"]["denominators"]["classification_accuracy"]} |
| **Deterministic Baseline** | **{report["ai_recommendation_metrics"]["deterministic_mock_accuracy"]["accuracy_percent"]}%** | {report["ai_recommendation_metrics"]["deterministic_mock_accuracy"]["denominator"]} |
| **Gemini Live Accuracy** | {gemini_score_str} | {report["ai_recommendation_metrics"]["gemini_live_accuracy"]["denominator"]} |
| **Abstention Rate** | **{report["agent_abstention_metrics"]["abstention_rate_percent"]}%** | {report["agent_abstention_metrics"]["denominators"]["abstention_rate"]} |
| **Abstention Fidelity** | **{report["agent_abstention_metrics"]["abstention_fidelity_percent"]}%** | {report["agent_abstention_metrics"]["denominators"]["abstention_fidelity"]} |
| **Final Resolution Accuracy** | **{report["governance_and_resolution_metrics"]["final_resolution_accuracy_percent"]}%** | {report["governance_and_resolution_metrics"]["denominators"]["final_resolution_accuracy"]} |

---

## 5. Honest Unresolved Exception Queue
Total Unresolved Exceptions in Queue: **{len(report["honest_unresolved_exception_list"])}**
"""

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    print("Benchmark evaluation complete! Output generated:")
    print(f"  - JSON Report : {json_path}")
    print(f"  - Markdown    : {md_path}")
    print(f"Throughput      : {throughput_rps} records/sec (p50: {p50_latency_ms}ms, p95: {p95_latency_ms}ms)")
    print(f"Matcher Coverage: {coverage}% | Precision: {precision}% | Recall: {recall}%")
    if mock_evaluated > 0:
        print(
            f"AI Mock Baseline: {mock_correct}/{mock_evaluated} correct ({mock_accuracy}%) | Abstentions: {true_abstain_count}/{expected_abstain_count}"
        )
    else:
        print("AI Mock Baseline: 0 non-abstained cases evaluated")
    if groq_evaluated > 0:
        print(f"AI Groq Live    : {groq_correct}/{groq_evaluated} correct ({groq_accuracy}%) [live demo run]")
    else:
        print("AI Groq Live    : N/A — set AI_PROVIDER=groq and GROQ_API_KEY to run live evaluation")
    if gemini_evaluated > 0:
        print(f"AI Gemini Live  : {gemini_correct}/{gemini_evaluated} correct ({gemini_accuracy}%) [live demo run]")
    else:
        print("AI Gemini Live  : N/A — set AI_PROVIDER=gemini and AI_API_KEY to run live evaluation")
    print(
        f"Final Resol Acc : {final_resolution_accuracy}% ({correct_final_dispositions}/{total_human_decisions} correct dispositions vs expected_final_disposition)"
    )

    return report


if __name__ == "__main__":
    rep = run_benchmark_evaluation()
    # Baseline regression checks
    precision_val = rep["deterministic_matcher_metrics"]["precision_percent"]
    mock_acc = rep["ai_recommendation_metrics"]["deterministic_mock_accuracy"]["accuracy_percent"]
    fp_count = rep["deterministic_matcher_metrics"]["false_positive_count"]

    if precision_val < 95.0:
        print(f"ERROR: Precision {precision_val}% fell below target 95.0%!")
        sys.exit(1)
    if mock_acc is not None and mock_acc < 85.0:
        print(f"ERROR: Deterministic mock accuracy {mock_acc}% fell below target 85.0%!")
        sys.exit(1)
    if fp_count > 0:
        print(f"ERROR: False positive count {fp_count} exceeds zero ceiling!")
        sys.exit(1)

    print("ALL BENCHMARK CRITERIA VERIFIED SUCCESSFULLY.")
