"""
test_money.py — Unit tests for canonical integer paise precision and safety guards.
"""

from decimal import Decimal

import pytest

from money import (
    calc_mdr_fee_and_tax_paise,
    format_paise_inr,
    paise_to_rupees,
    parse_inr_to_paise,
    require_paise,
    round_curr,
    to_decimal,
)


def test_parse_inr_to_paise_success():
    assert parse_inr_to_paise("1499.50") == 149950
    assert parse_inr_to_paise(Decimal("1499.50")) == 149950
    assert parse_inr_to_paise("0.00") == 0
    assert parse_inr_to_paise("0.01") == 1
    assert parse_inr_to_paise("499.00") == 49900
    assert parse_inr_to_paise("125000.00") == 12500000


def test_parse_inr_to_paise_rejects_raw_int():
    # Crucial safety requirement: parse_inr_to_paise must reject raw integers to avoid rupee/paise ambiguity
    with pytest.raises(TypeError, match="expects str or Decimal"):
        parse_inr_to_paise(1500)

    with pytest.raises(TypeError, match="expects str or Decimal"):
        parse_inr_to_paise(True)


def test_require_paise_type_guards():
    assert require_paise(149950) == 149950
    assert require_paise(0) == 0
    assert require_paise(-500) == -500

    with pytest.raises(TypeError):
        require_paise(1499.50)  # Float rejected

    with pytest.raises(TypeError):
        require_paise("149950")  # String rejected

    with pytest.raises(TypeError):
        require_paise(True)  # Bool rejected

    with pytest.raises(TypeError):
        require_paise(None)  # None rejected


def test_paise_to_rupees_roundtrip():
    amounts_str = ["0.00", "0.01", "499.00", "899.50", "1299.99", "12500.00"]
    for s in amounts_str:
        paise = parse_inr_to_paise(s)
        rupees = paise_to_rupees(paise)
        assert rupees == Decimal(s)


def test_format_paise_inr():
    assert format_paise_inr(149950) == "₹1,499.50"
    assert format_paise_inr(0) == "₹0.00"
    assert format_paise_inr(12500000) == "₹125,000.00"
    assert format_paise_inr(-5000) == "-₹50.00"


def test_calc_mdr_fee_and_tax_paise():
    # ₹1299.50 = 129950 paise at 2.0% MDR (200 bps) and 18% GST (1800 bps)
    gross_paise = 129950
    fee_paise, tax_paise = calc_mdr_fee_and_tax_paise(gross_paise, mdr_bps=200, gst_bps=1800)

    # 129950 * 0.02 = 2599 paise exact (₹25.99)
    assert fee_paise == 2599
    # 2599 * 0.18 = 467.82 -> rounded half up = 468 paise (₹4.68)
    assert tax_paise == 468


def test_calc_mdr_fee_and_tax_paise_corporate_rate():
    # ₹2000.00 = 200000 paise at corporate card 2.5% MDR (250 bps) and 18% GST (1800 bps)
    gross_paise = 200000
    fee_paise, tax_paise = calc_mdr_fee_and_tax_paise(gross_paise, mdr_bps=250, gst_bps=1800)

    # 200000 * 0.025 = 5000 paise (₹50.00)
    assert fee_paise == 5000
    # 5000 * 0.18 = 900 paise (₹9.00)
    assert tax_paise == 900


def test_legacy_helpers():
    assert to_decimal(None) == Decimal("0.00")
    assert round_curr(Decimal("1.255")) == Decimal("1.26")
    assert round_curr(Decimal("1.254")) == Decimal("1.25")
