"""Exact, bounded scalar validation independent from the records protocol."""
from decimal import DecimalException
import json
from pathlib import Path
import random

from bson.decimal128 import Decimal128
import pytest

from app.services.decimal128_value import validate_decimal128


ROOT = Path(__file__).resolve().parents[2]
ORACLE = json.loads((ROOT / "frontend/tests/browser/decimal128-oracle.json").read_text())


@pytest.mark.parametrize("case", ORACLE["cases"], ids=lambda case: case["name"])
def test_matches_pinned_native_oracle(case):
    value, bid = case["value"], case["bid"]
    if value is None:
        for text in ["0", "-0", "NaN", "Infinity", "1", "9" * 40]:
            assert validate_decimal128(text, bid) is False
    else:
        assert validate_decimal128(value, bid) is True
        assert validate_decimal128(value + "0", bid) is False
        assert validate_decimal128(" " + value, bid) is False


def test_live_native_random_bid_differential():
    rng = random.Random(987411)
    for _ in range(10000):
        raw = rng.getrandbits(128).to_bytes(16, "little")
        try:
            value = str(Decimal128.from_bid(raw))
        except DecimalException:
            assert not any(validate_decimal128(text, raw.hex()) for text in ["0", "NaN", "Infinity", "1"])
        else:
            assert validate_decimal128(value, raw.hex())
            assert not validate_decimal128(value + "0", raw.hex())


@pytest.mark.parametrize("value,bid", [
    (None, "0" * 32), (0, "0" * 32), (True, "0" * 32), ({}, "0" * 32),
    ("NaN", None), ("NaN", 0), ("NaN", {}), ("NaN", ["0"] * 32),
    ("", "0" * 32), ("9" * 51, "0" * 32), ("0", "0" * 31),
    ("0", "0" * 33), ("NaN", "A" * 32), ("NaN", "g" * 32),
    ("NaN", "0" * 31 + "\n"), ("NaN", "0" * 32 + "\n"),
    ("1e0", "01000000000000000000000000004030"),
])
def test_bad_types_spelling_or_unbounded_inputs_rejected(value, bid):
    assert validate_decimal128(value, bid) is False


def test_numeric_equivalence_does_not_erase_cohort_or_negative_zero():
    for source, impostors in [
        ("-0.0000", ["0.0000", "-0", "0", "-0E-4"]),
        ("1.2300", ["1.23", "1.23000", "12300E-4"]),
        ("NaN", ["-NaN", "sNaN", "nan"]),
        ("Infinity", ["+Infinity", "inf", "1E9999"]),
    ]:
        bid = Decimal128(source).bid.hex()
        assert validate_decimal128(source, bid)
        assert all(not validate_decimal128(value, bid) for value in impostors)


def test_backend_and_sandbox_helpers_are_byte_identical():
    assert (ROOT / "sandbox/app/services/decimal128_value.py").read_bytes() == (
        ROOT / "backend/app/application/services/decimal128_value.py"
    ).read_bytes()
