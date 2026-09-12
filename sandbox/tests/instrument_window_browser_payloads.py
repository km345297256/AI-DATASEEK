"""Print canonical, synthetic reader payloads for UI/worker cross-checks."""
import json

from app.services.instrument_window_reader import instrument_window_preview
from instrument_window_fixtures import MemorySource, edf_bytes, spe_bytes


def payloads():
    results = {"provenance": "Synthetic 4x3 two-frame fixtures; independent bounded EDF/SPE reader; no real experimental data."}
    for fmt, factory in (("edf", edf_bytes), ("spe", spe_bytes)):
        for kind in ("tree", "image"):
            source = MemorySource(factory())
            options = {} if kind == "tree" else {"frame": 1, "roi": [1, 1, 2, 2]}
            results[f"{fmt}_{kind}"] = instrument_window_preview(source.read, source.size, fmt, kind, options)
    return results


if __name__ == "__main__":
    print(json.dumps(payloads(), indent=2, ensure_ascii=False, allow_nan=False))
