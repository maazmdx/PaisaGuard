"""
exception_agent.py — AI Exception Investigation Agent for PaisaGuard.

Architectural Guarantees:
1. Read-Only Advisor:
   - The AI agent NEVER mutates money, settlements, ledger records, or rules directly.
   - Operates strictly as a read-only advisor returning structured, minimized diagnostics.
2. Provider Independence:
   - Provider-independent interface supporting DeterministicMockProvider (default / CI)
     and GeminiProvider (live Gemini 2.5).
   - Tests and CI strictly use DeterministicMockProvider; no live network calls in tests.
   - Fails closed if AI_PROVIDER='gemini' and AI_API_KEY is not set.
3. Strict Pydantic Response Model:
   - root_cause, confidence, evidence_record_ids, evidence_summary,
     proposed_action, requires_human_approval, should_abstain, abstention_reason.
4. Hallucination & Citation Guardrails:
   - Validates that cited evidence IDs are present in the provided evidence bundle.
   - Forces deliberate abstention if confidence < 0.70 or on provider timeout/failure.
5. Audit Provenance:
   - Hashes evidence bundle and records prompt/model metadata in agent_investigations.
"""

import os
import sys
import json
import hashlib
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field, ValidationError

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_db_connection, get_db_cursor, get_db_path, log_audit_event
from policy_gate import PolicyGatekeeper, ALLOWED_ACTIONS
from money import require_paise

logger = logging.getLogger("paisaguard.agent")

AI_PROVIDER_ENV = os.environ.get("AI_PROVIDER", "mock").lower()
AI_MODEL_ENV = os.environ.get("AI_MODEL", "gemini-2.5-flash")
AI_API_KEY_ENV = os.environ.get("AI_API_KEY", "")


class ExceptionInvestigationResult(BaseModel):
    """Strict Pydantic model for AI FinOps investigation output."""
    root_cause: str = Field(..., description="Classified financial exception root cause")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    evidence_record_ids: List[str] = Field(..., description="Cited source record IDs supporting the diagnosis")
    evidence_summary: str = Field(..., description="Factual mathematical summary of findings")
    proposed_action: str = Field(..., description="Recommended FinOps action from allowed whitelist")
    requires_human_approval: bool = Field(True, description="Always requires human approval before state mutation")
    should_abstain: bool = Field(False, description="True if ambiguity warrants abstention")
    abstention_reason: Optional[str] = Field(None, description="Reason for abstention if should_abstain is True")


class BaseAIProvider(ABC):
    """Abstract interface for AI diagnostic providers."""

    @abstractmethod
    def investigate(self, prompt: str, evidence: Dict[str, Any]) -> str:
        """Generates raw response text from evidence."""
        pass


