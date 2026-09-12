import json

import pytest

from app.services.bounded_mca_reader import MCAError, mca_preview


def fixture(counts="0\n10\n20\n30", calibration="", footer=""):
    return ("<<PMCA SPECTRUM>>\nDESCRIPTION - PRIVATE /Users/private\nLIVE_TIME - 3.25\nREAL_TIME - 5\n"
            + calibration + "<<DATA>>\n" + counts + "\n<<END>>\n" + footer).encode()


def test_counts_without_calibration_remain_channels():
    result = mca_preview(fixture())
    assert result["array"] == {"shape": [4, 2], "dimensions": ["channel", "counts"],
                               "values": [0, 0, 1, 10, 2, 20, 3, 30]}
    assert result["metadata"]["calibration_applied"] is False
    assert result["metadata"]["energy_unit"] is None
    assert result["metadata"]["live_time_seconds"] == 3.25
    assert result["metadata"]["raw_counts"] is True
    assert result["metadata"]["fitting"] is False
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("unit", ["eV", "keV", "MeV"])
def test_explicit_two_point_energy_calibration(unit):
    result = mca_preview(fixture(calibration=f"<<CALIBRATION>>\nLABEL - {unit}\n0 1.5\n3 7.5\n"))
    assert result["array"]["values"] == [1.5, 0, 3.5, 10, 5.5, 20, 7.5, 30]
    assert result["metadata"]["calibration_coefficients"] == [1.5, 2.0, 0.0]
    assert result["metadata"]["calibration_points"] == [[0, 1.5], [3, 7.5]]
    assert result["metadata"]["energy_unit"] == unit
    assert result["metadata"]["calibration_applied"] is True


@pytest.mark.parametrize("calibration", ["<<CALIBRATION>>\nLABEL - Channel\n0 0\n3 7\n",
    "<<CALIBRATION>>\nLABEL - keV\n3 7\n", "<<CALIBRATION>>\n0 0\n3 7\n",
    "<<CALIBRATION>>\nLABEL - keV\n0 0\n1 2\n3 7\n", "<<CALIBRATION>>\nLABEL - mV\n0 0\n3 7\n"])
def test_no_invented_energy_or_automatic_fit(calibration):
    result = mca_preview(fixture(calibration=calibration))
    assert result["metadata"]["axis"] == "channel"
    assert result["metadata"]["calibration_applied"] is False


def test_exact_safe_integer_count_and_upper_channel_budget():
    data = fixture(counts="\n".join([str(2**53 - 1)] * 8192))
    result = mca_preview(data)
    assert result["array"]["shape"] == [8192, 2]
    assert result["array"]["values"][1] == 2**53 - 1
    assert isinstance(result["array"]["values"][1], int)


@pytest.mark.parametrize("counts", ["", "-1", "1.5", "1e2", "NaN", "Infinity", "True", "1 2",
    "0,1", str(2**53), "9" * 512, "\n".join(["1"] * 8193), "__import__('os')"],
    ids=["empty", "negative", "fraction", "exponent", "nan", "infinity", "boolean", "columns", "csv",
         "precision", "long-integer", "too-many-channels", "expression"])
def test_unsafe_counts_rejected(counts):
    with pytest.raises(MCAError):
        mca_preview(fixture(counts=counts))


@pytest.mark.parametrize("body", [b"\x00\xff", b"1\n2\n3", b"$SPEC_ID:\nTest\n$DATA:\n0 1\n0\n1",
    b"<<UNKNOWN>>\n<<DATA>>\n1\n<<END>>", b"<<PMCA SPECTRUM>>\n<<DATA>>\n1",
    fixture().replace(b"<<END>>", b"<<ROI>>"), fixture() + b"<<DATA>>\n1\n<<END>>",
    fixture() + fixture(), fixture(footer="\0"), fixture(footer="x" * 513)],
    ids=["binary", "unmarked", "qxas", "unknown", "incomplete", "missing-end", "duplicate-data", "multi", "nul", "long-line"])
def test_unidentified_binary_multispectrum_and_incomplete_rejected(body):
    with pytest.raises(MCAError):
        mca_preview(body)


@pytest.mark.parametrize("calibration", ["<<CALIBRATION>>\nLABEL - keV\n0 0\n0 1\n",
    "<<CALIBRATION>>\nLABEL - keV\n0 1\n3 0\n", "<<CALIBRATION>>\nLABEL - keV\n0 0\n4 1\n",
    "<<CALIBRATION>>\nLABEL - keV\n0 NaN\n3 1\n", "<<CALIBRATION>>\nLABEL - keV\n0 1e99999\n3 1\n",
    "<<CALIBRATION>>\nLABEL - keV\nLABEL - eV\n", "<<CALIBRATION>>\n0 0 0\n",
    "<<CALIBRATION>>\nLABEL - keV\n<<CALIBRATION>>\n", "<<CALIBRATION>>\n" + "0 1\n" * 65])
def test_malformed_calibration_rejected(calibration):
    with pytest.raises(MCAError):
        mca_preview(fixture(calibration=calibration))


def test_roi_and_hardware_footer_are_inert_and_hidden():
    result = mca_preview(fixture(calibration="<<ROI>>\nPRIVATE ROI\n",
        footer="<<DP5 CONFIGURATION>>\nSERIAL - PRIVATE\nPATH - /Users/private\n"))
    assert "PRIVATE" not in json.dumps(result)
    assert result["metadata"]["channels"] == 4


@pytest.mark.parametrize("data", [b"x" * (4 * 1024**2 + 1), b"\n" * 40001 + fixture(),
    fixture(footer=("A" * 512 + "\n") * 129)], ids=["input-bytes", "line-count", "metadata-bytes"])
def test_input_metadata_and_line_budgets(data):
    with pytest.raises(MCAError):
        mca_preview(data)


@pytest.mark.parametrize("options", [{"fit": True}, {"path": "/private"}, [], False, ""])
def test_options_rejected(options):
    with pytest.raises(MCAError):
        mca_preview(fixture(), options=options)


def test_wrong_format_rejected():
    with pytest.raises(MCAError):
        mca_preview(fixture(), fmt="spe")


def test_crlf_ascii_supported():
    result = mca_preview(fixture().replace(b"\n", b"\r\n"))
    assert result["metadata"]["channels"] == 4


def test_output_budget_is_enforced(monkeypatch):
    from app.services import bounded_mca_reader as module
    monkeypatch.setattr(module, "MAX_OUTPUT_BYTES", 64)
    with pytest.raises(MCAError):
        mca_preview(fixture())
