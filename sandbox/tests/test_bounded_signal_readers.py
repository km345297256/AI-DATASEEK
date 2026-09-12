from __future__ import annotations

import base64
import io
import json

import pytest

from app.services.bounded_signal_readers import (
    BoundedRangeSource, MAX_SOURCE_BYTES, SignalPreviewError, edf_window_preview,
)
from app.services.window_visualization_worker import PROTOCOL, run


def fixture(*, fmt="edf", plus=False, discontinuous=False, records=3, duration="1", samples=(4, 2),
            labels=None, physical=None, digits=None, values=None, identity="PRIVATE SUBJECT",
            header_only=False):
    labels = list(labels or ("EEG C3", "Status" if fmt == "bdf" else "ECG I"))
    if plus:
        samples = (*samples, 32)
        labels.append(("EDF" if fmt == "edf" else "BDF") + " Annotations")
    n = len(samples)
    width = 2 if fmt == "edf" else 3
    minimum, maximum = -(1 << (width * 8 - 1)), (1 << (width * 8 - 1)) - 1
    physical = physical or [(minimum, maximum)] * n
    digits = digits or [(minimum, maximum)] * n

    def field(value, length):
        encoded = str(value).encode("ascii")
        assert len(encoded) <= length
        return encoded.ljust(length, b" ")

    main = b"0       " if fmt == "edf" else b"\xffBIOSEMI"
    main += field(identity, 80) + field("PRIVATE RECORDING /Users/private", 80)
    main += b"01.01.24" + b"12.34.56" + field((n + 1) * 256, 8)
    variant = fmt.upper() + ("+D" if discontinuous else "+C") if plus or discontinuous else ""
    main += field(variant, 44) + field(records, 8) + field(duration, 8) + field(n, 4)
    columns = [(labels, 16), (["PRIVATE SENSOR"] * n, 80), (["uV"] * n, 8),
        ([p[0] for p in physical], 8), ([p[1] for p in physical], 8),
        ([p[0] for p in digits], 8), ([p[1] for p in digits], 8),
        (["PRIVATE FILTER"] * n, 80), (samples, 8), ([""] * n, 32)]
    header = main + b"".join(field(value, width) for column, width in columns for value in column)
    assert len(header) == (n + 1) * 256
    size = len(header) + sum(samples) * width * records
    if header_only:
        return header, size
    content = bytearray(header)
    for record in range(records):
        for channel, count in enumerate(samples):
            if "Annotations" in labels[channel]:
                payload = b"+0\x14\x14\x00PRIVATE ANNOTATION PATIENT"
                content.extend(payload.ljust(count * width, b"\x00"))
                continue
            for i in range(count):
                value = values[record][channel][i] if values is not None else record * count + i
                content.extend(int(value).to_bytes(width, "little", signed=True))
    return bytes(content)


def preview(data, fmt="edf", options=None):
    calls = []
    def read(offset, length):
        calls.append((offset, length))
        return data[offset:offset + length]
    return edf_window_preview(read, len(data), fmt, options), calls


def change(data, start, value, length=8):
    value = str(value).encode().ljust(length, b" ")
    assert len(value) == length
    return data[:start] + value + data[start + length:]


def test_edf_selected_time_window_preserves_distinct_sample_rates_and_calibration():
    data = fixture(physical=[(-100, 100), (-10, 10)])
    result, calls = preview(data, options={"channels": [0, 1], "start_seconds": 0.5, "duration_seconds": 1.5})
    assert [s["sample_rate"] for s in result["series"]] == [4, 2]
    assert result["series"][0]["x"] == [0.5, 0.75, 1, 1.25, 1.5, 1.75]
    assert result["series"][1]["x"] == [0.5, 1, 1.5]
    assert result["series"][0]["y"][0] == pytest.approx(-100 + (2 + 32768) * 200 / 65535)
    assert result["choices"]["channels"][0]["physical_min"] == -100
    assert result["metadata"]["read_bytes"] == sum(n for _, n in calls) == 768 + 18
    assert result["metadata"]["read_requests"] == len(calls) == 6
    assert result["selected"] == {"channels": [0, 1], "start_seconds": .5, "duration_seconds": 1.5}
    assert result["metadata"]["no_resampling"] is True
    text = json.dumps(result)
    assert not any(v in text for v in ("PRIVATE", "12.34.56", "01.01.24", "/Users/private"))