class DeterministicMockProvider(BaseAIProvider):
    """
    Deterministic rule-governed provider for reproducible evaluation and CI tests.
    Does NOT receive or access ground-truth labels; infers root cause purely from factual evidence.
    """

    def investigate(self, prompt: str, evidence: Dict[str, Any]) -> str:
        discrepancy = evidence.get("discrepancy_code", "")
        reason = evidence.get("reason", "").lower()
        available_ids = evidence.get("available_record_ids", [])
        variance_p = evidence.get("variance_paise", 0)

        # 1. Deliberate Incoherent / Ambiguous Case -> Agent MUST Abstain
        if discrepancy == "GENUINE_AMBIGUITY_INSUFFICIENT_DATA" or "incoherent" in reason or "unknown" in reason:
            return json.dumps({
                "root_cause": "GENUINE_AMBIGUITY_INSUFFICIENT_DATA",
                "confidence": 0.50,
                "evidence_record_ids": available_ids,
                "evidence_summary": f"Incoherent data with unexplained variance of {variance_p} paise. Historical pattern is inconclusive.",
                "proposed_action": "ABSTAIN",
                "requires_human_approval": True,
                "should_abstain": True,
                "abstention_reason": "Conflicting transaction data with insufficient gateway audit history. Recommended for manual CFO escalation."
            })

        # 2. Fee discrepancy evaluation
        if discrepancy in ("FEE_TAX_DISCREPANCY", "CORPORATE_CARD_SURCHARGE") or "fee discrepancy" in reason:
            gross_p = evidence.get("gross_amount_paise") or evidence.get("amount_paise", 0)
            actual_fee_p = evidence.get("actual_fee_paise", 0)
            rate_bps = (actual_fee_p * 10000) // gross_p if gross_p > 0 else 0

            # Commercial corporate card surcharge matches ~2.50% MDR (245 - 255 bps)
            if 245 <= rate_bps <= 255:
                return json.dumps({
                    "root_cause": "CORPORATE_CARD_SURCHARGE",
                    "confidence": 0.95,
                    "evidence_record_ids": available_ids,
                    "evidence_summary": f"Detected 50 bps fee surcharge ({variance_p} paise variance) matching commercial credit card interchange tier (2.50% MDR).",
                    "proposed_action": "ACCEPT_SURCHARGE_ADJUSTMENT",
                    "requires_human_approval": True,
                    "should_abstain": False,
                    "abstention_reason": None
                })
            else:
                # Ambiguous fee anomaly with no recognized tier -> deliberate abstention!
                return json.dumps({
                    "root_cause": "GENUINE_AMBIGUITY_INSUFFICIENT_DATA",
                    "confidence": 0.50,
                    "evidence_record_ids": available_ids,
                    "evidence_summary": f"Incoherent fee deduction of {actual_fee_p} paise on gross {gross_p} paise ({rate_bps/100:.2f}% MDR). Does not match standard contract schedules.",
                    "proposed_action": "ABSTAIN",
                    "requires_human_approval": True,
                    "should_abstain": True,
                    "abstention_reason": "Non-standard fee deduction with no recognized contract schedule. Escalated for FinOps review."
                })

        # 3. Delayed bank payout credit
        if discrepancy == "DELAYED_BANK_CREDIT" or "not yet arrived in bank" in reason:
            return json.dumps({
                "root_cause": "DELAYED_BANK_CREDIT",
                "confidence": 0.93,
                "evidence_record_ids": available_ids,
                "evidence_summary": f"Settlement batch assigned to payout but bank UTR credit confirmation is pending.",
                "proposed_action": "AWAIT_BANK_CREDIT",
                "requires_human_approval": True,
                "should_abstain": False,
                "abstention_reason": None
            })

        # 4. Unsettled OMS order
        if discrepancy == "UNSETTLED_OMS_ORDER" or "no settlement recorded" in reason:
            return json.dumps({
                "root_cause": "UNSETTLED_OMS_ORDER",
                "confidence": 0.92,
                "evidence_record_ids": available_ids,
                "evidence_summary": f"OMS order captured internally; awaiting settlement credit cycle from Razorpay.",
                "proposed_action": "AWAIT_SETTLEMENT",
                "requires_human_approval": True,
                "should_abstain": False,
                "abstention_reason": None
            })

        # 5. Orphan settlement
        if discrepancy == "ORPHAN_SETTLEMENT" or "without corresponding internal oms" in reason:
            return json.dumps({
                "root_cause": "ORPHAN_SETTLEMENT",
                "confidence": 0.89,
                "evidence_record_ids": available_ids,
                "evidence_summary": f"Gateway settlement exists without corresponding OMS order ID in database.",
                "proposed_action": "REQUEST_MERCHANT_CLARIFICATION",
                "requires_human_approval": True,
                "should_abstain": False,
                "abstention_reason": None
            })

        # 6. Partial refund / amount mismatch
        if discrepancy == "PARTIAL_REFUND_MISMATCH" or "gross amount mismatch" in reason:
            return json.dumps({
                "root_cause": "PARTIAL_REFUND_MISMATCH",
                "confidence": 0.87,
                "evidence_record_ids": available_ids,
                "evidence_summary": f"Settled amount differs from OMS gross amount by {variance_p} paise, indicating partial refund or adjustment.",
                "proposed_action": "REQUEST_MERCHANT_CLARIFICATION",
                "requires_human_approval": True,
                "should_abstain": False,
                "abstention_reason": None
            })

        # 7. Bank payout amount mismatch
        if discrepancy == "BANK_AMOUNT_MISMATCH" or "bank credit mismatch" in reason:
            return json.dumps({
                "root_cause": "BANK_AMOUNT_MISMATCH",
                "confidence": 0.90,
                "evidence_record_ids": available_ids,
                "evidence_summary": f"Total settlement net amount does not match bank credit amount by {variance_p} paise.",
                "proposed_action": "INITIATE_GATEWAY_DISPUTE",
                "requires_human_approval": True,
                "should_abstain": False,
                "abstention_reason": None
            })

        # 8. Unidentified direct credit
        if discrepancy == "UNIDENTIFIED_DIRECT_CREDIT" or "direct bank credit" in reason:
            return json.dumps({
                "root_cause": "UNIDENTIFIED_DIRECT_CREDIT",
                "confidence": 0.91,
                "evidence_record_ids": available_ids,
                "evidence_summary": f"Unmatched direct credit of {variance_p} paise with no associated gateway settlement payout.",
                "proposed_action": "REQUEST_MERCHANT_CLARIFICATION",
                "requires_human_approval": True,
                "should_abstain": False,
                "abstention_reason": None
            })

        # Default fallback
        return json.dumps({
            "root_cause": "UNKNOWN_DISCREPANCY",
            "confidence": 0.60,
            "evidence_record_ids": available_ids,
            "evidence_summary": f"Generic discrepancy detected: {discrepancy}",
            "proposed_action": "ABSTAIN",
            "requires_human_approval": True,
            "should_abstain": True,
            "abstention_reason": "Low confidence match on non-standard exception pattern."
        })


