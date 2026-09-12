import copy
import importlib.util
from pathlib import Path

import pytest

from app.application.services.mca_visualization import (
    ScientificPreviewRejected, validate_mca_options, validate_mca_payload,
)


def reader():
    path = Path(__file__).resolve().parents[2] / "sandbox/app/services/bounded_mca_reader.py"
    spec = importlib.util.spec_from_file_location("independent_mca_contract_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def payload(calibrated=False):
    calibration = "<<CALIBRATION>>\nLABEL - keV\n0 1.5\n3 7.5\n" if calibrated else ""
    source = ("<<PMCA SPECTRUM>>\nLIVE_TIME - 2.5\n" + calibration + "<<DATA>>\n0\n10\n20\n30\n<<END>>\n").encode()
    return reader().mca_preview(source)


@pytest.mark.parametrize("calibrated", [False, True])
def test_real_independent_reader_payload_is_valid(calibrated):
    data = payload(calibrated)
    assert validate_mca_payload(data) is data


@pytest.mark.parametrize("options", [[], None, False, "", {"fit": True}, {"path": "/private"},
                                       {"energy_unit": "keV"}, {"calibration": [0, 1]}])
def test_rejects_undeclared_mca_options(options):
    with pytest.raises(ScientificPreviewRejected):
        validate_mca_options(options)


def test_no_options_is_valid():
    assert validate_mca_options({}) is None


@pytest.mark.parametrize("key,value", [("contract_version", True), ("contract_version", 1), ("reader", "root"),
    ("type", "jcamp"), ("kind", "image"), ("media_type", "text/html"), ("sampled", True),
    ("metadata", []), ("array", None), ("warnings", ["file:/private"]), ("data_base64", "AA=="), ("path", "/private")])
def test_rejects_envelope_escalation(key, value):
    data = payload()
    data[key] = value
    with pytest.raises(ValueError):
        validate_mca_payload(data)


@pytest.mark.parametrize("key,value", [("format", "spe"), ("dialect", "binary"), ("input_bytes", 4194305),
    ("input_bytes", True), ("channels", 8193), ("channels", 0), ("channels", True),
    ("raw_counts", False), ("fitting", True), ("header_text_hidden", False),
    ("live_time_seconds", -1), ("real_time_seconds", float("inf")), ("live_time_seconds", True),
    ("calibration_points", [[float("nan"), 0]]), ("calibration_points", [[1, 2, 3]]),
    ("calibration_points", [[0, 1]] * 65), ("limits", {"max_channels": 99999, "max_input_bytes": 4194304}),
    ("patient", "PRIVATE"), ("path", "/private")])
def test_rejects_metadata_and_budget_escalation(key, value):
    data = payload()
    data["metadata"][key] = value
    with pytest.raises(ValueError):
        validate_mca_payload(data)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), None, True, -1, 1.5, 2**53, "10", 10**1000])
def test_count_type_precision_and_finiteness(value):
    data = payload()
    data["array"]["values"][1] = value
    with pytest.raises(ValueError):
        validate_mca_payload(data)


@pytest.mark.parametrize("mutation", [lambda d: d["array"].update(shape=[4, True]),
    lambda d: d["array"].update(shape=[3, 2]), lambda d: d["array"].update(dimensions=["time", "counts"]),
    lambda d: d["array"].update(values=[0, 1]), lambda d: d["array"].update(url="https://invalid.example"),
    lambda d: d["array"]["values"].__setitem__(0, .1), lambda d: d["array"]["values"].__setitem__(0, 0.0),
    lambda d: d["metadata"].update(energy_unit="keV"), lambda d: d["metadata"].update(axis="energy"),
    lambda d: d["metadata"].update(calibration_coefficients=[0, 1, 0])])
def test_uncalibrated_cannot_claim_energy_or_distort_channels(mutation):
    data = payload()
    mutation(data)
    with pytest.raises(ValueError):
        validate_mca_payload(data)


@pytest.mark.parametrize("mutation", [lambda d: d["metadata"].update(energy_unit=None),
    lambda d: d["metadata"].update(energy_unit="mV"), lambda d: d["metadata"].update(energy_unit=[]),
    lambda d: d["metadata"].update(calibration_points=[]),
    lambda d: d["metadata"].update(calibration_points=[[0, 1.5], [0, 7.5]]),
    lambda d: d["metadata"].update(calibration_points=[[0, 1.5], [4, 7.5]]),
    lambda d: d["metadata"].update(calibration_coefficients=[1.5, -2, 0]),
    lambda d: d["metadata"].update(calibration_coefficients=[1.5, 2, .01]),
    lambda d: d["metadata"].update(calibration_coefficients=[1.5, 3, 0]),
    lambda d: d["metadata"].update(calibration_coefficients=[1.5, float("nan"), 0]),
    lambda d: d["array"]["values"].__setitem__(2, 100), lambda d: d["array"].update(dimensions=["channel", "counts"]),
    lambda d: d["warnings"].pop()])
def test_energy_must_match_explicit_calibration(mutation):
    data = payload(True)
    mutation(data)
    with pytest.raises(ValueError):
        validate_mca_payload(data)


def test_unit_validation_does_not_mutate_input():
    data = payload(True)
    before = copy.deepcopy(data)
    validate_mca_payload(data)
    assert data == before


def test_maximum_channel_count_and_exact_counts_accepted():
    data = reader().mca_preview(b"<<PMCA SPECTRUM>>\n<<DATA>>\n" + (str(2**53 - 1).encode() + b"\n") * 8192 + b"<<END>>\n")
    validate_mca_payload(data)
    assert data["array"]["values"][1] == 2**53 - 1
