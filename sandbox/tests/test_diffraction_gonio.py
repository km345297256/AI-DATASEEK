"""Gonio previews use declared coordinates, never inferred instrument axes."""
import json
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from app.services.diffraction_payload import DiffractionError
from app.services.diffraction_reader import diffraction_preview
from diffraction_fixtures import xrdml


FIXED_OMEGA = b'<positions axis="Omega" unit="deg"><commonPosition>5</commonPosition></positions>'
LINEAR_THETA = b'<startPosition>10</startPosition><endPosition>13</endPosition>'


def gonio(*, version="2.1", positions=None, omega=None, scans=1):
    """Derive synthetic Gonio input from the established public test fixture."""
    raw = xrdml(version=version, scans=scans).replace(b'scanAxis="2Theta"', b'scanAxis="Gonio"')
    raw = raw.replace(FIXED_OMEGA, b"" if omega is None else omega)
    return raw if positions is None else raw.replace(LINEAR_THETA, positions)


def series(raw, scan=0):
    return diffraction_preview(raw, "xrdml", "series", {"scan": scan})


class GonioSingleCoordinateTests(unittest.TestCase):
    def test_versions_keep_declared_theta_and_raw_counts(self):
        for version in ("1.0", "1.6", "1.7", "2.0", "2.1", "2.4"):
            with self.subTest(version=version):
                value = series(gonio(version=version))
                trace = value["series"][0]
                choice = value["choices"]["scans"][0]
                self.assertEqual(trace["x"], [10, 11, 12, 13])
                self.assertEqual(trace["y"], [10, 40, 20, 5])
                self.assertEqual(choice["x_quantity"], "2Theta")
                self.assertEqual(choice["x_unit"], "deg")
                self.assertEqual(choice["y_unit"], "counts")
                self.assertEqual(choice["y_quantity"], "intensities" if version.startswith("1.") else "counts")
                self.assertEqual(choice["axis_mode"], "linear-declared")
                self.assertIsNone(trace["x_error"])
                self.assertIsNone(trace["y_error"])
                self.assertNotIn("Omega", json.dumps(value))
                self.assertFalse(value["sampled"])

    def test_explicit_nonuniform_and_descending_coordinates_remain_in_order(self):
        cases = (
            (b"<listPositions>10 10.25 11.5 14</listPositions>", [10, 10.25, 11.5, 14], "explicit"),
            (b"<listPositions>14 11.5 10.25 10</listPositions>", [14, 11.5, 10.25, 10], "explicit"),
            (b"<startPosition>13</startPosition><endPosition>10</endPosition>", [13, 12, 11, 10], "linear-declared"),
        )
        for version in ("1.0", "2.4"):
            for positions, expected, mode in cases:
                with self.subTest(version=version, positions=positions):
                    value = series(gonio(version=version, positions=positions))
                    self.assertEqual(value["series"][0]["x"], expected)
                    self.assertEqual(value["series"][0]["y"], [10, 40, 20, 5])
                    self.assertEqual(value["choices"]["scans"][0]["axis_mode"], mode)

    def test_counting_times_and_attenuation_are_not_applied(self):
        raw = gonio().replace(
            b'<commonCountingTime unit="seconds">2</commonCountingTime>',
            b'<countingTimes unit="seconds">2 4 5 10</countingTimes>',
        ).replace(
            b"<commonBeamAttenuationFactor>3</commonBeamAttenuationFactor>",
            b"<beamAttenuationFactors>3 5 7 9</beamAttenuationFactors>"
            b"<divergenceCorrections>2 3 4 5</divergenceCorrections>",
        )
        self.assertEqual(series(raw)["series"][0]["y"], [10, 40, 20, 5])

    def test_multiple_scans_are_separate_and_selection_is_bound(self):
        raw = gonio(scans=2).replace(b"10 40 20 5", b"1 4 2 0.5", 1)
        tree = diffraction_preview(raw, "xrdml", "tree", {})
        self.assertEqual(tree["metadata"]["scan_count"], 2)
        self.assertEqual(tree["metadata"]["total_points"], 8)
        self.assertEqual(tree["metadata"]["returned_points"], 0)
        self.assertNotIn("series", tree)
        self.assertEqual(series(raw, 0)["series"][0]["y"], [1, 4, 2, 0.5])
        self.assertEqual(series(raw, 1)["series"][0]["y"], [10, 40, 20, 5])
        self.assertEqual(series(raw, 1)["series"][0]["scan"], 1)
        with self.assertRaises(DiffractionError):
            series(raw, 2)

    def test_optional_explicit_omega_still_requires_coupling(self):
        cases = (
            (None, b"<startPosition>5</startPosition><endPosition>6.5</endPosition>"),
            (b"<listPositions>10 10.25 11.5 14</listPositions>", b"<listPositions>5 5.125 5.75 7</listPositions>"),
            (b"<listPositions>14 11.5 10.25 10</listPositions>", b"<listPositions>7 5.75 5.125 5</listPositions>"),
        )
        for positions, omega_values in cases:
            with self.subTest(positions=positions):
                omega = b'<positions axis="Omega" unit="deg">' + omega_values + b"</positions>"
                without_omega = series(gonio(positions=positions))
                with_omega = series(gonio(positions=positions, omega=omega))
                self.assertEqual(with_omega["series"], without_omega["series"])
                self.assertEqual(with_omega["choices"], without_omega["choices"])

    def test_scan_axis_presence_is_resolved_independently_for_each_scan(self):
        omega = b'<positions axis="Omega" unit="deg"><startPosition>5</startPosition><endPosition>6.5</endPosition></positions>'
        raw = gonio(scans=2, omega=omega)
        for mixed in (raw.replace(omega, b"", 1), b"".join(raw.rsplit(omega, 1))):
            with self.subTest(raw=mixed):
                tree = diffraction_preview(mixed, "xrdml", "tree", {})
                self.assertEqual(tree["metadata"]["scan_count"], 2)
                for scan in (0, 1):
                    value = series(mixed, scan)
                    self.assertEqual(value["series"][0]["x"], [10, 11, 12, 13])
                    self.assertEqual(value["series"][0]["y"], [10, 40, 20, 5])

    def test_non_gonio_axes_preserve_their_existing_coordinate_policies(self):
        raw = xrdml(coupled=True, scans=1).replace(
            b"<startPosition>5</startPosition><endPosition>6.5</endPosition>",
            b"<startPosition>6</startPosition><endPosition>7.5</endPosition>",
        )
        self.assertEqual(series(raw)["series"][0]["x"], [10, 11, 12, 13])
        omega_first = raw.replace(b'scanAxis="2Theta-Omega"', b'scanAxis="Omega-2Theta"')
        value = series(omega_first)
        self.assertEqual(value["series"][0]["x"], [6, 6.5, 7, 7.5])
        self.assertEqual(value["choices"]["scans"][0]["x_quantity"], "Omega")
        omega_only = gonio().replace(b'scanAxis="Gonio"', b'scanAxis="Omega"').replace(b'axis="2Theta"', b'axis="Omega"')
        value = series(omega_only)
        self.assertEqual(value["series"][0]["x"], [10, 11, 12, 13])
        self.assertEqual(value["choices"]["scans"][0]["x_quantity"], "Omega")


