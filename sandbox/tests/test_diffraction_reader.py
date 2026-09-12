import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from app.services import diffraction_reader as reader
from app.services.diffraction_payload import DiffractionError, validate_diffraction_payload, validate_diffraction_options
from diffraction_fixtures import xrdml, cansas

class DiffractionTests(unittest.TestCase):
    def preview(self, raw, fmt="xrdml", kind="series", options=None):
        return reader.diffraction_preview(raw, fmt, kind, {"scan": 1} if options is None and kind == "series" else options)

    def test_xrdml_versions_raw_units_and_no_normalization(self):
        for version in ("1.0", "1.6", "1.7", "2.0", "2.1", "2.4"):
            with self.subTest(version=version):
                value = self.preview(xrdml(version=version)); scan = value["choices"]["scans"][1]
                self.assertEqual(value["series"][0]["x"], [10, 11, 12, 13]); self.assertEqual(value["series"][0]["y"], [10, 40, 20, 5])
                self.assertEqual(scan["y_quantity"], "intensities" if version.startswith("1.") else "counts")
                self.assertEqual(scan["y_unit"], "counts"); self.assertIsNone(value["series"][0]["y_error"])

    def test_explicit_axis_and_coupled_axes(self):
        self.assertEqual(self.preview(xrdml(explicit=True))["series"][0]["x"], [10, 10.25, 11.5, 14])
        self.assertEqual(self.preview(xrdml(coupled=True))["series"][0]["x"], [10, 11, 12, 13])
        with self.assertRaises(DiffractionError): self.preview(xrdml(coupled=True, explicit=True))
        raw = xrdml().replace(b"10</startPosition>", b"13</startPosition>").replace(b"13</endPosition>", b"10</endPosition>")
        self.assertEqual(self.preview(raw)["series"][0]["x"], [13, 12, 11, 10])

    def test_cansas_versions_explicit_errors_negative_intensity(self):
        for version in ("1.0", "1.1"):
            value = self.preview(cansas(version=version), "xml"); trace = value["series"][0]
            self.assertEqual(trace["x"], [.01, .021, .045, .1]); self.assertEqual(trace["y"], [10, -2, 5, 1])
            self.assertEqual(trace["x_error"], [.001, .0012, .0015, .002]); self.assertEqual(trace["y_error"], [.5, .2, .3, .1])
            self.assertEqual(value["choices"]["scans"][1]["x_unit"], "1/A")

    def test_absent_errors_not_inferred_and_tree_no_returned_points(self):
        value = self.preview(cansas(errors=False), "xml"); self.assertIsNone(value["series"][0]["y_error"])
        tree = self.preview(cansas(), "xml", "tree", {})
        self.assertEqual(tree["metadata"]["total_points"], 8); self.assertEqual(tree["metadata"]["returned_points"], 0)
        self.assertEqual(tree["tree"][1]["path"], "/scan-1"); self.assertNotIn("series", tree)

    def test_wrong_options_rejected_before_xml(self):
        with patch.object(reader.ET, "fromstring", side_effect=AssertionError("XML must not run")):
            for kind, options in (("tree", {"scan": 0}), ("series", {}), ("series", {"scan": True}), ("series", {"scan": -1}), ("series", {"scan": 32}), ("series", {"scan": 0, "url": "secret"})):
                with self.assertRaises(DiffractionError): reader.diffraction_preview(b"<x/>", "xml", kind, options)

    def test_scan_selection_is_required_and_bound(self):
        with self.assertRaises(DiffractionError): self.preview(xrdml(), options={"scan": 2})
        for raw, fmt in ((xrdml(), "xml"), (cansas(), "xrdml"), (b"1 2\n3 4", "xy")):
            with self.assertRaises(DiffractionError): self.preview(raw, fmt)

    def test_xrdml_ambiguous_or_unsupported_data_rejected(self):
        changes = [(b'scanAxis="2Theta"', b'scanAxis="Reciprocal Space"'), (b'status="Completed"', b'status="Aborted"'),
                   (b'<commonPosition>5</commonPosition>', b'<startPosition>5</startPosition><endPosition>6</endPosition>'),
                   (b'unit="deg"', b'unit="rad"'), (b'unit="counts"', b'unit="cps"'), (b'<counts', b'<intensities'),
                   (b'</counts>', b'</counts><relativeCounts>1 2 3 4</relativeCounts>'),
                   (b'</counts>', b'</counts><background>1 2 3 4</background>'),
                   (b'<commonCountingTime unit="seconds">2', b'<commonCountingTime unit="seconds">0'),
                   (b'<counts unit="counts">10 40 20 5', b'<counts unit="counts">10')]
        for old, new in changes:
            with self.subTest(new=new), self.assertRaises(DiffractionError): self.preview(xrdml().replace(old, new))

    def test_cansas_undeclared_or_mismatched_errors_rejected(self):
        raw = cansas()
        changes = [(b'<Idev unit="1/cm">0.5</Idev>', b''), (b'<Qdev unit="1/A">', b'<Qdev unit="1/nm">'),
                   (b'<Idev unit="1/cm">0.5', b'<Idev unit="1/cm">-0.5'), (b'<I unit="1/cm">', b'<I>'),
                   (b'<Q unit="1/A">0.021', b'<Q unit="1/A">0.01'),
                   (b'<Q unit="1/A">0.01', b'<Q unit="1/A">-0.01'),
                   (b'</Idata>', b'<dQw unit="1/A">0.1</dQw></Idata>')]
        for old, new in changes:
            with self.subTest(new=new), self.assertRaises(DiffractionError): self.preview(raw.replace(old, new, 1), "xml")

    def test_xml_entities_stylesheets_external_and_nested_data_rejected(self):
        for raw in (b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///private/secret">]>'+cansas(),
                    b'<?xml-stylesheet href="https://example.invalid/a.xsl"?>'+cansas(),
                    cansas().replace(b'<SASentry>', b'<SASentry href="https://example.invalid/">'),
                    cansas().replace(b'<SASentry>', b'<SASentry><include xmlns="http://www.w3.org/2001/XInclude"/>'),
                    cansas().replace(b'<SASdata>', b'<SASdata><foreign xmlns="urn:bad"/>'),
                    b'<a>'*33+b'x'+b'</a>'*33, cansas()+b'<a/>'):
            with self.assertRaises(DiffractionError) as error: self.preview(raw, "xml")
            self.assertNotIn("private", str(error.exception)); self.assertNotIn("example", str(error.exception))

    def test_numeric_lexemes_and_precision(self):
        for value in ("NaN", "Infinity", "1e999", "1e-999", "9007199254740993", "1"*65, "0x10", "1,2"):
            with self.subTest(value=value), self.assertRaises(DiffractionError): reader.scalar(value)
        self.assertEqual(reader.scalar("9.007199254740991e15"), 9007199254740991)
        self.assertEqual(reader.scalar("-2.5e-10"), -2.5e-10)

    def test_input_scan_and_point_limits(self):
        for raw in (b"x"*(16*1024**2+1), xrdml(scans=33), xrdml().replace(b'10 40 20 5', b'1 '*16385)):
            with self.assertRaises(DiffractionError): self.preview(raw)

    def test_backend_pure_copy_and_binding(self):
        root = Path(__file__).resolve().parents[2]
        source = root / "backend/app/application/services/diffraction_visualization.py"
        self.assertEqual(source.read_bytes(), (root / "sandbox/app/services/diffraction_payload.py").read_bytes())
        spec = importlib.util.spec_from_file_location("diffraction_backend", source); module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        result = self.preview(cansas(), "xml")
        self.assertIs(module.validate_diffraction_payload(result, kind="series", options={"scan": 1}, fmt="xml", size=result["metadata"]["source_bytes"]), result)
        for kw in ({"kind":"tree"}, {"options":{"scan":0}}, {"size":1}, {"fmt":"xrdml"}, {"limit":200}):
            with self.assertRaises(ValueError): module.validate_diffraction_payload(result, **kw)

    def test_worker_payload_mutations(self):
        result = self.preview(cansas(), "xml")
        changes = [lambda v:v.update(contract_version=True), lambda v:v.update(sampled=True), lambda v:v["selected"].update(scan=True),
                   lambda v:v["choices"]["scans"][0].update(id=False), lambda v:v["choices"]["scans"][0].update(label="/private/secret"),
                   lambda v:v["metadata"].update(returned_points=True), lambda v:v["metadata"]["limits"].update(max_points=True),
                   lambda v:v["series"][0].update(scan=0), lambda v:v["series"][0]["y"].__setitem__(0, True),
                   lambda v:v["series"][0]["y_error"].__setitem__(0, -1), lambda v:v["series"][0].update(x_error=None)]
        for change in changes:
            bad=copy.deepcopy(result); change(bad)
            with self.assertRaises(DiffractionError): validate_diffraction_payload(bad)

if __name__ == "__main__": unittest.main()
