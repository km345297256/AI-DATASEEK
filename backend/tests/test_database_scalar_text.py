"""Shared literal corpus for browser, backend and sandbox; no native DB import."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES = json.loads((ROOT / "sandbox/tests/fixtures/database/scalar-cases.json").read_text())


def load_contract(relative):
    spec = importlib.util.spec_from_file_location("scalar_contract", ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONTRACTS = [load_contract("backend/app/application/services/database_table_visualization.py"),
             load_contract("sandbox/app/services/database_table_payload.py")]


@pytest.mark.parametrize("kind,value", CASES["accepted"])
def test_scalar_literals_preserve_source_verbatim(kind, value):
    for contract in CONTRACTS:
        cell = {"type": kind, "value": value}
        contract.validate_cell(cell)
        assert cell == {"type": kind, "value": value}


@pytest.mark.parametrize("kind,value", CASES["rejected"])
def test_scalar_literals_reject_wrong_components_and_lexical_forms(kind, value):
    for contract in CONTRACTS:
        with pytest.raises(contract.DatabaseTableError):
            contract.validate_cell({"type": kind, "value": value})


def test_untyped_text_not_reinterpreted_as_a_date_or_uuid():
    for contract in CONTRACTS:
        contract.validate_cell({"type": "text", "value": "foo 24:00:01"})


def test_scalar_contract_copies_stay_identical():
    assert (ROOT / "backend/app/application/services/database_table_visualization.py").read_bytes() == (ROOT / "sandbox/app/services/database_table_payload.py").read_bytes()
