"""Actual bounded reader payloads from synthetic/official FlowIO FCS binaries."""
import json
from app.services.fcs_window_reader import fcs_window_preview
from fcs_window_fixtures import fcs_bytes, official_fcs, SparseFcs


def generate():
    import flowio
    cases = {}
    sources = {"float": official_fcs(), "integer": fcs_bytes([[0, 4294967295], [4294967295, 0], [2147483648, 2147483647], [42, 3]], datatype="I", bits=32, endian=">"),
        "double": fcs_bytes([[1e-20, 2], [.5, float("nan")], [1e5, 4], [1.5, -3]], datatype="D")}
    for name, raw in sources.items():
        read = lambda o, n: raw[o:o+n]
        cases[name] = {"tree": fcs_window_preview(read, len(raw), "fcs")}
        for label, opts in [("scatter", {"view": "scatter", "channels": [0, 1], "event_offset": 0, "event_count": 4}),
            ("histogram", {"view": "histogram", "channels": [0], "event_offset": 0, "event_count": 4, "bins": 4}),
            ("window", {"view": "scatter", "channels": [1, 0], "event_offset": 1, "event_count": 2})]:
            cases[name][label] = fcs_window_preview(read, len(raw), "fcs", "series", opts)
    prefix, size = fcs_bytes(virtual_total=200000000); sparse = SparseFcs(prefix, size)
    def read(o, n): sparse.seek(o); return sparse.read(n)
    cases["large"] = {"window": fcs_window_preview(read, size, "fcs", "series", {"view": "scatter", "channels": [0, 1], "event_offset": 199999998, "event_count": 2})}
    return {"provenance": {"generator": "sandbox/tests/fcs_window_browser_payloads.py", "reference": "FlowIO "+flowio.__version__, "synthetic": True}, "cases": cases}


if __name__ == "__main__": print(json.dumps(generate(), ensure_ascii=False, allow_nan=False))
