"""Regression checks for real, namespaced PANalytical XRDML axis encodings."""

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("xrd_operations", ROOT / "tools/xrd/operations.py")
OPS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(OPS)


def source(tmp_path, body):
    path = tmp_path / "sample.xrdml"
    path.write_text(
        '<xrdMeasurements xmlns="http://www.xrdml.com/XRDMeasurement/1.5">'
        '<xrdMeasurement><scan><dataPoints>' + body
        + '</dataPoints></scan></xrdMeasurement></xrdMeasurements>',
        encoding="utf-8",
    )
    return path


def test_standard_start_end_axis_is_not_reconstructed_from_intensities(tmp_path):
    path = source(tmp_path,
        '<positions axis="2Theta" unit="deg"><startPosition>4</startPosition>'
        '<endPosition>4.03</endPosition></positions>'
        '<intensities unit="counts">2314.494 2167.012 2078.533 2115.203</intensities>')
    scan = OPS.parse(path)["scans"][0]
    assert scan["positions"] == pytest.approx([4, 4.01, 4.02, 4.03])
    assert scan["intensities"] == [2314.494, 2167.012, 2078.533, 2115.203]


def test_prefer_2theta_over_omega_or_fixed_motor_positions(tmp_path):
    path = source(tmp_path,
        '<positions axis="Omega"><startPosition>10</startPosition><endPosition>11</endPosition></positions>'
        '<positions axis="2Theta"><startPosition>20</startPosition><endPosition>22</endPosition></positions>'
        '<positions axis="Phi"><commonPosition>0</commonPosition></positions>'
        '<counts>7 8 9</counts>')
    scan = OPS.parse(path)["scans"][0]
    assert scan["positions"] == [20, 21, 22]
    assert scan["intensities"] == [7, 8, 9]


@pytest.mark.parametrize("positions", [
    '<positions axis="2Theta">1 2 3</positions>',
    '<positions axis="2Theta"><listPositions>1 2 3</listPositions></positions>',
    '<positions>1 2 3</positions>',
    '<twotheta>1 2 3</twotheta>',
])
def test_explicit_and_legacy_position_lists(tmp_path, positions):
    scan = OPS.parse(source(tmp_path, positions + '<intensities>4 5 6</intensities>'))["scans"][0]
    assert scan["positions"] == [1, 2, 3]
    assert scan["intensities"] == [4, 5, 6]


def test_missing_axis_does_not_split_intensities_into_fake_angles(tmp_path):
    assert OPS.parse(source(tmp_path, '<intensities>100 200 300 400</intensities>'))["scans"] == []


def test_mismatched_explicit_axis_is_not_silently_truncated(tmp_path):
    path = source(tmp_path, '<positions axis="2Theta">1 2</positions><intensities>4 5 6</intensities>')
    with pytest.raises(ValueError, match="axis/intensity length mismatch"):
        OPS.parse(path)


def test_common_position_keeps_one_coordinate_per_count(tmp_path):
    path = source(tmp_path, '<positions axis="2Theta"><commonPosition>12</commonPosition></positions>'
        '<intensities>4 5 6</intensities>')
    assert OPS.parse(path)["scans"][0]["positions"] == [12, 12, 12]


def run_operation(operation, path):
    arguments = base64.urlsafe_b64encode(json.dumps({"input_path": str(path)}).encode()).decode()
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/xrd/operations.py"), operation, arguments],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("positions,intensities,valid", [
    ("1 2 3", "4 5 6", True),
    ("1 1e309 3", "4 5 6", False),
    ("1 2 3", "4 1e309 6", False),
])
def test_validate_cli_handles_finite_arrays_and_nonfinite_values(tmp_path, positions, intensities, valid):
    path = source(tmp_path, f'<positions axis="2Theta">{positions}</positions>'
        f'<intensities>{intensities}</intensities>')
    result = run_operation("xrdml_validate", path)
    assert result["success"] is True
    assert result["valid"] is valid
    assert result["issues"] == ([] if valid else ["scan 0: non-finite values"])


def test_inspect_cli_reports_complete_standard_scan(tmp_path):
    path = source(tmp_path,
        '<positions axis="2Theta"><startPosition>4</startPosition><endPosition>4.03</endPosition></positions>'
        '<intensities>2314.494 2167.012 2078.533 2115.203</intensities>')
    result = run_operation("xrdml_inspect", path)
    assert result["success"] is True
    assert result["scan_count"] == 1
    assert result["points_per_scan"] == [4]