class GonioSafetyRegressionTests(unittest.TestCase):
    def assert_rejected(self, raw):
        for kind, options in (("tree", {}), ("series", {"scan": 0})):
            with self.subTest(kind=kind), self.assertRaises(DiffractionError):
                diffraction_preview(raw, "xrdml", kind, options)

    def test_missing_theta_and_unknown_axes_are_not_inferred(self):
        raw = gonio()
        theta = b'<positions axis="2Theta" unit="deg">' + LINEAR_THETA + b"</positions>"
        cases = (
            raw.replace(theta, b""),
            raw.replace(b'axis="2Theta"', b'axis="Omega"'),
            raw.replace(b'axis="2Theta"', b'axis="Unknown"'),
            raw.replace(b'scanAxis="Gonio"', b'scanAxis="Unknown"'),
            raw.replace(b"</dataPoints>", b'<positions axis="Phi" unit="deg"><startPosition>1</startPosition><endPosition>2</endPosition></positions></dataPoints>'),
            raw.replace(b"</dataPoints>", b'<positions axis="Unknown" unit="deg"><commonPosition>1</commonPosition></positions></dataPoints>'),
        )
        for invalid in cases:
            with self.subTest(raw=invalid):
                self.assert_rejected(invalid)

    def test_units_lengths_and_ambiguous_positions_remain_rejected(self):
        cases = (
            (b'unit="deg"', b'unit="rad"'),
            (b'unit="deg"', b'unit="degrees"'),
            (b'unit="deg"', b'unit=""'),
            (b'unit="deg"', b""),
            (b'unit="counts"', b'unit="cps"'),
            (b'unit="seconds"', b'unit="ms"'),
            (LINEAR_THETA, b"<listPositions>10 11 12</listPositions>"),
            (LINEAR_THETA, b"<listPositions>10 11 12 13 14</listPositions>"),
            (LINEAR_THETA, b"<commonPosition>10</commonPosition>"),
            (LINEAR_THETA, b"<listPositions>10 12 11 13</listPositions>"),
            (LINEAR_THETA, b"<listPositions>10 11 11 13</listPositions>"),
            (LINEAR_THETA, LINEAR_THETA + b"<listPositions>10 11 12 13</listPositions>"),
            (b"10 40 20 5", b"10"),
        )
        for old, new in cases:
            with self.subTest(new=new):
                self.assert_rejected(gonio().replace(old, new))

    def test_present_omega_must_be_complete_matching_and_zero_offset(self):
        for content, unit in (
            (b"<commonPosition>5</commonPosition>", b"deg"),
            (b"<startPosition>5</startPosition><endPosition>6</endPosition>", b"deg"),
            (b"<startPosition>6</startPosition><endPosition>7.5</endPosition>", b"deg"),
            (b"<listPositions>5 5.5 6 6.6</listPositions>", b"deg"),
            (b"<listPositions>5 5.5 6</listPositions>", b"deg"),
            (b"<startPosition>5</startPosition><endPosition>6.5</endPosition>", b"rad"),
        ):
            with self.subTest(content=content, unit=unit):
                omega = b'<positions axis="Omega" unit="' + unit + b'">' + content + b"</positions>"
                self.assert_rejected(gonio(omega=omega))

    def test_other_declared_coupled_scan_axes_still_require_both_axes(self):
        for axis in (b"2Theta-Omega", b"Omega-2Theta", b"Omega"):
            with self.subTest(axis=axis):
                self.assert_rejected(gonio().replace(b'scanAxis="Gonio"', b'scanAxis="' + axis + b'"'))

    def test_bad_unselected_scan_cannot_be_hidden_by_selecting_a_valid_scan(self):
        omega = b'<positions axis="Omega" unit="deg"><startPosition>6</startPosition><endPosition>7.5</endPosition></positions>'
        raw = gonio(scans=2).replace(b"</dataPoints>", omega + b"</dataPoints>", 1)
        self.assert_rejected(raw)
        with self.assertRaises(DiffractionError):
            series(raw, 1)

    def test_dtd_entities_and_external_processing_remain_rejected(self):
        for prefix in (
            b'<!DOCTYPE xrdMeasurements [<!ENTITY probe SYSTEM "file:///not-readable">]>',
            b'<!DOCTYPE xrdMeasurements SYSTEM "https://example.invalid/schema.dtd">',
            b'<?xml-stylesheet href="https://example.invalid/view.xsl"?>',
        ):
            with self.subTest(prefix=prefix):
                self.assert_rejected(prefix + gonio())