@pytest.mark.parametrize("fmt", ["edf", "bdf"])
def test_signed_samples_and_inverted_gain(fmt):
    limit = 32768 if fmt == "edf" else 8388608
    data = fixture(fmt=fmt, records=1, physical=[(limit - 1, -limit), (-limit, limit - 1)],
        values=[[[-limit, -1, 0, limit - 1], [0, 1]]])
    result, _ = preview(data, fmt, {"duration_seconds": 1})
    assert result["series"][0]["y"] == [limit - 1, 0, -1, -limit]


@pytest.mark.parametrize("fmt", ["edf", "bdf"])
def test_annotations_hidden_and_never_read_as_sample_data(fmt):
    data = fixture(fmt=fmt, plus=True)
    result, calls = preview(data, fmt, {"channels": [0], "duration_seconds": 2})
    assert result["metadata"]["variant"] == fmt.upper() + "+C"
    assert result["choices"]["channels"][-1]["channel_type"] == "annotation"
    assert result["choices"]["channels"][-1]["selectable"] is False
    width = 2 if fmt == "edf" else 3
    for offset, size in calls[2:]:
        assert (offset - 1024) % (38 * width) < 4 * width
        assert size <= 4 * width
    assert "PRIVATE" not in json.dumps(result)
    with pytest.raises(SignalPreviewError):
        preview(data, fmt, {"channels": [2], "duration_seconds": 1})


def test_bdf_status_is_explicitly_blocked():
    data = fixture(fmt="bdf")
    result, _ = preview(data, "bdf")
    assert result["choices"]["channels"][1]["channel_type"] == "status"
    with pytest.raises(SignalPreviewError):
        preview(data, "bdf", {"channels": [1]})


def test_source_larger_than_full_preview_cap_reads_only_small_selected_ranges():
    header, size = fixture(records=1000000, samples=(1000, 1), header_only=True)
    calls = []
    def read(offset, length):
        calls.append((offset, length))
        if offset < len(header):
            return header[offset:offset + length]
        return b"\0" * length
    result = edf_window_preview(read, size, "edf", {"channels": [0], "start_seconds": 900000, "duration_seconds": .25})
    assert size > 1024**3
    assert result["series"][0]["x"][0] == 900000
    assert len(result["series"][0]["y"]) == 250
    assert sum(n for _, n in calls) == len(header) + 500
    assert len(calls) == 3
    assert calls[-1][0] > 1024**3


def test_default_window_shrinks_not_sample_rate():
    data = fixture(samples=(10000, 2), records=2)
    result, _ = preview(data)
    assert 0 < result["selected"]["duration_seconds"] < 2
    assert result["series"][0]["sample_rate"] == 10000
    assert len(result["series"][0]["y"]) <= 16384
    assert result["sampled"] is True
    with pytest.raises(SignalPreviewError):
        preview(data, options={"duration_seconds": 2})


def test_sample_budget_checked_before_any_samples_are_read():
    data = fixture(samples=(10000, 10000))
    calls = []
    def read(o, n):
        calls.append((o, n))
        return data[o:o+n]
    with pytest.raises(SignalPreviewError):
        edf_window_preview(read, len(data), "edf", {"channels": [0, 1], "duration_seconds": 1})
    assert len(calls) == 2


def test_low_sampling_rate_no_implicit_resampling():
    data = fixture(samples=(2, 1), duration="30")
    result, _ = preview(data, options={"channels": [0, 1], "duration_seconds": 60})
    assert result["series"][0]["x"] == [0, 15, 30, 45]
    assert result["series"][1]["x"] == [0, 30]


def test_fractional_window_boundaries_are_exact():
    data = fixture(samples=(5, 2))
    result, _ = preview(data, options={"start_seconds": 1.2, "duration_seconds": .6})
    assert result["series"][0]["x"] == [1.2, 1.4, 1.6]


