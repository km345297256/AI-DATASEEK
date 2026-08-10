import base64
import importlib.util
import json
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "dataset_analysis_runner.py"
SPEC = importlib.util.spec_from_file_location("dataset_analysis_runner", MODULE_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _encoded(source: str) -> str:
    return base64.b64encode(source.encode()).decode()


def test_runner_executes_program_and_validates_generated_artifact(tmp_path, capsys, monkeypatch):
    output_root = tmp_path / "output"
    output_root.mkdir()
    monkeypatch.setattr(runner, "OUTPUT_ROOT", output_root.resolve())
    output_dir = output_root / "analysis-1"
    result_path = output_dir / "result.json"
    source = f"""
from pathlib import Path
import json
output = Path({str(output_dir)!r})
chart = output / 'chart.png'
chart.write_bytes(b'png')
Path({str(result_path)!r}).write_text(json.dumps({{'success': True, 'result': '完成分析', 'attachments': [str(chart)]}}), encoding='utf-8')
"""

    assert runner.main([
        "--program-base64", _encoded(source),
        "--output-dir", str(output_dir),
        "--result-path", str(result_path),
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["attachments"] == [str(output_dir / "chart.png")]


def test_runner_rejects_attachment_outside_output_directory(tmp_path, capsys, monkeypatch):
    output_root = tmp_path / "output"
    output_root.mkdir()
    monkeypatch.setattr(runner, "OUTPUT_ROOT", output_root.resolve())
    output_dir = output_root / "analysis-2"
    result_path = output_dir / "result.json"
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    source = f"""
from pathlib import Path
import json
Path({str(result_path)!r}).write_text(json.dumps({{'success': True, 'result': 'bad', 'attachments': [str(Path({str(outside)!r}))]}}), encoding='utf-8')
"""

    assert runner.main([
        "--program-base64", _encoded(source),
        "--output-dir", str(output_dir),
        "--result-path", str(result_path),
    ]) == 2
    assert "outside the output directory" in capsys.readouterr().out


def test_runner_returns_typed_program_failure(tmp_path, capsys, monkeypatch):
    output_root = tmp_path / "output"
    output_root.mkdir()
    monkeypatch.setattr(runner, "OUTPUT_ROOT", output_root.resolve())
    output_dir = output_root / "analysis-3"
    result_path = output_dir / "result.json"

    assert runner.main([
        "--program-base64", _encoded("raise RuntimeError('broken plot')"),
        "--output-dir", str(output_dir),
        "--result-path", str(result_path),
    ]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert "RuntimeError" in payload["error"]


def test_runner_serializes_numpy_scalars_in_model_result(tmp_path, capsys, monkeypatch):
    numpy = __import__("numpy")
    output_root = tmp_path / "output"
    output_root.mkdir()
    monkeypatch.setattr(runner, "OUTPUT_ROOT", output_root.resolve())
    output_dir = output_root / "analysis-numpy"
    result_path = output_dir / "result.json"
    source = f"""
from pathlib import Path
import json
import numpy as np
Path({str(result_path)!r}).write_text(json.dumps({{'success': True, 'result': '完成分析', 'evidence': {{'mean': np.float32(1.25)}}, 'attachments': []}}), encoding='utf-8')
"""

    assert runner.main([
        "--program-base64", _encoded(source),
        "--output-dir", str(output_dir),
        "--result-path", str(result_path),
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["result"] == "完成分析"
