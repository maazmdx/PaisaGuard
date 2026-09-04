"""
money.py — Centralized Canonical Integer Paise Precision for PaisaGuard.

Architectural Rule:
- All financial state in database, API, and engine MUST be represented as integer paise.
- No float calculations.
- parse_inr_to_paise(str | Decimal): Converts fixture/UI rupee input into canonical integer paise.
- require_paise(val): Validates that an input is strictly an integer representing paise.
- calc_mdr_fee_and_tax_paise(): Pure integer paise MDR and GST calculations with commercial rounding.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Union, Any, Tuple

TWO_PLACES = Decimal("0.01")


def parse_inr_to_paise(val: Union[str, Decimal]) -> int:
    """
    Parses a human or fixture INR string or Decimal to exact integer paise.
    e.g. "1499.50" -> 149950 paise.
    
    SAFETY CONSTRAINT:
    Strictly rejects raw integer amounts to eliminate ambiguity between integer rupees and integer paise.
    """
    if isinstance(val, bool) or isinstance(val, int):
        raise TypeError(
            f"parse_inr_to_paise expects str or Decimal INR amount, not raw int ({val}). "
            "In PaisaGuard, integers always represent paise; use require_paise(val) directly."
        )
    if val is None:
        raise ValueError("Cannot parse None to paise.")

    val_str = str(val).strip()
    if not val_str:
        raise ValueError("Cannot parse empty string to paise.")

    try:
        dec = Decimal(val_str)
    except Exception as exc:
        raise ValueError(f"Invalid monetary string '{val_str}': {exc}") from exc

    paise_dec = (dec * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(paise_dec)


def require_paise(val: Any) -> int:
    """
    Validates and enforces that an input is strictly an integer representing paise.
    Guards against floats, strings, booleans, and None in internal financial operations.
    """
    if isinstance(val, bool) or not isinstance(val, int):
        raise TypeError(f"Expected canonical integer paise, received {type(val).__name__}: {val}")
    return val


def paise_to_rupees(paise: int) -> Decimal:
    """
    Converts integer paise to a Decimal INR amount for human display or export only.
    e.g. 149950 paise -> Decimal('1499.50').
    """
    require_paise(paise)
    p_dec = Decimal(paise)
    return (p_dec / Decimal("100")).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def format_paise_inr(paise: int) -> str:
    """
    Formats an integer paise value into a standard Indian currency display string.
    e.g. 149950 -> "₹1,499.50", -5000 -> "-₹50.00"
    """
    rupees = paise_to_rupees(paise)
    if rupees < 0:
        return f"-₹{abs(rupees):,.2f}"
    return f"₹{rupees:,.2f}"


def calc_mdr_fee_and_tax_paise(
    gross_paise: int,
    mdr_bps: int = 200,      # Default 2.00% MDR = 200 basis points
    gst_bps: int = 1800      # Default 18.00% GST on fee = 1800 basis points
) -> Tuple[int, int]:
    """
    Computes exact contracted MDR fee and GST tax on fee in canonical integer paise.
    Uses commercial statutory rounding (ROUND_HALF_UP equivalent) in pure integer arithmetic:
      half_up_int(x, denom) = (x + denom // 2) // denom

    Returns:
        (expected_fee_paise: int, expected_tax_paise: int)
    """
    require_paise(gross_paise)
    require_paise(mdr_bps)
    require_paise(gst_bps)

    if gross_paise <= 0:
        return 0, 0

    # Fee = gross_paise * (mdr_bps / 10000) with commercial half-up rounding
    fee_paise = (gross_paise * mdr_bps + 5000) // 10000

    # GST = fee_paise * (gst_bps / 10000) with commercial half-up rounding
    tax_paise = (fee_paise * gst_bps + 5000) // 10000

    return fee_paise, tax_paise


def to_decimal(val: Any, default: str = "0.00") -> Decimal:
    """Safe Decimal parser helper for reporting / display."""
    if val is None:
        return Decimal(default)
    if isinstance(val, Decimal):
        return val
    try:
        return Decimal(str(val).strip())
    except Exception:
        return Decimal(default)


def round_curr(val: Any) -> Decimal:
    """Quantizes a value to 2 decimal places using standard commercial rounding."""
    return to_decimal(val).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