class PublicGonioDatasetRegressionTests(unittest.TestCase):
    def test_public_carbonate_scans_match_independently_read_source_values(self):
        """Use repository public samples read-only, with no production filename rule."""
        root = Path(__file__).resolve().parents[2]
        sources = sorted((root / "backend/app/resources/datasets/mendeley-calcium-carbonate").glob("*.xrdml"))
        self.assertEqual(len(sources), 2, "The two public regression samples must be present")
        for source in sources:
            with self.subTest(source=source.name):
                raw = source.read_bytes()
                xml = ET.fromstring(raw)
                ns = {"x": xml.tag[1:].split("}", 1)[0]}
                scans = xml.findall("x:xrdMeasurement/x:scan", ns)
                tree = diffraction_preview(raw, "xrdml", "tree", {})
                self.assertEqual(tree["metadata"]["scan_count"], len(scans))
                for index, scan in enumerate(scans):
                    points = scan.find("x:dataPoints", ns)
                    theta = points.find('x:positions[@axis="2Theta"]', ns)
                    self.assertEqual(scan.get("scanAxis"), "Gonio")
                    self.assertIsNone(points.find('x:positions[@axis="Omega"]', ns))
                    intensity = points.find("x:intensities", ns)
                    if intensity is None:
                        intensity = points.find("x:counts", ns)
                    expected_y = [float(value) for value in intensity.text.split()]
                    begin = float(theta.find("x:startPosition", ns).text)
                    end = float(theta.find("x:endPosition", ns).text)
                    value = series(raw, index)
                    trace = value["series"][0]
                    self.assertEqual(trace["y"], expected_y)
                    self.assertEqual(len(trace["x"]), len(expected_y))
                    self.assertEqual(trace["x"][0], begin)
                    self.assertEqual(trace["x"][-1], end)
                    for i, coordinate in enumerate(trace["x"]):
                        expected = begin * (1 - i / (len(expected_y) - 1)) + end * i / (len(expected_y) - 1)
                        self.assertAlmostEqual(coordinate, expected, places=11)
                    self.assertEqual(value["choices"]["scans"][index]["x_quantity"], "2Theta")
                    self.assertEqual(value["choices"]["scans"][index]["x_unit"], theta.get("unit"))
                    self.assertEqual(value["choices"]["scans"][index]["y_unit"], intensity.get("unit"))
                    self.assertIsNone(trace["x_error"])
                    self.assertIsNone(trace["y_error"])
                    self.assertNotIn("Omega", json.dumps(value))


if __name__ == "__main__":
    unittest.main()
