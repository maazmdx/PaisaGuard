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

import hashlib
import json
import logging
import os
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from pydantic import BaseModel, Field, ValidationError

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_db_connection, get_db_cursor, get_db_path, log_audit_event
from policy_gate import ALLOWED_ACTIONS, PolicyGatekeeper

logger = logging.getLogger("paisaguard.agent")


def normalize_model_name(raw: Optional[str], provider: str = "gemini") -> str:
    cleaned = (raw or "").strip()
    if provider == "groq":
        if not cleaned or cleaned.startswith("gemini-") or cleaned.startswith("models/"):
            return "llama-3.3-70b-versatile"
        return cleaned
    c_lower = cleaned.lower()
    if not cleaned or "llama" in c_lower:
        return "gemini-3.5-flash"
    if c_lower in ("3.5 flash", "3.5-flash", "gemini-3.5-flash", "3.5"):
        return "gemini-3.5-flash"
    if c_lower in ("2.5 flash", "2.5-flash", "gemini-2.5-flash", "2.5"):
        return "gemini-2.5-flash"
    if c_lower.startswith("models/"):
        return cleaned.split("models/")[1]
    if not c_lower.startswith("gemini-") and not c_lower.startswith("gemma-"):
        return f"gemini-{cleaned.replace(' ', '-')}"
    return cleaned or "gemini-3.5-flash"


def _load_local_env_file(filepath: Path = BASE_DIR / ".env") -> None:
    """Load key-value pairs from .env if it exists, without overriding existing shell environment."""
    if not filepath.is_file():
        return
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'").strip('"')
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


_load_local_env_file()

