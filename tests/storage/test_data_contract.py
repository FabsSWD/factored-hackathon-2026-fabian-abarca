from __future__ import annotations

import pytest

from app.storage import data_contract as dc


def test_expected_sets_match_the_policy() -> None:
    assert dc.TRANSACTION_TYPES == {
        "Purchase",
        "Withdrawal",
        "Transfer",
        "Payment",
        "Deposit",
        "Adjustment",
    }
    assert dc.TRANSACTION_STATUSES == {"Approved", "Pending", "Declined", "Reversed"}
    assert dc.CARD_PRODUCT_TYPES == {"Tarjeta Crédito", "Tarjeta Débito"}
    assert dc.CARD_PRODUCT_TYPES <= dc.PRODUCT_TYPES
    assert "Blocked" in dc.PRODUCT_STATUSES
    assert "Active" in dc.CUSTOMER_STATUSES


def test_amount_usd_sources() -> None:
    assert {s.value for s in dc.AmountUsdSource} == {"source", "fx_rate", "identity", "missing"}


@pytest.mark.parametrize(
    ("age", "band"),
    [
        (18, "18-24"),
        (24, "18-24"),
        (25, "25-34"),
        (34, "25-34"),
        (35, "35-44"),
        (54, "45-54"),
        (55, "55-64"),
        (64, "55-64"),
        (65, "65+"),
        (99, "65+"),
    ],
)
def test_age_band_boundaries(age: int, band: str) -> None:
    assert dc.age_band(age) == band


def test_age_under_18_is_rejected() -> None:
    with pytest.raises(ValueError, match="unexpected customer age"):
        dc.age_band(17)
