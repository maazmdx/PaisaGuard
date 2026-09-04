"""
policy_gate.py — Deterministic Financial Policy Gatekeeper for PaisaGuard.

Architectural Guarantees:
1. Hard Safety Ceilings:
   - Maximum allowable auto-override: ₹50.00 (5,000 paise).
   - Maximum allowable MDR rate: 3.50% (350 basis points).
2. Action Whitelist:
   - Only pre-approved action types permitted.
3. Deterministic Evaluation:
   - Evaluates economic risk purely through arithmetic. AI recommendations
     violating policy are rejected and flagged for mandatory human CFO sign-off.
"""

from typing import Tuple

from money import format_paise_inr, require_paise

ALLOWED_ACTIONS = {
    "REQUEST_MERCHANT_CLARIFICATION",
    "ACCEPT_SURCHARGE_ADJUSTMENT",
    "INITIATE_GATEWAY_DISPUTE",
    "AWAIT_SETTLEMENT",
    "AWAIT_BANK_CREDIT",
    "ABSTAIN",
}

MAX_ALLOWABLE_VARIANCE_PAISE = 5000  # ₹50.00 hard limit for automated policy approval
MAX_ALLOWABLE_MDR_BPS = 350  # 3.50% absolute contract cap


class PolicyGatekeeper:
    """
    Deterministic safety boundary between AI suggestions and FinOps financial disposition.
    """

    @classmethod
    def evaluate(
        cls,
        gross_amount_paise: int,
        actual_fee_paise: int,
        expected_fee_paise: int,
        variance_paise: int,
        proposed_action: str,
    ) -> Tuple[bool, str]:
        """
        Validates economic safety boundaries.
        Returns: (policy_approved: bool, policy_reason: str)
        """
        require_paise(gross_amount_paise)
        require_paise(actual_fee_paise)
        require_paise(expected_fee_paise)
        require_paise(variance_paise)

        # 1. Action Whitelist Check
        if proposed_action not in ALLOWED_ACTIONS:
            return (
                False,
                f"Policy Gate Rejected: Action '{proposed_action}' is not in approved FinOps action whitelist.",
            )

        # 2. If agent abstained, pass through as policy abstention
        if proposed_action == "ABSTAIN":
            return True, "Policy Gatekeeper: Agent abstention acknowledged within safety boundaries."

        # 3. Variance Ceiling Check (if action suggests accepting adjustment)
        if proposed_action == "ACCEPT_SURCHARGE_ADJUSTMENT":
            if abs(variance_paise) > MAX_ALLOWABLE_VARIANCE_PAISE:
                return False, (
                    f"Policy Gate Rejected: Variance of {format_paise_inr(abs(variance_paise))} "
                    f"exceeds hard safety ceiling of {format_paise_inr(MAX_ALLOWABLE_VARIANCE_PAISE)}. "
                    "Manual CFO sign-off required."
                )

            # 4. Effective MDR Rate Cap Check
            if gross_amount_paise > 0:
                effective_mdr_bps = (actual_fee_paise * 10000) // gross_amount_paise
                if effective_mdr_bps > MAX_ALLOWABLE_MDR_BPS:
                    return False, (
                        f"Policy Gate Rejected: Effective fee rate of {effective_mdr_bps / 100:.2f}% "
                        f"breaches statutory merchant contract cap of {MAX_ALLOWABLE_MDR_BPS / 100:.2f}%."
                    )

        return True, "Policy Gatekeeper: Verified within safe deterministic economic bounds."
