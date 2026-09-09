"""No-model integration check of the actual constrained Docker reader."""
import json
import threading
from pathlib import Path

from app.core.config import get_settings
from app.application.services.scientific_visualization import ScientificVisualizationResult, _safe_result_payload
from app.infrastructure.external.sandbox.visualization_worker import run_visualization_worker, VisualizationWorkerError


def main():
    image = get_settings().sandbox_image
    if not image:
        raise SystemExit("Configure the existing sandbox image first")
    root = Path(__file__).resolve().parents[1] / "app/resources/datasets"
    samples = [
        ("netcdf", "map", (root / "open-noaa-air-climatology/air.sig995.mon.ltm.1991-2020.nc").read_bytes()),
        ("netcdf", "series", (root / "open-noaa-air-climatology/air.sig995.mon.ltm.1991-2020.nc").read_bytes()),
        ("fits", "image", (root / "nasa-hst-wfpc2/WFPC2ASSNu5780205bx.fits").read_bytes()),
        ("fits", "series", (root / "nasa-hst-fos/FOSy19g0309t_c2f.fits").read_bytes()),
        ("fastq", "quality", b"@read1\nACGT\n+\nIIII\n@read2\nGGTA\n+\n5555\n"),
    ]
    checked = []
    for reader, kind, data in samples:
        result = run_visualization_worker(image, data, reader=reader, kind=kind, options={}, truncated=False, cancelled=threading.Event())
        assert result["ok"], result.get("error", "Invalid worker response")
        _safe_result_payload(result["data"])
        output = ScientificVisualizationResult.model_validate(result["data"])
        checked.append({"reader": reader, "view": kind, "input_bytes": len(data), "values": len(output.values) or len(output.y), "sampled": output.sampled})
    cancelled = threading.Event()
    timer = threading.Timer(0.05, cancelled.set)
    timer.start()
    try:
        try:
            run_visualization_worker(image, samples[0][2], reader="netcdf", kind="map", options={}, truncated=False, cancelled=cancelled)
        except VisualizationWorkerError:
            pass
        else:
            raise AssertionError("Cancelled worker must not return a preview")
    finally:
        timer.cancel()
    print(json.dumps({"scientific_previews": checked, "cancellation_check": "passed", "model_calls": 0}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