class GeminiProvider(BaseAIProvider):
    """
    Live Gemini integration using Google GenAI SDK or HTTP request.
    Fails closed if AI_API_KEY is not set. Never logs secret values.
    """

    def __init__(self, api_key: str, model_name: str = "gemini-2.5-flash"):
        if not api_key:
            raise ValueError("FinOps Configuration Violation: AI_API_KEY must be provided when AI_PROVIDER='gemini'.")
        self.api_key = api_key
        self.model_name = model_name

    def investigate(self, prompt: str, evidence: Dict[str, Any]) -> str:
        import requests
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:generateContent?key={self.api_key}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "response_mime_type": "application/json"
            }
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=12.0)
            if resp.status_code != 200:
                raise RuntimeError(f"Gemini API returned status {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as exc:
            raise RuntimeError(f"Gemini API invocation failed: {exc}") from exc


def get_ai_provider() -> BaseAIProvider:
    """Instantiates the configured AI provider, failing closed on misconfiguration."""
    provider_name = os.environ.get("AI_PROVIDER", AI_PROVIDER_ENV).lower()
    if provider_name == "gemini":
        api_key = os.environ.get("AI_API_KEY", AI_API_KEY_ENV)
        if not api_key:
            raise RuntimeError("CRITICAL ERROR: AI_PROVIDER='gemini' configured but AI_API_KEY is unset.")
        return GeminiProvider(api_key=api_key, model_name=os.environ.get("AI_MODEL", AI_MODEL_ENV))
    return DeterministicMockProvider()


def investigate_exception(
    decision_id: int,
    db_path: Optional[Path] = None,
    provider: Optional[BaseAIProvider] = None
) -> Dict[str, Any]:
    """
    Performs AI investigation of a reconciliation decision:
    1. Extracts minimized factual evidence bundle (ZERO ground truth exposure).
    2. Invokes AI provider to classify root cause, confidence, evidence citations, and action.
    3. Validates citations and schema: rejects hallucinated IDs, unsupported actions.
    4. Enforces deterministic Policy Gatekeeper.
    5. Persists append-only investigation record into agent_investigations.
    """
    target_db = db_path if db_path else get_db_path()
    conn = get_db_connection(target_db)
    cursor = conn.cursor()

    # Load decision record
    row = cursor.execute("""
        SELECT decision_id, subject_type, subject_id, business_tx_id, order_id,
               payment_id, payout_id, credit_id, match_status, discrepancy_code,
               variance_paise, evidence_json
        FROM reconciliation_decisions WHERE decision_id = ?;
    """, (decision_id,)).fetchone()

    if not row:
        conn.close()
        raise ValueError(f"Decision ID {decision_id} not found in database.")

    evidence_dict = json.loads(row["evidence_json"])
    discrepancy_code = row["discrepancy_code"]
    variance_paise = row["variance_paise"]

    # Collect valid citation IDs present in this evidence bundle to verify against hallucinations
    valid_ids = []
    for field in ["order_id", "payment_id", "payout_id", "credit_id", "business_tx_id"]:
        val = row[field]
        if val:
            valid_ids.append(str(val))
    if "order_id" in evidence_dict and evidence_dict["order_id"]:
        valid_ids.append(str(evidence_dict["order_id"]))
    if "payment_id" in evidence_dict and evidence_dict["payment_id"]:
        valid_ids.append(str(evidence_dict["payment_id"]))
    if "credit_id" in evidence_dict and evidence_dict["credit_id"]:
        valid_ids.append(str(evidence_dict["credit_id"]))
    valid_ids = sorted(list(set(valid_ids)))

    # Construct minimized evidence bundle (strictly factual, zero ground truth labels)
    minimized_evidence = {
        "decision_id": decision_id,
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "discrepancy_code": discrepancy_code,
        "variance_paise": variance_paise,
        "gross_amount_paise": evidence_dict.get("amount_paise", evidence_dict.get("oms_amount_paise", 0)),
        "actual_fee_paise": evidence_dict.get("actual_fee_paise", evidence_dict.get("fee_paise", 0)),
        "reason": evidence_dict.get("reason", ""),
        "available_record_ids": valid_ids
    }

    evidence_bundle_bytes = json.dumps(minimized_evidence, sort_keys=True).encode("utf-8")
    evidence_bundle_hash = f"sha256:{hashlib.sha256(evidence_bundle_bytes).hexdigest()}"

    active_provider = provider if provider else get_ai_provider()
    provider_name = active_provider.__class__.__name__
    model_name = os.environ.get("AI_MODEL", AI_MODEL_ENV)

    prompt = f"""
You are PaisaGuard AI Finance Controller. Analyze this reconciliation exception:
EVIDENCE BUNDLE:
{json.dumps(minimized_evidence, indent=2)}

Return strict JSON with fields:
- root_cause (string)
- confidence (float 0.0 to 1.0)
- evidence_record_ids (array of cited IDs from available_record_ids)
- evidence_summary (string)
- proposed_action (string from ALLOWED_ACTIONS: {sorted(list(ALLOWED_ACTIONS))})
- requires_human_approval (boolean: true)
- should_abstain (boolean)
- abstention_reason (string or null)
"""

    # Invoke provider with error and timeout handling
    raw_response = ""
    try:
        raw_response = active_provider.investigate(prompt, minimized_evidence)
        parsed_data = json.loads(raw_response)
        result = ExceptionInvestigationResult(**parsed_data)

        # Citation verification guardrail: all cited IDs must be in valid_ids
        for cited_id in result.evidence_record_ids:
            if cited_id not in valid_ids:
                logger.warning(f"Hallucinated citation detected: '{cited_id}' not in evidence {valid_ids}")
                result.should_abstain = True
                result.proposed_action = "ABSTAIN"
                result.abstention_reason = f"Verification failure: Cited record '{cited_id}' not present in source evidence bundle."
                break

        # Action whitelist guardrail
        if result.proposed_action not in ALLOWED_ACTIONS:
            result.should_abstain = True
            result.proposed_action = "ABSTAIN"
            result.abstention_reason = f"Verification failure: Proposed action '{result.proposed_action}' not permitted."

        # Low confidence guardrail
        if result.confidence < 0.70:
            result.should_abstain = True
            result.proposed_action = "ABSTAIN"
            if not result.abstention_reason:
                result.abstention_reason = f"Agent abstention: Confidence score {result.confidence:.2f} is below safety threshold 0.70."

    except (ValidationError, json.JSONDecodeError, Exception) as exc:
        logger.error(f"AI investigation provider failure: {exc}")
        raw_response = str(raw_response) or str(exc)
        result = ExceptionInvestigationResult(
            root_cause="DIAGNOSTIC_FAILURE",
            confidence=0.0,
            evidence_record_ids=valid_ids,
            evidence_summary="AI provider generated malformed response or encountered an execution timeout.",
            proposed_action="ABSTAIN",
            requires_human_approval=True,
            should_abstain=True,
            abstention_reason=f"Agent abstention: Provider failure ({exc}). Fallback to manual review."
        )

    # Deterministic Policy Gatekeeper Evaluation
    gross_amt = evidence_dict.get("amount_paise", evidence_dict.get("oms_amount_paise", 0))
    actual_fee = evidence_dict.get("actual_fee_paise", evidence_dict.get("fee_paise", 0))
    expected_fee = evidence_dict.get("expected_fee_paise", 0)

    policy_approved, policy_reason = PolicyGatekeeper.evaluate(
        gross_amount_paise=gross_amt,
        actual_fee_paise=actual_fee,
        expected_fee_paise=expected_fee,
        variance_paise=variance_paise,
        proposed_action=result.proposed_action
    )

    policy_status = "POLICY_APPROVED" if policy_approved else "POLICY_REJECTED"

    # Persist investigation into agent_investigations table
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_db_cursor(target_db) as cur:
        cur.execute("""
            INSERT INTO agent_investigations (
                decision_id, evidence_bundle_hash, provider, model,
                root_cause, confidence, evidence_record_ids, evidence_summary,
                proposed_action, should_abstain, abstention_reason,
                policy_status, policy_reason, raw_response, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            decision_id,
            evidence_bundle_hash,
            provider_name,
            model_name,
            result.root_cause,
            result.confidence,
            json.dumps(result.evidence_record_ids),
            result.evidence_summary,
            result.proposed_action,
            1 if result.should_abstain else 0,
            result.abstention_reason,
            policy_status,
            policy_reason,
            raw_response,
            timestamp
        ))
        investigation_id = cur.lastrowid

    log_audit_event(
        target_db,
        event_type="AI_INVESTIGATION_COMPLETED",
        aggregate_type="DECISION",
        aggregate_id=str(decision_id),
        payload={
            "investigation_id": investigation_id,
            "root_cause": result.root_cause,
            "confidence": result.confidence,
            "should_abstain": result.should_abstain,
            "policy_status": policy_status,
            "proposed_action": result.proposed_action
        }
    )

    conn.close()

    return {
        "investigation_id": investigation_id,
        "decision_id": decision_id,
        "evidence_bundle_hash": evidence_bundle_hash,
        "root_cause": result.root_cause,
        "confidence": result.confidence,
        "evidence_record_ids": result.evidence_record_ids,
        "evidence_summary": result.evidence_summary,
        "proposed_action": result.proposed_action,
        "requires_human_approval": result.requires_human_approval,
        "should_abstain": result.should_abstain,
        "abstention_reason": result.abstention_reason,
        "policy_status": policy_status,
        "policy_reason": policy_reason,
        "created_at": timestamp
    }