@pytest.mark.parametrize("options", [[], {"channels": []}, {"channels": [True]}, {"channels": [256]},
    {"channels": [-1]}, {"channels": [0, 0]}, {"channels": list(range(9))}, {"channels": "0"},
    {"start_seconds": True}, {"start_seconds": -1}, {"start_seconds": float("inf")},
    {"duration_seconds": 0}, {"duration_seconds": 61}, {"duration_seconds": "1"},
    {"duration_seconds": float("nan")}, {"path": "/forbidden"}, {"url": "https://invalid"},
    {"start_seconds": 3}, {"start_seconds": 2.5, "duration_seconds": 1}, {"channels": [5]},
    {"channels": None}, {"start_seconds": 10**1000}])
def test_rejects_invalid_options(options):
    with pytest.raises(SignalPreviewError):
        preview(fixture(), options=options)


@pytest.mark.parametrize("start,value,length", [(0, "{ EDF", 8), (184, 512, 8), (184, 99999999, 8),
    (236, -1, 8), (236, 0, 8), (236, 9999, 8), (244, 0, 8), (244, "NaN", 8),
    (244, "1e99", 8), (244, "1e-99", 8), (252, 0, 4), (252, 257, 4),
    (192, "BDF+C", 44), (192, "EDF+D", 44), (192, "EDF+X", 44)])
def test_invalid_fixed_headers(start, value, length):
    with pytest.raises(SignalPreviewError):
        preview(change(fixture(), start, value, length))


@pytest.mark.parametrize("fmt", ["edf", "bdf"])
def test_discontinuous_rejected_without_data_reads(fmt):
    with pytest.raises(SignalPreviewError, match="不连续"):
        preview(fixture(fmt=fmt, plus=True, discontinuous=True), fmt)


def test_extension_and_signature_must_agree():
    with pytest.raises(SignalPreviewError):
        preview(fixture(), "bdf")
    with pytest.raises(SignalPreviewError):
        preview(fixture(), "tif")


@pytest.mark.parametrize("tail", [b"x", b"\0\0", b"-truncate-"])
def test_complete_record_size_validation(tail):
    data = fixture()
    data = data[:-1] if tail == b"-truncate-" else data + tail
    with pytest.raises(SignalPreviewError):
        preview(data)


@pytest.mark.parametrize("size", [0, 255, MAX_SOURCE_BYTES + 1, True, 256.0])
def test_source_budget_rejected_before_callback(size):
    def fail(*_):
        pytest.fail("invalid source must not read")
    with pytest.raises(SignalPreviewError):
        edf_window_preview(fail, size, "edf")


@pytest.mark.parametrize("limits", [{}, {"max_read_bytes": True, "max_total_bytes": 10, "max_reads": 1},
    {"max_read_bytes": 1048577, "max_total_bytes": 10, "max_reads": 1},
    {"max_read_bytes": 1, "max_total_bytes": 8388609, "max_reads": 1},
    {"max_read_bytes": 1, "max_total_bytes": 10, "max_reads": 129}])
def test_limits_cannot_be_escalated(limits):
    with pytest.raises(SignalPreviewError):
        edf_window_preview(lambda *_: b"", 1000, "edf", limits=limits)


def test_read_count_cap_preflight():
    data = fixture(duration=".01", records=300)
    calls = []
    def read(o, n):
        calls.append((o, n))
        return data[o:o+n]
    with pytest.raises(SignalPreviewError):
        edf_window_preview(read, len(data), "edf", {"duration_seconds": 2})
    assert len(calls) == 2


@pytest.mark.parametrize("response", [b"", "text", bytearray(256), b"x" * 255, b"x" * 257],
                         ids=["empty", "str", "bytearray", "short", "long"])
def test_callback_requires_exact_bytes(response):
    with pytest.raises(SignalPreviewError):
        edf_window_preview(lambda *_: response, 1000, "edf")


def test_annotation_only_refuses_numeric_output():
    with pytest.raises(SignalPreviewError):
        preview(fixture(labels=["EDF Annotations", "EDF Annotations"]))


def test_range_source_rejects_out_of_range_and_boolean_offsets():
    source = BoundedRangeSource(1000, lambda _, n: b"\0" * n)
    for args in ((-1, 5), (0, 0), (999, 2), (True, 1), (0, True)):
        with pytest.raises(SignalPreviewError):
            source.read(*args)
    assert source.read_requests == 0


