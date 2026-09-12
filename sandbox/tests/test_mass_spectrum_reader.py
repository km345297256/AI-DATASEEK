import base64
import copy
import importlib.util
import io
import json
from pathlib import Path
import struct
import zlib

import pytest

from app.services import mass_spectrum_reader as reader
from app.services.mass_spectrum_payload import MassSpectrumError, validate_mass_spectrum_payload
from mass_spectrum_fixtures import browser_payloads, mgf_bytes, mzml_bytes, psims_mzml_bytes


def test_actual_official_psims_writer_generates_supported_profile():
    data = psims_mzml_bytes()
    result = preview(data)
    assert result["array"]["values"] == [100, -1, 100.25, 3, 100.5, 7, 100.75, 2]
    assert result["choices"]["spectra"][0]["representation"] == "profile"


def preview(data, fmt="mzml", kind="series", options=None, **kwargs):
    return reader.mass_spectrum_preview(data, fmt, kind, options if options is not None else ({"spectrum": "s-000000"} if kind == "series" else {}), **kwargs)


@pytest.mark.parametrize("width", [4, 8])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("profile", [False, True])
@pytest.mark.parametrize("indexed", [False, True])
def test_real_pyteomics_independent_oracle_inline_arrays_units_and_order(width, compressed, profile, indexed, monkeypatch):
    from pyteomics import mzml
    from psims.controlled_vocabulary.controlled_vocabulary import OBOCache
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("no schema/CV network"))
    vocabulary = OBOCache(enabled=False, use_remote=False).load("http://purl.obolibrary.org/obo/ms/psi-ms.obo")
    data = mzml_bytes(width=width, compressed=compressed, profile=profile, indexed=indexed)
    with mzml.MzML(io.BytesIO(data), read_schema=False, use_index=False, retrieve_refs=False, cv=vocabulary) as handle:
        oracle = next(handle)
    result = preview(data)
    assert result["array"]["values"][::2] == oracle["m/z array"].tolist()
    assert result["array"]["values"][1::2] == oracle["intensity array"].tolist()
    desc = result["choices"]["spectra"][0]
    assert desc["representation"] == ("profile" if profile else "centroid")
    assert desc["intensity_unit"] == "MS:1000131" and desc["time_unit"] == "min" and desc["retention_time"] == 2.5
    assert result["metadata"]["decoded_bytes"] == 8 * width


def test_mgf_oracle_centroid_original_order_no_guessed_intensity_units():
    from pyteomics import mgf
    data = mgf_bytes()
    with mgf.MGF(io.StringIO(data.decode()), read_charges=False) as handle: oracle = next(handle)
    result = preview(data, "mgf")
    assert result["array"]["values"][::2] == oracle["m/z array"].tolist() == [150.5, 100.25, 200.75]
    assert result["array"]["values"][1::2] == oracle["intensity array"].tolist()
    assert result["choices"]["spectra"][0]["intensity_unit"] is None
    assert "/private" not in json.dumps(result) and "TITLE" not in json.dumps(result)


def test_tree_never_base64_or_zlib_decodes(monkeypatch):
    data = mzml_bytes(compressed=True)
    monkeypatch.setattr(base64, "b64decode", lambda *_a, **_k: pytest.fail("no array decode"))
    monkeypatch.setattr(zlib, "decompressobj", lambda *_a, **_k: pytest.fail("no zlib decode"))
    result = preview(data, kind="tree")
    assert "array" not in result and result["metadata"]["decoded_bytes"] == 0


def test_metadata_pages_exactly_64_1_then_reject_past_end():
    data = mgf_bytes(65)
    first, second = preview(data, "mgf", "tree"), preview(data, "mgf", "tree", {"offset": 64})
    assert len(first["choices"]["spectra"]) == 64 and first["metadata"]["next_offset"] == 64
    assert len(second["choices"]["spectra"]) == 1 and second["metadata"]["next_offset"] is None
    with pytest.raises(MassSpectrumError): preview(data, "mgf", "tree", {"offset": 128})


@pytest.mark.parametrize("bad", [
    b'<!DOCTYPE mzML [<!ENTITY secret SYSTEM "file:///private/secret">]>',
    b'<!DOCTYPE mzML [<!ENTITY a "ha"><!ENTITY b "&a;&a;&a;">]>',
    b'<?xml-stylesheet href="https://invalid"?>',
])
def test_xml_dtd_entities_processing_instructions_rejected(bad):
    with pytest.raises(MassSpectrumError): preview(bad + mzml_bytes())