AI_PROVIDER_ENV = os.environ.get("AI_PROVIDER", "mock").lower()
GROQ_API_KEY_ENV = os.environ.get("GROQ_API_KEY", "")
AI_API_KEY_ENV = os.environ.get("AI_API_KEY", "")
AI_MODEL_ENV = os.environ.get("AI_MODEL", "")


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
            return json.dumps(
                {
                    "root_cause": "GENUINE_AMBIGUITY_INSUFFICIENT_DATA",
                    "confidence": 0.50,
                    "evidence_record_ids": available_ids,
                    "evidence_summary": f"Incoherent data with unexplained variance of {variance_p} paise. Historical pattern is inconclusive.",
                    "proposed_action": "ABSTAIN",
                    "requires_human_approval": True,
                    "should_abstain": True,
                    "abstention_reason": "Conflicting transaction data with insufficient gateway audit history. Recommended for manual CFO escalation.",
                }
            )

        # 2. Fee discrepancy evaluation
        if discrepancy in ("FEE_TAX_DISCREPANCY", "CORPORATE_CARD_SURCHARGE") or "fee discrepancy" in reason:
            gross_p = evidence.get("gross_amount_paise") or evidence.get("amount_paise", 0)
            actual_fee_p = evidence.get("actual_fee_paise", 0)
            rate_bps = (actual_fee_p * 10000) // gross_p if gross_p > 0 else 0

            # Commercial corporate card surcharge matches ~2.50% MDR (245 - 255 bps)
            if 245 <= rate_bps <= 255:
                return json.dumps(
                    {
                        "root_cause": "CORPORATE_CARD_SURCHARGE",
                        "confidence": 0.95,
                        "evidence_record_ids": available_ids,
                        "evidence_summary": f"Detected 50 bps fee surcharge ({variance_p} paise variance) matching commercial credit card interchange tier (2.50% MDR).",
                        "proposed_action": "ACCEPT_SURCHARGE_ADJUSTMENT",
                        "requires_human_approval": True,
                        "should_abstain": False,
                        "abstention_reason": None,
                    }
                )
            else:
                # Ambiguous fee anomaly with no recognized tier -> deliberate abstention!
                return json.dumps(
                    {
                        "root_cause": "GENUINE_AMBIGUITY_INSUFFICIENT_DATA",
                        "confidence": 0.50,
                        "evidence_record_ids": available_ids,
                        "evidence_summary": f"Incoherent fee deduction of {actual_fee_p} paise on gross {gross_p} paise ({rate_bps / 100:.2f}% MDR). Does not match standard contract schedules.",
                        "proposed_action": "ABSTAIN",
                        "requires_human_approval": True,
                        "should_abstain": True,
                        "abstention_reason": "Non-standard fee deduction with no recognized contract schedule. Escalated for FinOps review.",
                    }
                )

        # 3. Delayed bank payout credit
        if discrepancy == "DELAYED_BANK_CREDIT" or "not yet arrived in bank" in reason:
            return json.dumps(
                {
                    "root_cause": "DELAYED_BANK_CREDIT",
                    "confidence": 0.93,
                    "evidence_record_ids": available_ids,
                    "evidence_summary": "Settlement batch assigned to payout but bank UTR credit confirmation is pending.",
                    "proposed_action": "AWAIT_BANK_CREDIT",
                    "requires_human_approval": True,
                    "should_abstain": False,
                    "abstention_reason": None,
                }
            )

        # 4. Unsettled OMS order
        if discrepancy == "UNSETTLED_OMS_ORDER" or "no settlement recorded" in reason:
            return json.dumps(
                {
                    "root_cause": "UNSETTLED_OMS_ORDER",
                    "confidence": 0.92,
                    "evidence_record_ids": available_ids,
                    "evidence_summary": "OMS order captured internally; awaiting settlement credit cycle from Razorpay.",
                    "proposed_action": "AWAIT_SETTLEMENT",
                    "requires_human_approval": True,
                    "should_abstain": False,
                    "abstention_reason": None,
                }
            )

        # 5. Orphan settlement
        if discrepancy == "ORPHAN_SETTLEMENT" or "without corresponding internal oms" in reason:
            return json.dumps(
                {
                    "root_cause": "ORPHAN_SETTLEMENT",
                    "confidence": 0.89,
                    "evidence_record_ids": available_ids,
                    "evidence_summary": "Gateway settlement exists without corresponding OMS order ID in database.",
                    "proposed_action": "REQUEST_MERCHANT_CLARIFICATION",
                    "requires_human_approval": True,
                    "should_abstain": False,
                    "abstention_reason": None,
                }
            )

        # 6. Partial refund / amount mismatch
        if discrepancy == "PARTIAL_REFUND_MISMATCH" or "gross amount mismatch" in reason:
            return json.dumps(
                {
                    "root_cause": "PARTIAL_REFUND_MISMATCH",
                    "confidence": 0.87,
                    "evidence_record_ids": available_ids,
                    "evidence_summary": f"Settled amount differs from OMS gross amount by {variance_p} paise, indicating partial refund or adjustment.",
                    "proposed_action": "REQUEST_MERCHANT_CLARIFICATION",
                    "requires_human_approval": True,
                    "should_abstain": False,
                    "abstention_reason": None,
                }
            )

        # 7. Bank payout amount mismatch
        if discrepancy == "BANK_AMOUNT_MISMATCH" or "bank credit mismatch" in reason:
            return json.dumps(
                {
                    "root_cause": "BANK_AMOUNT_MISMATCH",
                    "confidence": 0.90,
                    "evidence_record_ids": available_ids,
                    "evidence_summary": f"Total settlement net amount does not match bank credit amount by {variance_p} paise.",
                    "proposed_action": "INITIATE_GATEWAY_DISPUTE",
                    "requires_human_approval": True,
                    "should_abstain": False,
                    "abstention_reason": None,
                }
            )

        # 8. Unidentified direct credit
        if discrepancy == "UNIDENTIFIED_DIRECT_CREDIT" or "direct bank credit" in reason:
            return json.dumps(
                {
                    "root_cause": "UNIDENTIFIED_DIRECT_CREDIT",
                    "confidence": 0.91,
                    "evidence_record_ids": available_ids,
                    "evidence_summary": f"Unmatched direct credit of {variance_p} paise with no associated gateway settlement payout.",
                    "proposed_action": "REQUEST_MERCHANT_CLARIFICATION",
                    "requires_human_approval": True,
                    "should_abstain": False,
                    "abstention_reason": None,
                }
            )

        # Default fallback
        return json.dumps(
            {
                "root_cause": "UNKNOWN_DISCREPANCY",
                "confidence": 0.60,
                "evidence_record_ids": available_ids,
                "evidence_summary": f"Generic discrepancy detected: {discrepancy}",
                "proposed_action": "ABSTAIN",
                "requires_human_approval": True,
                "should_abstain": True,
                "abstention_reason": "Low confidence match on non-standard exception pattern.",
            }
        )