@pytest.mark.parametrize("limits", [
    {"max_read_bytes": 256, "max_total_bytes": 8388608, "max_reads": 128},
    {"max_read_bytes": 1048576, "max_total_bytes": 770, "max_reads": 128},
    {"max_read_bytes": 1048576, "max_total_bytes": 8388608, "max_reads": 2},
])
def test_smaller_trusted_limits_are_enforced_before_samples(limits):
    data, calls = fixture(), []
    def read(o, n):
        calls.append((o, n))
        return data[o:o+n]
    with pytest.raises(SignalPreviewError):
        edf_window_preview(read, len(data), "edf", {"duration_seconds": 1}, limits)
    assert all(offset < 768 for offset, _ in calls)


def test_signal_result_output_limit_enforced(monkeypatch):
    from app.services import bounded_signal_readers as module
    monkeypatch.setattr(module, "MAX_OUTPUT_BYTES", 512)
    with pytest.raises(SignalPreviewError):
        preview(fixture())


def init_for(data, **updates):
    result = {"protocol": PROTOCOL, "type": "init", "size": len(data), "reader": "edf", "kind": "series",
        "format": "edf", "options": {"duration_seconds": 1},
        "limits": {"max_read_bytes": 1048576, "max_total_bytes": 8388608, "max_reads": 128}}
    result.update(updates)
    return result


class HostInput:
    """Synchronous fake transport, still exercising exact private JSONL."""
    def __init__(self, data, output, init=None, mutate=None):
        self.data, self.output = data, output
        self.init, self.mutate = init or init_for(data), mutate
        self.initial = True
    def readline(self, maximum):
        if self.initial:
            self.initial = False
            return (json.dumps(self.init).encode() + b"\n")[:maximum]
        request = json.loads(self.output.getvalue().splitlines()[-1])
        assert request["type"] == "read"
        response = {"type": "bytes", "id": request["id"], "data_base64": base64.b64encode(
            self.data[request["offset"]:request["offset"] + request["length"]]).decode()}
        if self.mutate:
            response = self.mutate(response)
        return (json.dumps(response).encode() + b"\n")[:maximum]


def test_private_jsonl_roundtrip():
    data, output = fixture(), io.BytesIO()
    run(HostInput(data, output), output)
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [m["id"] for m in messages[:-1]] == [1, 2, 3]
    assert messages[-1]["type"] == "result" and messages[-1]["ok"] is True
    assert messages[-1]["data"]["metadata"]["read_requests"] == 3


@pytest.mark.parametrize("mutate", [lambda x: {**x, "id": 2}, lambda x: {**x, "id": True},
    lambda x: {**x, "path": "/forbidden"}, lambda x: {**x, "data_base64": "!"},
    lambda x: {**x, "type": "init"}, lambda x: {**x, "data_base64": x["data_base64"] + "AAAA"}])
def test_private_jsonl_rejects_bad_response_without_diagnostics(mutate):
    data, output = fixture(), io.BytesIO()
    run(HostInput(data, output, mutate=mutate), output)
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(messages) == 2
    assert messages[-1] == {"type": "result", "ok": False, "error": "rejected"}
    assert b"PRIVATE" not in output.getvalue()


@pytest.mark.parametrize("raw", [b"{}\n", b"[]\n", b'{"type":"init","type":"init"}\n',
    b'{"size":NaN}\n', b'{"size":Infinity}\n', b"x" * 8193, b"{}", b"\xff\n"],
    ids=["empty-object", "array", "duplicate", "nan", "infinity", "oversize", "unterminated", "encoding"])
def test_private_protocol_rejects_invalid_init(raw):
    output = io.BytesIO()
    run(io.BytesIO(raw), output)
    assert json.loads(output.getvalue()) == {"type": "result", "ok": False, "error": "rejected"}


@pytest.mark.parametrize("updates", [{"reader": "tabular"}, {"format": "tif"}, {"kind": "tree"},
    {"path": "/forbidden"}, {"protocol": "SSE"}, {"size": True}, {"options": {"url": "file:/secret"}}])
def test_private_protocol_rejects_escalation_before_read(updates):
    data, output = fixture(), io.BytesIO()
    run(HostInput(data, output, init=init_for(data, **updates)), output)
    assert json.loads(output.getvalue()) == {"type": "result", "ok": False, "error": "rejected"}