@pytest.mark.parametrize("old,new,reason", [
    (b"MS:1000128", b"MS:1999999", "unsupported-representation"),
    (b"MS:1000523", b"MS:1000519", "unsupported-encoding"),
    (b"MS:1000576", b"MS:1002312", "unsupported-encoding"),
    (b"UO:0000031", b"UO:9999999", "ambiguous-metadata"),
    (b'defaultArrayLength="4"', b'defaultArrayLength="2147483647"', "point-budget"),
    (b'<scanList count="1">', b'<referenceableParamGroupRef ref="external"/><scanList count="1">', "ambiguous-metadata"),
    (b'<binaryDataArray encodedLength=', b'<binaryDataArray externalFile="https://invalid" encodedLength=', "unsupported-encoding"),
])
def test_unsupported_spectra_are_explicitly_disabled_and_never_decoded(old, new, reason, monkeypatch):
    data = mzml_bytes().replace(old, new)
    monkeypatch.setattr(reader, "decode", lambda *_a: pytest.fail("unsupported must not decode"))
    item = preview(data, kind="tree")["choices"]["spectra"][0]
    assert item["selectable"] is False and item["reason"] == reason
    with pytest.raises(MassSpectrumError): preview(data)


@pytest.mark.parametrize("mod", ["invalid-base64", "trailing-zlib", "short-zlib", "expansion", "nan", "unordered-profile", "wrong-count", "duplicate-id", "wrong-index", "deep", "duplicate-cv"])
def test_malformed_array_or_document_rejects(mod):
    data = mzml_bytes(compressed=False)
    if mod == "invalid-base64": data = data.replace(b"<binary>", b"<binary>!", 1).replace(b'encodedLength="44"', b'encodedLength="45"', 1)
    elif mod in {"trailing-zlib", "short-zlib", "expansion"}:
        raw = struct.pack("<4d", 100, 100.25, 100.5, 100.75)
        new = zlib.compress(raw * (100000 if mod == "expansion" else 1))
        if mod == "trailing-zlib": new += b"junk"
        elif mod == "short-zlib": new = new[:-1]
        old = base64.b64encode(raw); encoded = base64.b64encode(new)
        data = data.replace(old, encoded, 1).replace(b'encodedLength="44"', f'encodedLength="{len(encoded)}"'.encode(), 1).replace(b"MS:1000576", b"MS:1000574", 1)
    elif mod == "nan": data = mzml_bytes(intensity=[0, float("nan"), 2, 3])
    elif mod == "unordered-profile": data = mzml_bytes(mz=[100, 99, 101, 102])
    elif mod == "wrong-count": data = data.replace(b'spectrumList count="1"', b'spectrumList count="2"')
    elif mod == "wrong-index": data = data.replace(b'index="0"', b'index="1"')
    elif mod == "duplicate-id":
        start, end = data.index(b"<spectrum id="), data.index(b"</spectrum>") + len(b"</spectrum>")
        data = data[:end] + data[start:end].replace(b'index="0"', b'index="1"') + data[end:]
        data = data.replace(b'spectrumList count="1"', b'spectrumList count="2"')
    elif mod == "deep": data = data.replace(b"<run ", b"<unknown>" * 25 + b"<run ").replace(b"</run>", b"</run>" + b"</unknown>" * 25)
    elif mod == "duplicate-cv": data = data.replace(b'<scanList count="1">', b'<cvParam accession="MS:1000128"/><scanList count="1">')
    with pytest.raises(MassSpectrumError): preview(data)


@pytest.mark.parametrize("bad", [b"BEGIN IONS\n1 2\n", b"BEGIN IONS\n1 2 3\nEND IONS", b"BEGIN IONS\nNaN 2\nEND IONS", b"BEGIN IONS\n1e-999 2\nEND IONS", b"BEGIN IONS\n1 2\nTITLE=late\nEND IONS", b"BEGIN IONS\nPEPMASS=100\nPEPMASS=101\n1 2\nEND IONS", b"BEGIN IONS\nRTINSECONDS=1-2\n1 2\nEND IONS", b"BEGIN IONS\n# inner comment\n1 2\nEND IONS", b"1 2\n"])
def test_mgf_dialect_never_silently_discards_columns_or_ambiguous_metadata(bad):
    with pytest.raises(MassSpectrumError): preview(bad, "mgf")


@pytest.mark.parametrize("options", [{}, {"spectrum": True}, {"spectrum": "s-999999"}, {"spectrum": "s-000000", "path": "/private"}, {"spectrum": 0}])
def test_invalid_selection_before_parsing(options, monkeypatch):
    monkeypatch.setattr(reader, "mzml", lambda *_: pytest.fail("no parse"))
    with pytest.raises(MassSpectrumError): preview(b"xml", options=options)


