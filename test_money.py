"""
test_money.py — Unit tests for monetary precision hygiene and currency conversions.

Covers:
  - to_decimal safe conversion from float, int, str, Decimal, None, invalid values
  - round_curr with standard ROUND_HALF_UP commercial rounding
  - Edge cases in rounding (e.g. 0.005 -> 0.01, 0.0049 -> 0.00)
  - to_paise lossless integer conversion
  - paise_to_rupees roundtrip fidelity
  - calc_mdr_fee_and_tax and sub-paise rounding drift calculation
"""

from decimal import Decimal
import pytest
from money import (
    to_decimal,
    round_curr,
    to_paise,
    paise_to_rupees,
    calc_mdr_fee_and_tax
)


def test_to_decimal_basic():
    assert to_decimal(100) == Decimal("100")
    assert to_decimal(100.5) == Decimal("100.5")
    assert to_decimal("2450.75") == Decimal("2450.75")
    assert to_decimal(Decimal("30.00")) == Decimal("30.00")


def test_to_decimal_defensive_none_and_empty():
    assert to_decimal(None) == Decimal("0.00")
    assert to_decimal("") == Decimal("0.00")
    assert to_decimal("   ") == Decimal("0.00")
    assert to_decimal("invalid-string") == Decimal("0.00")
    assert to_decimal(None, default="10.00") == Decimal("10.00")


def test_round_curr_commercial_rounding():
    # 0.005 rounds up to 0.01 in commercial banking (ROUND_HALF_UP)
    assert round_curr(Decimal("0.005")) == Decimal("0.01")
    assert round_curr(Decimal("0.0049")) == Decimal("0.00")
    assert round_curr(Decimal("1.255")) == Decimal("1.26")
    assert round_curr(Decimal("1.254")) == Decimal("1.25")
    assert round_curr(1499.00) == Decimal("1499.00")


def test_to_paise_and_paise_to_rupees_roundtrip():
    amounts = [
        Decimal("499.00"),
        Decimal("899.50"),
        Decimal("1299.99"),
        Decimal("0.01"),
        Decimal("0.00"),
        Decimal("125000.00")
    ]
    for amt in amounts:
        paise = to_paise(amt)
        # Verify paise is integer
        assert isinstance(paise, int)
        # Convert back
        recovered = paise_to_rupees(paise)
        assert recovered == amt


def test_calc_mdr_fee_and_tax_drift():
    # Test ₹1299.50 at standard 2% MDR and 18% GST
    gross = Decimal("1299.50")
    expected_fee, expected_tax, total_deduction, drift = calc_mdr_fee_and_tax(gross)

    # 1299.50 * 0.02 = 25.99 exact
    assert expected_fee == Decimal("25.99")
    # 25.99 * 0.18 = 4.6782 -> rounded to 4.68
    assert expected_tax == Decimal("4.68")
    assert total_deduction == Decimal("30.67")
    # Mathematical drift = (25.99 + 4.68) - (25.99 + 4.6782) = 0.0018
    assert abs(drift - Decimal("0.0018")) < Decimal("0.000001")


def test_sub_paise_drift_is_bounded():
    # Over 100 typical e-commerce amounts, cumulative drift must be bounded within ±₹0.50
    amounts = [
        Decimal("499.00"), Decimal("899.00"), Decimal("1299.50"), Decimal("1499.00"),
        Decimal("2499.00"), Decimal("3999.00"), Decimal("5490.00"), Decimal("7999.00")
    ]
    total_drift = Decimal("0.0000")
    for i in range(100):
        amt = amounts[i % len(amounts)]
        _, _, _, drift = calc_mdr_fee_and_tax(amt)
        total_drift += drift

    assert abs(total_drift) < Decimal("0.50")