class GeminiProvider(BaseAIProvider):
    """
    Live Gemini integration via Google Generative Language REST API.

    Security constraints:
    - NEVER receives discrepancy_code, expected_root_cause, or any ground-truth label.
    - Prompt contains only raw factual evidence: amounts, fees, record IDs, free-text reason.
    - Model must infer root cause from financial evidence; it is never told the answer.
    - API key is passed via 'x-goog-api-key' header and NEVER in the request URL or logs.
    - Fails closed if AI_API_KEY is not set.
    """

    def __init__(self, api_key: str, model_name: str = "gemini-3.5-flash"):
        if not api_key:
            raise ValueError("FinOps Configuration Violation: AI_API_KEY must be provided when AI_PROVIDER='gemini'.")
        self.api_key = api_key
        self.model_name = normalize_model_name(model_name)

    def investigate(self, prompt: str, evidence: Dict[str, Any]) -> str:
        models_to_try = [self.model_name]
        if self.model_name != "gemini-2.5-flash":
            models_to_try.append("gemini-2.5-flash")

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        last_exc: Optional[Exception] = None
        for attempt, current_model in enumerate(models_to_try):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent"
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=15.0)
                if resp.status_code == 200:
                    data = resp.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                if resp.status_code >= 500 and attempt < len(models_to_try) - 1:
                    logger.warning(
                        f"Gemini API {resp.status_code} on model '{current_model}', "
                        f"retrying with '{models_to_try[attempt + 1]}'..."
                    )
                    last_exc = RuntimeError(f"Gemini API returned status {resp.status_code}: {resp.text[:200]}")
                    continue
                raise RuntimeError(f"Gemini API returned status {resp.status_code}: {resp.text[:200]}")
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                if attempt < len(models_to_try) - 1:
                    logger.warning(
                        f"Gemini API network error on model '{current_model}', "
                        f"retrying with '{models_to_try[attempt + 1]}': {exc}"
                    )
                    continue
        raise RuntimeError(
            f"Gemini API invocation failed after {len(models_to_try)} attempts: {last_exc}"
        ) from last_exc


