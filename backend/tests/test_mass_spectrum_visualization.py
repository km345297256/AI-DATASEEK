import copy
import json
from pathlib import Path
import pytest
from app.application.services.mass_spectrum_visualization import MassSpectrumError, validate_mass_spectrum_options, validate_mass_spectrum_payload

PATH = Path(__file__).resolve().parents[2] / "frontend/tests/browser/mass-spectrum-data.json"


@pytest.fixture
def data(): return json.loads(PATH.read_text())


@pytest.mark.parametrize("name", ["mgf_tree", "mgf_series", "mzml_tree", "mzml_series", "page_first", "page_second"])
def test_actual_reader_payload_matches_request_and_source(data, name):
    v = data[name]
    assert validate_mass_spectrum_payload(v, kind=v["kind"], options=v["selected"], size=v["metadata"]["source_bytes"], fmt=v["metadata"]["format"]) is v


CHANGES = {
    "contract_bool": lambda v: v.__setitem__("contract_version", True), "unknown": lambda v: v.__setitem__("url", "https://invalid"),
    "reader": lambda v: v.__setitem__("reader", "mca"), "kind": lambda v: v.__setitem__("kind", "image"),
    "media": lambda v: v.__setitem__("media_type", "text/html"), "sampled": lambda v: v.__setitem__("sampled", True),
    "warnings": lambda v: v.__setitem__("warnings", []), "choices": lambda v: v["choices"].__setitem__("extra", 1),
    "duplicate": lambda v: v["choices"]["spectra"].append(v["choices"]["spectra"][0]),
    "id": lambda v: v["choices"]["spectra"][0].__setitem__("id", "s-999999"),
    "path": lambda v: v["choices"]["spectra"][0].__setitem__("id", "/private"),
    "index": lambda v: v["choices"]["spectra"][0].__setitem__("index", True),
    "points": lambda v: v["choices"]["spectra"][0].__setitem__("points", 16385),
    "profile": lambda v: v["choices"]["spectra"][0].__setitem__("representation", "unknown"),
    "mz_unit": lambda v: v["choices"]["spectra"][0].__setitem__("mz_unit", "seconds"),
    "intensity_unit": lambda v: v["choices"]["spectra"][0].__setitem__("intensity_unit", "<script>"),
    "time": lambda v: v["choices"]["spectra"][0].__setitem__("retention_time", -1),
    "time_unit": lambda v: v["choices"]["spectra"][0].__setitem__("time_unit", None),
    "precursor": lambda v: v["choices"]["spectra"][0].__setitem__("precursor_mz", float("inf")),
    "level": lambda v: v["choices"]["spectra"][0].__setitem__("ms_level", True),
    "reason": lambda v: v["choices"]["spectra"][0].__setitem__("reason", "other"),
    "selectable": lambda v: v["choices"]["spectra"][0].__setitem__("selectable", False),
    "selected_extra": lambda v: v["selected"].__setitem__("path", "/private"),
    "selected_other": lambda v: v["selected"].__setitem__("spectrum", "s-000001"),
    "meta_extra": lambda v: v["metadata"].__setitem__("secret", 1),
    "source": lambda v: v["metadata"].__setitem__("source_bytes", 16 * 1024**2 + 1),
    "total": lambda v: v["metadata"].__setitem__("total_spectra", True),
    "offset": lambda v: v["metadata"].__setitem__("offset", 1),
    "next": lambda v: v["metadata"].__setitem__("next_offset", 64),
    "decoded": lambda v: v["metadata"].__setitem__("decoded_bytes", 1),
    "output": lambda v: v["metadata"].__setitem__("output_points", 1),
    "mode": lambda v: v["metadata"].__setitem__("input_mode", "window"),
    "dialect": lambda v: v["metadata"].__setitem__("dialect", "mzml-all"),
    "format": lambda v: v["metadata"].__setitem__("format", "mca"),
    "shape": lambda v: v["array"].__setitem__("shape", [2, 4]),
    "dimensions": lambda v: v["array"].__setitem__("dimensions", ["time", "counts"]),
    "value_bool": lambda v: v["array"]["values"].__setitem__(0, True),
    "value_null": lambda v: v["array"]["values"].__setitem__(0, None),
    "value_nan": lambda v: v["array"]["values"].__setitem__(0, float("nan")),
    "value_order": lambda v: v["array"]["values"].__setitem__(2, 99),
    "value_negative_mz": lambda v: v["array"]["values"].__setitem__(0, -1),
    "length": lambda v: v["array"]["values"].pop(),
}


@pytest.mark.parametrize("name", CHANGES)
def test_adversarial_schema_and_scientific_semantics_fail_closed(data, name):
    v = data["mzml_series"]; CHANGES[name](v)
    with pytest.raises(MassSpectrumError): validate_mass_spectrum_payload(v)


@pytest.mark.parametrize("binding,value", [("kind", "tree"), ("fmt", "mgf"), ("size", 1), ("size", True), ("options", {"spectrum": "s-000001"}), ("limit", 128)])
def test_valid_response_must_bind_to_exact_request(data, binding, value):
    with pytest.raises(MassSpectrumError): validate_mass_spectrum_payload(data["mzml_series"], **{binding: value})


@pytest.mark.parametrize("kind,options", [("tree", {"offset": 1}), ("tree", {"offset": True}), ("tree", {"offset": 1024}), ("series", {}), ("series", {"spectrum": "s-000000", "url": "x"}), ("map", {})])
def test_options_cannot_expand_capability(kind, options):
    with pytest.raises(MassSpectrumError): validate_mass_spectrum_options(kind, options)


def test_canonical_first_page_and_last_page_shape(data):
    assert validate_mass_spectrum_options("tree", {}) == {"offset": 0}
    assert validate_mass_spectrum_payload(data["page_first"], options={}) is data["page_first"]
    v = data["page_second"]; v["choices"]["spectra"] = []; v["tree"] = []
    with pytest.raises(MassSpectrumError): validate_mass_spectrum_payload(v)
