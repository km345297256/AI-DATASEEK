"""Backend needs no BSON/native dependency to authenticate Decimal128 text."""
import json
from pathlib import Path

from app.application.services.decimal128_value import validate_decimal128


def test_byte_identical_backend_decoder_matches_native_oracle_without_bson_import():
    root = Path(__file__).resolve().parents[2]
    source = root / "backend/app/application/services/decimal128_value.py"
    assert source.read_bytes() == (root / "sandbox/app/services/decimal128_value.py").read_bytes()
    oracle = json.loads((root / "frontend/tests/browser/decimal128-oracle.json").read_text())
    assert oracle["version"] == "4.17.0"
    for case in oracle["cases"]:
        value, bid = case["value"], case["bid"]
        if value is None:
            assert not any(validate_decimal128(text, bid) for text in ["0", "NaN", "Infinity", "1"])
        else:
            assert validate_decimal128(value, bid), case["name"]
            assert not validate_decimal128(value + "0", bid), case["name"]
    # This validator's implementation imports only a bounded regex, not a
    # BSON decoder, database client or decimal context with mutable precision.
    import ast
    imports = [node for node in ast.walk(ast.parse(source.read_text())) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert len(imports) == 1
    assert isinstance(imports[0], ast.Import)
    assert [name.name for name in imports[0].names] == ["re"]
