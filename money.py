"""
money.py — Centralized Monetary Precision & Currency Math Helpers for PaisaGuard.

Ensures strict IEEE-754 floating-point hygiene across the application.
Provides:
  - to_decimal(): Safe conversion from float, int, str, Decimal, or None to Decimal.
  - round_curr(): Standard commercial rounding (ROUND_HALF_UP) to 2 decimal places (paise precision).
  - to_paise(): Lossless integer paise representation of any currency amount.
  - paise_to_rupees(): Exact conversion of integer paise back to Decimal INR.
  - calc_mdr_fee_and_tax(): Mathematical MDR fee, GST tax, and sub-paise rounding drift calculation.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Union, Optional, Tuple

TWO_PLACES = Decimal("0.01")
FOUR_PLACES = Decimal("0.0001")


def to_decimal(val: Optional[Union[float, int, str, Decimal]], default: str = "0.00") -> Decimal:
    """
    Safely converts arbitrary numeric or string inputs into a Decimal.
    Guards against None, empty strings, and floating-point precision artifacts.
    """
    if val is None:
        return Decimal(default)
    if isinstance(val, Decimal):
        return val
    if isinstance(val, (int, float)):
        # Convert via string to avoid binary floating-point representation traps
        return Decimal(str(val))
    val_str = str(val).strip()
    if not val_str:
        return Decimal(default)
    try:
        return Decimal(val_str)
    except Exception:
        return Decimal(default)


def round_curr(val: Union[Decimal, float, int, str]) -> Decimal:
    """
    Quantizes a numeric value to 2 decimal places using standard commercial
    statutory rounding (ROUND_HALF_UP).
    """
    dec = to_decimal(val)
    return dec.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def to_paise(val: Union[Decimal, float, int, str]) -> int:
    """
    Converts an INR currency amount to an exact integer number of paise.
    e.g., 1499.50 INR -> 149950 paise.
    """
    dec = to_decimal(val)
    paise_dec = (dec * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(paise_dec)


def paise_to_rupees(paise: Union[int, Decimal, str]) -> Decimal:
    """
    Converts an integer number of paise back into a Decimal INR amount.
    e.g., 149950 paise -> Decimal('1499.50').
    """
    p_dec = to_decimal(paise)
    return (p_dec / Decimal("100")).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def calc_mdr_fee_and_tax(
    gross_amount: Decimal,
    mdr_rate: Decimal = Decimal("0.020"),
    gst_rate: Decimal = Decimal("0.18")
) -> Tuple[Decimal, Decimal, Decimal, Decimal]:
    """
    Computes exact contracted MDR fee, GST tax on fee, and sub-paise rounding drift.

    Returns:
        (expected_fee, expected_tax, raw_deduction, sub_paise_drift)
    """
    gross = to_decimal(gross_amount)
    raw_fee = gross * mdr_rate
    expected_fee = round_curr(raw_fee)

    raw_tax = expected_fee * gst_rate
    expected_tax = round_curr(raw_tax)

    raw_total_deduction = raw_fee + raw_tax
    rounded_total_deduction = expected_fee + expected_tax
    drift = rounded_total_deduction - raw_total_deduction

    return expected_fee, expected_tax, rounded_total_deduction, drift