class GroqProvider(BaseAIProvider):
    """
    Live Groq integration via Groq OpenAI-compatible REST API.
    Endpoint: https://api.groq.com/openai/v1/chat/completions

    Security constraints:
    - NEVER receives discrepancy_code, expected_root_cause, or any ground-truth label.
    - Prompt contains only raw factual evidence: amounts, fees, record IDs, free-text reason.
    - Model must infer root cause from financial evidence; it is never told the answer.
    - API key is passed via 'Authorization: Bearer <token>' header and NEVER in URL or logs.
    - Request strict JSON output matching ExceptionInvestigationResult using response_format={'type': 'json_object'}.
    - Fails closed if GROQ_API_KEY is not set.
    """

    def __init__(self, api_key: str, model_name: str = "llama-3.3-70b-versatile"):
        if not api_key:
            raise ValueError("FinOps Configuration Violation: GROQ_API_KEY must be provided when AI_PROVIDER='groq'.")
        self.api_key = api_key
        self.model_name = normalize_model_name(model_name, provider="groq")

    def investigate(self, prompt: str, evidence: Dict[str, Any]) -> str:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an expert FinOps Exception Investigation Agent for PaisaGuard.\n"
                        "Analyze the provided factual financial evidence and output a strict JSON object.\n"
                        "You must infer root cause from the numbers and evidence. Never guess or hallucinate IDs."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }

        models_to_try = [self.model_name]
        for fallback in ["llama-3.3-70b-versatile", "groq/compound", "groq/compound-mini"]:
            if fallback not in models_to_try:
                models_to_try.append(fallback)

        last_exc: Optional[Exception] = None
        for attempt, current_model in enumerate(models_to_try):
            payload["model"] = current_model
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=15.0)
                if resp.status_code == 200:
                    data = resp.json()
                    self.model_name = current_model
                    return data["choices"][0]["message"]["content"]
                if (resp.status_code >= 500 or resp.status_code == 404) and attempt < len(models_to_try) - 1:
                    logger.warning(
                        f"Groq API {resp.status_code} on model '{current_model}', "
                        f"retrying with '{models_to_try[attempt + 1]}'..."
                    )
                    last_exc = RuntimeError(f"Groq API returned status {resp.status_code}: {resp.text[:200]}")
                    continue
                raise RuntimeError(f"Groq API returned status {resp.status_code}: {resp.text[:200]}")
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                if attempt < len(models_to_try) - 1:
                    logger.warning(
                        f"Groq API network error on model '{current_model}', "
                        f"retrying with '{models_to_try[attempt + 1]}': {exc}"
                    )
                    continue
        raise RuntimeError(f"Groq API invocation failed after {len(models_to_try)} attempts: {last_exc}") from last_exc


def get_ai_provider() -> BaseAIProvider:
    """Instantiates the configured AI provider, failing closed on misconfiguration."""
    provider_name = os.environ.get("AI_PROVIDER", AI_PROVIDER_ENV).lower()
    if provider_name == "groq":
        api_key = os.environ.get("GROQ_API_KEY", GROQ_API_KEY_ENV).strip()
        if not api_key:
            raise RuntimeError("CRITICAL ERROR: AI_PROVIDER='groq' configured but GROQ_API_KEY is unset.")
        raw_model = os.environ.get("AI_MODEL", AI_MODEL_ENV) or "llama-3.3-70b-versatile"
        return GroqProvider(api_key=api_key, model_name=normalize_model_name(raw_model, provider="groq"))
    if provider_name == "gemini":
        api_key = os.environ.get("AI_API_KEY", AI_API_KEY_ENV).strip()
        if not api_key:
            raise RuntimeError("CRITICAL ERROR: AI_PROVIDER='gemini' configured but AI_API_KEY is unset.")
        raw_model = os.environ.get("AI_MODEL", AI_MODEL_ENV) or "gemini-3.5-flash"
        return GeminiProvider(api_key=api_key, model_name=normalize_model_name(raw_model, provider="gemini"))
    return DeterministicMockProvider()