def test_source_output_and_spectrum_limits():
    with pytest.raises(MassSpectrumError): preview(b"a" * (16 * 1024**2 + 1))
    with pytest.raises(MassSpectrumError): preview(mgf_bytes(1025), "mgf", "tree")
    with pytest.raises(MassSpectrumError): preview(mgf_bytes(), "mgf", output_limit=128)
    data = b"BEGIN IONS\n" + b"100 1\n" * 16385 + b"END IONS\n"
    assert preview(data, "mgf", "tree")["choices"]["spectra"][0]["reason"] == "point-budget"
    with pytest.raises(MassSpectrumError): preview(data, "mgf")


@pytest.mark.parametrize("variant", ["multi-scan", "multi-precursor", "multi-ion", "wrong-precursor-unit", "conflicting-representation", "array-role-conflict", "array-length-conflict"])
def test_multiple_or_conflicting_scientific_metadata_never_silently_selects_first(variant):
    data = mzml_bytes()
    if variant == "multi-scan": data = data.replace(b'<scanList count="1">', b'<scanList count="2"><scan/>')
    elif variant == "multi-precursor": data = data.replace(b'<binaryDataArrayList', b'<precursorList count="2"><precursor/><precursor/></precursorList><binaryDataArrayList', 1)
    elif variant == "multi-ion": data = data.replace(b'<binaryDataArrayList', b'<precursorList count="1"><precursor><selectedIonList count="2"><selectedIon/><selectedIon/></selectedIonList></precursor></precursorList><binaryDataArrayList', 1)
    elif variant == "wrong-precursor-unit": data = data.replace(b'<binaryDataArrayList', b'<precursorList count="1"><precursor><selectedIonList count="1"><selectedIon><cvParam accession="MS:1000744" value="123" unitAccession="UO:0000010"/></selectedIon></selectedIonList></precursor></precursorList><binaryDataArrayList', 1)
    elif variant == "conflicting-representation": data = data.replace(b'<scanList', b'<cvParam accession="MS:1000127"/><scanList', 1)
    elif variant == "array-role-conflict": data = data.replace(b'MS:1000515', b'MS:1000514')
    else: data = data.replace(b'<binaryDataArray encodedLength=', b'<binaryDataArray arrayLength="3" encodedLength=', 1)
    desc = preview(data, kind="tree")["choices"]["spectra"][0]
    assert desc["selectable"] is False
    with pytest.raises(MassSpectrumError): preview(data)


def test_exact_maximum_16384_pairs_not_silently_reduced_to_8192():
    data = b"BEGIN IONS\n" + b"100 1\n" * 16384 + b"END IONS\n"
    value = preview(data, "mgf")
    assert value["array"]["shape"] == [16384, 2] and len(value["array"]["values"]) == 32768
    assert value["sampled"] is False


def test_array_declared_bomb_never_allocates_base64_or_inflater(monkeypatch):
    data = mzml_bytes().replace(b'defaultArrayLength="4"', b'defaultArrayLength="2147483647"')
    monkeypatch.setattr(base64, "b64decode", lambda *_a, **_k: pytest.fail("no base64 allocation"))
    monkeypatch.setattr(zlib, "decompressobj", lambda *_a, **_k: pytest.fail("no inflater"))
    with pytest.raises(MassSpectrumError): preview(data)


def test_inflater_output_limit_is_declared_length_plus_one(monkeypatch):
    original = zlib.decompressobj; seen = []
    class Checked:
        def __init__(self): self.inner = original()
        def decompress(self, raw, max_length): seen.append(max_length); return self.inner.decompress(raw, max_length)
        def __getattr__(self, key): return getattr(self.inner, key)
    monkeypatch.setattr(zlib, "decompressobj", Checked)
    result = preview(mzml_bytes(width=4, compressed=True))
    assert seen == [17, 17] and result["metadata"]["decoded_bytes"] == 32


def test_worker_host_contract_exact_copy_and_real_payloads():
    root = Path(__file__).resolve().parents[2]
    host = root / "backend/app/application/services/mass_spectrum_visualization.py"
    if not host.exists(): pytest.skip("separate build context")
    assert host.read_bytes().rstrip() == (root / "sandbox/app/services/mass_spectrum_payload.py").read_bytes().rstrip()
    spec = importlib.util.spec_from_file_location("mass_host", host)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    for value in browser_payloads().values():
        assert module.validate_mass_spectrum_payload(value, kind=value["kind"], options=value["selected"], size=value["metadata"]["source_bytes"], fmt=value["metadata"]["format"]) is value
