"""API-side pure contract: the actual isolated reader fixture, no scientific import."""
import copy
import json
from pathlib import Path
import unittest
from app.application.services.radar_window_visualization import validate_radar_window_options, validate_radar_window_payload

ROOT = Path(__file__).resolve().parents[2]
DATA = json.loads((ROOT / "frontend/tests/browser/domain-expansion-radar-data.json").read_text())


class RadarVisualizationTests(unittest.TestCase):
    def test_validator_is_exact_sandbox_copy(self):
        self.assertEqual((ROOT / "sandbox/app/services/radar_window_payload.py").read_text().rstrip(),
            (ROOT / "backend/app/application/services/radar_window_visualization.py").read_text().rstrip())

    def test_true_fixture_and_host_count_bindings(self):
        for group in DATA.values():
            for kind, value in group.items():
                m = value["metadata"]
                self.assertIs(validate_radar_window_payload(value, kind=kind, options=value["selected"], fmt=m["format"],
                    source_bytes=m["source_bytes"], read_bytes=m["read_bytes"], read_requests=m["read_requests"]), value)
                for binding in ({"source_bytes": m["source_bytes"]+1}, {"read_bytes": m["read_bytes"]+1},
                                {"read_requests": m["read_requests"]+1}, {"fmt": "hdf5"}, {"read_bytes": True}):
                    with self.subTest(binding=binding), self.assertRaises(ValueError): validate_radar_window_payload(value, **binding)

    def test_strict_untrusted_scalar_and_selection_contract(self):
        original = DATA["positive"]["image"]
        edits = [lambda r: r.__setitem__("contract_version", 2.), lambda r: r.__setitem__("sampled", 1),
            lambda r: r["selected"].__setitem__("ray_start", True), lambda r: r["selected"].__setitem__("decode", "physical"),
            lambda r: r["array"]["values"].__setitem__(0, True), lambda r: r["array"]["values"].__setitem__(0, 256),
            lambda r: r["radar"]["range_m"].__setitem__(0, 1250), lambda r: r["radar"]["ray_indices"].__setitem__(0, 3),
            lambda r: r["radar"].__setitem__("undetect_count", 0), lambda r: r["metadata"].__setitem__("read_requests", 129),
            lambda r: r["metadata"].__setitem__("source_bytes", True), lambda r: r["metadata"].__setitem__("path", "/private/hidden"),
            lambda r: r["choices"]["sweeps"][0]["quantities"][0].__setitem__("gain", 0),
            lambda r: r["choices"]["sweeps"][0]["quantities"][0].__setitem__("unit", "<script>"),
            lambda r: r["choices"]["sweeps"][0].__setitem__("a1gate", True)]
        for i, edit in enumerate(edits):
            value = copy.deepcopy(original); edit(value)
            with self.subTest(edit=i), self.assertRaises(ValueError): validate_radar_window_payload(value)
        for selected in ({**original["selected"], "sweep":2}, {**original["selected"], "ray_start":0}):
            with self.assertRaises(ValueError): validate_radar_window_payload(original, options=selected)

    def test_options_are_closed_and_never_accept_calibration_or_paths(self):
        self.assertEqual(validate_radar_window_options("tree", {}), {})
        selected = DATA["positive"]["image"]["selected"]
        for kind, options in (("tree", {"sweep":1}), ("image", {}), ("image", {**selected, "gain":2}),
                              ("image", {**selected, "ray_count":129}), ("image", {**selected, "path":"/data"}),
                              ("geometry", {}), (False, {})):
            with self.subTest(kind=kind, options=options), self.assertRaises(ValueError): validate_radar_window_options(kind, options)


if __name__ == "__main__": unittest.main()
