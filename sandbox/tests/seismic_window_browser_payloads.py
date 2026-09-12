"""Synthetic private results; MiniSEED reference generated/read by ObsPy libmseed."""
import io
import json
import struct
from app.services.seismic_window_reader import seismic_window_preview
from seismic_window_fixtures import mseed_bytes, official_mseed, sac_bytes


def generate():
    import obspy
    result = {"provenance": {"generator": "sandbox/tests/seismic_window_browser_payloads.py", "engine": "ObsPy "+obspy.__version__+" / libmseed", "synthetic": True}, "cases": {}}
    for fmt, raw in [("mseed", official_mseed([1, -2, 3, 4, 2, 5, -3, 2], "STEIM2")), ("sac", sac_bytes(version=7))]:
        def read(offset, length): return raw[offset:offset+length]
        tree = seismic_window_preview(read, len(raw), fmt)
        case = {"tree": tree}
        count = tree["choices"]["records"][0]["samples"]
        for name, options in [("full", {"record": 0, "start_sample": 0, "sample_count": count}), ("window", {"record": 0, "start_sample": 1, "sample_count": 2})]:
            case[name] = seismic_window_preview(read, len(raw), fmt, "series", options)
        if fmt == "mseed":
            assert case["full"]["series"][0]["y"] == obspy.read(io.BytesIO(raw), format="MSEED")[0].data.tolist()
        else: assert case["full"]["series"][0]["y"] == [1.25, -2.5, 0.0, 4.75]
        result["cases"][fmt] = case
    raw = b"".join(mseed_bytes(second=i if i < 2 else i+1) for i in range(20))
    read = lambda o, n: raw[o:o+n]
    result["cases"]["paged"] = {
        "tree": seismic_window_preview(read, len(raw), "miniseed"),
        "page": seismic_window_preview(read, len(raw), "miniseed", "tree", {"record_offset": 16, "record_limit": 16}),
        "window": seismic_window_preview(read, len(raw), "miniseed", "series", {"record": 16, "start_sample": 1, "sample_count": 2}),
    }
    header, size = sac_bytes(sample_count=300000000)
    def sparse_read(offset, length):
        if offset == 0: return header[:length]
        return struct.pack("<"+str(length//4)+"f", *([1.25]*(length//4)))
    result["cases"]["large_sac"] = {"window": seismic_window_preview(sparse_read, size, "sac", "series", {"record": 0, "start_sample": 299999996, "sample_count": 4})}
    return result


if __name__ == "__main__": print(json.dumps(generate(), ensure_ascii=False, allow_nan=False))
