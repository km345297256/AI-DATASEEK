"""Generate original BID/text cases using pinned PyMongo, never our helper.

Run with PyMongo 4.17.0 installed. Only this declared synthetic fixture is
written; no database, BSON document decoder or network connection is used.
"""
import hashlib
import inspect
import json
from decimal import DecimalException
from pathlib import Path
import random

import bson.decimal128
import pymongo
from bson.decimal128 import Decimal128


def generate():
    assert pymongo.version == "4.17.0", "Use the pinned native oracle version"
    cases = []

    def add(name, raw):
        try:
            value = str(Decimal128.from_bid(raw))
        except DecimalException as error:
            cases.append({"name": name, "bid": raw.hex(), "value": None, "error": type(error).__name__})
        else:
            cases.append({"name": name, "bid": raw.hex(), "value": value})

    for text in [
        "0", "-0", "0.0000", "-0.0000", "0.000000", "-0.000000",
        "0E-7", "-0E-7", "0E-6176", "-0E-6176", "0E+6111", "-0E+6111",
        "1", "-1", "1.2300", "-1.2300", "123.00", "9007199254740993",
        "1E-6", "1E-7", "1E+1", "1E-6176", "-1E-6176", "1E-6143",
        "9.999999999999999999999999999999999E+6144",
        "-9.999999999999999999999999999999999E+6144",
        "123456789012345678901234567890.1234", "NaN", "-NaN", "sNaN", "-sNaN",
        "Infinity", "-Infinity",
    ]:
        add("text:" + text, Decimal128(text).bid)

    for coefficient in [0, 1, 10, 10**33, 10**34 - 1, 10**34, 10**34 + 1,
                        10**34 + 10, 2**113 - 2, 2**113 - 1]:
        for exponent in [-6176, -6175, -7, -6, -1, 0, 1, 6110, 6111]:
            for sign in [0, 1]:
                high = (sign << 63) | ((exponent + 6176) << 49) | (coefficient >> 64)
                low = coefficient & (2**64 - 1)
                add(f"finite:{sign}:{coefficient}:{exponent}", low.to_bytes(8, "little") + high.to_bytes(8, "little"))
    for exponent in [-6176, -7, -6, 0, 6111]:
        for sign in [0, 1]:
            high = (sign << 63) | 0x6000000000000000 | ((exponent + 6176) << 47) | 0x1FFF
            add(f"steering-zero:{sign}:{exponent}", b"\xff" * 8 + high.to_bytes(8, "little"))
    for high in [0x7C0000000000ABCD, 0xFC0000000000ABCD, 0x7E0000000000ABCD,
                 0xFE0000000000ABCD, 0x780000000000ABCD, 0xF80000000000ABCD]:
        add(f"special-payload:{high:x}", b"\xff" * 8 + high.to_bytes(8, "little"))
    add("regression:inexact", bytes.fromhex("9af24c187304af93c0b81376f7facdd3"))
    rng = random.Random(7417)
    for index in range(2048):
        add(f"random-seed7417:{index}", rng.getrandbits(128).to_bytes(16, "little"))
    source = Path(inspect.getfile(bson.decimal128))
    return {
        "oracle": "PyMongo Decimal128.from_bid then str, without DataSeek helpers",
        "version": pymongo.version,
        "source": "https://github.com/mongodb/mongo-python-driver/blob/4.17.0/bson/decimal128.py",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "seed": 7417,
        "cases": cases,
    }


if __name__ == "__main__":
    destination = Path(__file__).resolve().parents[2] / "frontend/tests/browser/decimal128-oracle.json"
    destination.write_text(json.dumps(generate(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote original Decimal128 oracle: {destination.name}")
