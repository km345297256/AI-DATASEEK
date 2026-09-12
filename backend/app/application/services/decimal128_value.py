"""Bounded, exact BSON Decimal128 BID/text validation; no native dependency.

Compatibility oracle: MongoDB PyMongo 4.17.0 ``bson/decimal128.py``:
https://github.com/mongodb/mongo-python-driver/blob/4.17.0/bson/decimal128.py
Wire/text specification:
https://github.com/mongodb/specifications/blob/master/source/bson-decimal128/decimal128.md

The independently implemented decoder preserves the driver's cohort, signed-zero
and NaN presentation. It never converts the supplied text to a floating value.
"""

import re

_BID = re.compile(r"[0-9a-f]{32}")
_SIGN = 0x8000000000000000
_NAN = 0x7C00000000000000
_INFINITY = 0x7800000000000000
_STEERING = 0x6000000000000000


def _canonical_decimal128(bid: str) -> str | None:
    raw = bytes.fromhex(bid)
    low = int.from_bytes(raw[:8], "little")
    high = int.from_bytes(raw[8:], "little")
    sign = "-" if high & _SIGN else ""
    # PyMongo renders all signed/signaling/payload NaNs as the same text.
    if high & _NAN == _NAN:
        return "NaN"
    if high & _INFINITY == _INFINITY:
        return sign + "Infinity"
    if high & _STEERING == _STEERING:
        # The reserved finite coefficient form is interpreted as zero by the
        # pinned driver, retaining its sign and encoded quantum exponent.
        exponent = ((high & 0x1FFFE00000000000) >> 47) - 6176
        coefficient = 0
    else:
        exponent = ((high & 0x7FFF800000000000) >> 49) - 6176
        coefficient = ((high & 0x0001FFFFFFFFFFFF) << 64) | low
        # A 113-bit coefficient can contain 35 decimal digits. The driver's
        # precision-34 context permits an exact trailing-zero reduction only;
        # any nonzero discarded digit raises Inexact and must be rejected.
        if coefficient >= 10**34:
            if coefficient % 10:
                return None
            coefficient //= 10
            exponent += 1
    if not -6176 <= exponent <= 6111:
        return None
    digits = str(coefficient)
    adjusted = exponent + len(digits) - 1
    # This branch can insert at most five leading fractional zeros: the
    # adjusted-exponent condition prevents a huge decimal expansion.
    if exponent <= 0 and adjusted >= -6:
        point = len(digits) + exponent
        if exponent == 0:
            rendered = digits
        elif point > 0:
            rendered = digits[:point] + "." + digits[point:]
        else:
            rendered = "0." + "0" * -point + digits
    else:
        mantissa = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        rendered = mantissa + "E" + ("+" if adjusted >= 0 else "-") + str(abs(adjusted))
    return sign + rendered


def validate_decimal128(value: object, bid: object) -> bool:
    """Accept only the canonical driver text for exactly these 16 BID bytes."""
    if type(value) is not str or not 1 <= len(value) <= 50:
        return False
    if type(bid) is not str or len(bid) != 32 or _BID.fullmatch(bid) is None:
        return False
    return value == _canonical_decimal128(bid)