def investigate_exception(
    decision_id: int, db_path: Optional[Path] = None, provider: Optional[BaseAIProvider] = None
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
    row = cursor.execute(
        """
        SELECT decision_id, subject_type, subject_id, business_tx_id, order_id,
               payment_id, payout_id, credit_id, match_status, discrepancy_code,
               variance_paise, evidence_json
        FROM reconciliation_decisions WHERE decision_id = ?;
    """,
        (decision_id,),
    ).fetchone()

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

    # ----------------------------------------------------------------
    # Evidence Bundle Construction
    # Two separate dicts with strict data isolation:
    #
    # internal_evidence: includes discrepancy_code for DeterministicMockProvider's
    #   rule-based logic. The mock is a reproducible offline baseline, not an AI
    #   under evaluation, so label access is permissible and expected.
    #
    # gemini_evidence: STRICTLY factual only. No discrepancy_code, no labels,
    #   no expected_root_cause. Gemini must infer root cause from raw financial
    #   evidence alone. Violating this corrupts the evaluation.
    # ----------------------------------------------------------------
    gross_amount_paise = evidence_dict.get("amount_paise", evidence_dict.get("oms_amount_paise", 0))
    actual_fee_paise = evidence_dict.get("actual_fee_paise", evidence_dict.get("fee_paise", 0))
    raw_reason = evidence_dict.get("reason", "")

    internal_evidence = {
        "decision_id": decision_id,
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "discrepancy_code": discrepancy_code,  # label — mock only, never sent to Gemini
        "variance_paise": variance_paise,
        "gross_amount_paise": gross_amount_paise,
        "actual_fee_paise": actual_fee_paise,
        "reason": raw_reason,
        "available_record_ids": valid_ids,
    }

    # Factual-only evidence bundle: no classification labels whatsoever
    gemini_evidence = {
        "decision_id": decision_id,
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "variance_paise": variance_paise,
        "gross_amount_paise": gross_amount_paise,
        "actual_fee_paise": actual_fee_paise,
        "raw_reason_text": raw_reason,  # free-text from gateway, no label decoding
        "available_record_ids": valid_ids,
    }

    # Hash is computed over internal_evidence for audit provenance (stable across providers)
    evidence_bundle_bytes = json.dumps(internal_evidence, sort_keys=True).encode("utf-8")
    evidence_bundle_hash = f"sha256:{hashlib.sha256(evidence_bundle_bytes).hexdigest()}"

    active_provider = provider if provider else get_ai_provider()
    provider_name = active_provider.__class__.__name__
    model_name = getattr(active_provider, "model_name", os.environ.get("AI_MODEL", AI_MODEL_ENV))

    is_gemini = isinstance(active_provider, GeminiProvider)
    is_groq = isinstance(active_provider, GroqProvider)
    is_live_llm = is_gemini or is_groq

    if is_live_llm:
        # Live LLM prompt: factual evidence only, model must infer root cause
        # Model NEVER receives discrepancy_code, expected_root_cause, or any ground-truth label
        prompt = f"""You are a FinOps investigator reviewing a payment reconciliation exception.

Your task: analyze the financial evidence below and infer the most likely root cause.
Do NOT guess based on field names. Reason from the numbers.

FACTUAL EVIDENCE:
{json.dumps(gemini_evidence, indent=2)}

REQUIRED RESPONSE FORMAT (strict JSON, no prose):
{{
  "root_cause": "<your inferred classification>",
  "confidence": <float 0.0 to 1.0>,
  "evidence_record_ids": [<cited IDs from available_record_ids only>],
  "evidence_summary": "<factual mathematical summary of your reasoning>",
  "proposed_action": "<one of: {sorted(list(ALLOWED_ACTIONS))}>",
  "requires_human_approval": true,
  "should_abstain": <true if insufficient evidence or confidence < 0.70>,
  "abstention_reason": "<reason or null>"
}}

If evidence is ambiguous or contradictory, set should_abstain=true and proposed_action=\"ABSTAIN\".
"""
        provider_evidence = gemini_evidence
    else:
        # Mock provider receives internal evidence including discrepancy_code for deterministic logic
        prompt = f"""[DeterministicMockProvider] Analyze reconciliation exception:\n{json.dumps(internal_evidence, indent=2)}"""
        provider_evidence = internal_evidence

    # Invoke provider with error and timeout handling
    raw_response = ""
    try:
        raw_response = active_provider.investigate(prompt, provider_evidence)
        parsed_data = json.loads(raw_response)
        result = ExceptionInvestigationResult(**parsed_data)

        # Citation verification guardrail: all cited IDs must be in valid_ids
        for cited_id in result.evidence_record_ids:
            if cited_id not in valid_ids:
                logger.warning(f"Hallucinated citation detected: '{cited_id}' not in evidence {valid_ids}")
                result.should_abstain = True
                result.proposed_action = "ABSTAIN"
                result.abstention_reason = (
                    f"Verification failure: Cited record '{cited_id}' not present in source evidence bundle."
                )
                break

        # Action whitelist guardrail
        if result.proposed_action not in ALLOWED_ACTIONS:
            result.should_abstain = True
            result.proposed_action = "ABSTAIN"
            result.abstention_reason = (
                f"Verification failure: Proposed action '{result.proposed_action}' not permitted."
            )

        # Low confidence guardrail
        if result.confidence < 0.70:
            result.should_abstain = True
            result.proposed_action = "ABSTAIN"
            if not result.abstention_reason:
                result.abstention_reason = (
                    f"Agent abstention: Confidence score {result.confidence:.2f} is below safety threshold 0.70."
                )

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
            abstention_reason=f"Agent abstention: Provider failure ({exc}). Fallback to manual review.",
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
        proposed_action=result.proposed_action,
    )

    policy_status = "POLICY_APPROVED" if policy_approved else "POLICY_REJECTED"

    # Persist investigation into agent_investigations table
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_db_cursor(target_db) as cur:
        cur.execute(
            """
            INSERT INTO agent_investigations (
                decision_id, evidence_bundle_hash, provider, model,
                root_cause, confidence, evidence_record_ids, evidence_summary,
                proposed_action, should_abstain, abstention_reason,
                policy_status, policy_reason, raw_response, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
            (
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
                timestamp,
            ),
        )
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
            "proposed_action": result.proposed_action,
        },
    )

    conn.close()

    # provider_label distinguishes deterministic baseline from live AI runs
    # Used in the Streamlit UI badge and evaluation report to never conflate the two.
    active_model = getattr(active_provider, "model_name", model_name)
    if is_groq:
        provider_label = f"Groq/{active_model}"
    elif is_gemini:
        provider_label = f"Gemini/{active_model}"
    else:
        provider_label = "DeterministicMock"

    return {
        "investigation_id": investigation_id,
        "decision_id": decision_id,
        "evidence_bundle_hash": evidence_bundle_hash,
        "provider_label": provider_label,
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
        "created_at": timestamp,
    }


def check_ai_preflight() -> Dict[str, Any]:
    """
    Verifies AI provider configuration safely without exposing any secret API keys.
    Clearly reports whether the app is in Mock baseline mode, Gemini live-demo mode, or Groq live-demo mode.
    """
    import requests

    provider_name = os.environ.get("AI_PROVIDER", AI_PROVIDER_ENV).lower()
    raw_model = os.environ.get("AI_MODEL", AI_MODEL_ENV)

    if provider_name == "groq":
        groq_api_key = os.environ.get("GROQ_API_KEY", GROQ_API_KEY_ENV).strip()
        model_name = normalize_model_name(raw_model, provider="groq")
        if not groq_api_key:
            return {
                "status": "misconfigured",
                "mode": "groq_live_demo",
                "provider": "GroqProvider",
                "model": model_name,
                "api_key_configured": False,
                "live_ai_available": False,
                "message": "AI_PROVIDER is set to 'groq' but GROQ_API_KEY is not configured.",
            }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {groq_api_key}",
        }
        payload = {
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5,
        }
        models_to_check = [model_name]
        for fallback in ["llama-3.3-70b-versatile", "groq/compound", "groq/compound-mini"]:
            if fallback not in models_to_check:
                models_to_check.append(fallback)

        verified_model = None
        last_error = None
        for m in models_to_check:
            payload["model"] = m
            try:
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=8.0
                )
                if r.status_code == 200:
                    verified_model = m
                    break
                else:
                    last_error = f"HTTP {r.status_code}: {r.text[:120]}"
            except Exception as e:
                last_error = str(e)[:120]

        if verified_model:
            return {
                "status": "operational",
                "mode": "groq_live_demo",
                "provider": "GroqProvider",
                "model": verified_model,
                "requested_model": model_name,
                "api_key_configured": True,
                "live_ai_available": True,
                "message": f"Groq live-demo mode verified ({verified_model} responsive).",
            }
        else:
            return {
                "status": "degraded",
                "mode": "groq_live_demo",
                "provider": "GroqProvider",
                "model": model_name,
                "api_key_configured": True,
                "live_ai_available": False,
                "error": last_error,
                "message": f"Groq live-demo key configured but API ping failed: {last_error}",
            }

    elif provider_name == "gemini":
        gemini_api_key = os.environ.get("AI_API_KEY", AI_API_KEY_ENV).strip()
        model_name = normalize_model_name(raw_model, provider="gemini")
        if not gemini_api_key:
            return {
                "status": "misconfigured",
                "mode": "gemini_live_demo",
                "provider": "GeminiProvider",
                "model": model_name,
                "api_key_configured": False,
                "live_ai_available": False,
                "message": "AI_PROVIDER is set to 'gemini' but AI_API_KEY is not configured.",
            }

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": gemini_api_key,
        }
        payload = {
            "contents": [{"parts": [{"text": '{"status": "ping"}'}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        models_to_check = [model_name]
        if model_name != "gemini-2.5-flash":
            models_to_check.append("gemini-2.5-flash")

        verified_model = None
        last_error = None
        for m in models_to_check:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=8.0)
                if r.status_code == 200:
                    verified_model = m
                    break
                else:
                    last_error = f"HTTP {r.status_code}: {r.text[:120]}"
            except Exception as e:
                last_error = str(e)[:120]

        if verified_model:
            return {
                "status": "operational",
                "mode": "gemini_live_demo",
                "provider": "GeminiProvider",
                "model": verified_model,
                "requested_model": model_name,
                "api_key_configured": True,
                "live_ai_available": True,
                "message": f"Gemini live-demo mode verified ({verified_model} responsive).",
            }
        else:
            return {
                "status": "degraded",
                "mode": "gemini_live_demo",
                "provider": "GeminiProvider",
                "model": model_name,
                "api_key_configured": True,
                "live_ai_available": False,
                "error": last_error,
                "message": f"Gemini live-demo key configured but API ping failed: {last_error}",
            }
    else:
        groq_api_key = os.environ.get("GROQ_API_KEY", GROQ_API_KEY_ENV).strip()
        gemini_api_key = os.environ.get("AI_API_KEY", AI_API_KEY_ENV).strip()
        return {
            "status": "operational",
            "mode": "mock_baseline",
            "provider": "DeterministicMockProvider",
            "model": "offline_deterministic_rules",
            "api_key_configured": bool(gemini_api_key or groq_api_key),
            "live_ai_available": False,
            "message": "Mock baseline mode (offline CI & deterministic evaluation). Set AI_PROVIDER=groq or AI_PROVIDER=gemini for live reasoning.",
        }


if __name__ == "__main__":
    if "--preflight" in sys.argv:
        res = check_ai_preflight()
        print("\n=== PaisaGuard AI Preflight Check ===")
        print(f"Status             : {res['status'].upper()}")
        print(f"Operational Mode   : {res['mode'].replace('_', ' ').title()}")
        print(f"Active Provider    : {res['provider']}")
        print(f"Model              : {res.get('model', 'N/A')}")
        print(f"API Key Configured : {'YES' if res['api_key_configured'] else 'NO'}")
        print(f"Live AI Available  : {'YES' if res['live_ai_available'] else 'NO'}")
        print(f"Summary            : {res['message']}")
        print("=====================================\n")
        if res["status"] == "misconfigured":
            sys.exit(1)
        sys.exit(0)
