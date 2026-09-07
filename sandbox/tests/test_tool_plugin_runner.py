import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from scripts.scientific_data_ops import build_parser as build_scientific_parser
from scripts.scientific_recipe_ops import build_parser as build_recipe_parser
from scripts.tool_plugin_runner import (
    _MAX_PLUGIN_OUTPUT_BYTES,
    _run_command_with_bounded_output,
    load_handler,
    load_execution_registry_snapshot,
    load_registry,
    load_registry_snapshot,
    main,
    run_tool,
)


ROOT = Path(__file__).resolve().parents[2]


def _process_is_running(pid: int) -> bool:
    """Treat an unreaped Linux zombie as terminated, not as a live process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False

    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            state = proc_stat.read_text().rsplit(")", 1)[1].strip().split()[0]
        except (IndexError, OSError):
            return True
        return state not in {"Z", "X"}
    return True


def test_runner_discovers_builtin_scientific_tools():
    registry = load_registry(ROOT / "tools")

    assert len(registry) >= 117
    assert "scientific_inspect" in registry
    assert "scientific_region_timeseries" in registry
    assert "geoscience_collection_inspect" in registry
    assert "hierarchical_store_inspect" in registry
    assert "presentation_inspect" in registry
    assert "geodata_product_package" in registry
    assert "netcdf_multi_file_concat" in registry
    assert "raster_calculator" in registry
    assert "netcdf_subset" in registry
    assert "netcdf_time_aggregate" in registry
    assert "netcdf_regrid" in registry
    assert "netcdf_collection_diagnose" in registry
    assert "raster_band_semantics" in registry
    assert "raster_index" in registry
    assert "raster_rgb_composite" in registry
    assert "shapefile_package_validate" in registry
    assert "vector_attribute_filter" in registry
    assert "vector_geometry_repair" in registry
    assert "space_fits_inspect" in registry
    assert "space_quality_report" in registry
    assert "sequence_inspect" in registry


def test_runner_rejects_duplicate_names(tmp_path):
    for plugin in ("one", "two"):
        directory = tmp_path / plugin
        directory.mkdir()
        (directory / "handler.py").write_text("def build_command(name, arguments): return ['true']\n")
        (directory / "manifest.json").write_text(json.dumps({
            "plugin": plugin,
            "version": "1.0.0",
            "tools": [{"name": "same"}],
        }))

    with pytest.raises(RuntimeError, match="Duplicate plugin tool name"):
        load_registry(tmp_path)


def test_runner_executes_only_registered_handler_command(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "echo"
    directory.mkdir()
    (directory / "handler.py").write_text(
        "def build_command(name, arguments):\n"
        "    return ['printf', '%s', arguments['value']]\n"
    )
    (directory / "manifest.json").write_text(json.dumps({
        "plugin": "echo",
        "version": "1.0.0",
        "handler": "handler.py",
        "tools": [{"name": "echo_value"}],
    }))
    monkeypatch.setenv("AI_DATASEEK_TOOLS_DIR", str(tmp_path))
    payload = base64.urlsafe_b64encode(json.dumps({"value": "hello"}).encode()).decode()

    assert run_tool("echo_value", payload) == 0
    assert capsys.readouterr().out == "hello"


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_runner_rejects_oversized_child_output_before_forwarding(
    tmp_path,
    monkeypatch,
    capsys,
    stream,
):
    directory = tmp_path / "oversized"
    directory.mkdir()
    (directory / "handler.py").write_text(
        "import sys\n"
        "def build_command(name, arguments):\n"
        "    program = (\n"
        "        \"import os, sys; \"\n"
        "        \"target = sys.stdout.buffer if sys.argv[1] == 'stdout' \"\n"
        "        \"else sys.stderr.buffer; \"\n"
        "        \"target.write(b'x' * int(sys.argv[2]))\"\n"
        "    )\n"
        "    return [sys.executable, '-c', program, arguments['stream'], "
        "str(arguments['size'])]\n"
    )
    (directory / "manifest.json").write_text(json.dumps({
        "plugin": "oversized",
        "version": "1.0.0",
        "handler": "handler.py",
        "tools": [{"name": "oversized_output"}],
    }))
    monkeypatch.setenv("AI_DATASEEK_TOOLS_DIR", str(tmp_path))
    payload = base64.urlsafe_b64encode(json.dumps({
        "stream": stream,
        "size": _MAX_PLUGIN_OUTPUT_BYTES + 1,
    }).encode()).decode()

    assert main([
        "run",
        "oversized_output",
        "--arguments-base64",
        payload,
    ]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.encode("utf-8")) < 512
    failure = json.loads(captured.err)
    assert failure == {
        "success": False,
        "error": "Plugin tool output exceeded the combined in-memory limit",
    }


def test_bounded_capture_kills_a_nonterminating_output_process_promptly():
    started = time.monotonic()
    result = _run_command_with_bounded_output([
        sys.executable,
        "-c",
        (
            "import os\n"
            "chunk = b'x' * 65536\n"
            "while True:\n"
            "    os.write(1, chunk)\n"
        ),
    ])

    assert time.monotonic() - started < 3
    assert result.output_limit_exceeded is True
    assert len(result.stdout) + len(result.stderr) == _MAX_PLUGIN_OUTPUT_BYTES
    assert result.returncode != 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are required")
def test_bounded_capture_stops_an_output_descendant_after_its_leader_exits():
    descendant_program = (
        "import os\n"
        "chunk = b'x' * 65536\n"
        "while True:\n"
        "    os.write(1, chunk)\n"
    )
    leader_program = (
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-c', {descendant_program!r}])\n"
    )

    started = time.monotonic()
    result = _run_command_with_bounded_output([
        sys.executable,
        "-c",
        leader_program,
    ])

    assert time.monotonic() - started < 3
    assert result.output_limit_exceeded is True
    assert len(result.stdout) + len(result.stderr) == _MAX_PLUGIN_OUTPUT_BYTES


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal forwarding is required")
def test_runner_forwards_outer_termination_to_isolated_tool_process_group(tmp_path):
    child_pid_path = tmp_path / "grandchild.pid"
    survived_marker = tmp_path / "grandchild-survived"
    leader_term_marker = tmp_path / "leader-received-term"
    grandchild_program = (
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(child_pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(1)\n"
        f"open({str(survived_marker)!r}, 'w').write('survived')\n"
        "time.sleep(30)\n"
    )
    child_program = (
        "import signal, subprocess, sys, time\n"
        "def on_term(*_args):\n"
        f"    open({str(leader_term_marker)!r}, 'w').write('term')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, on_term)\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild_program!r}])\n"
        "time.sleep(30)\n"
    )
    driver_program = (
        "import sys\n"
        "from scripts.tool_plugin_runner import _run_command_with_bounded_output\n"
        f"_run_command_with_bounded_output([sys.executable, '-c', {child_program!r}])\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "sandbox")
    driver = subprocess.Popen(
        [sys.executable, "-c", driver_program],
        cwd=ROOT / "sandbox",
        env=environment,
    )
    child_pid = None
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not child_pid_path.exists():
            time.sleep(0.01)
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text())

        driver.terminate()
        driver.wait(timeout=3)

        time.sleep(1.1)
        assert leader_term_marker.read_text() == "term"
        assert not survived_marker.exists()

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _process_is_running(child_pid):
            time.sleep(0.02)
        assert not _process_is_running(child_pid)
    finally:
        if driver.poll() is None:
            driver.kill()
            driver.wait(timeout=3)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_runner_rejects_a_different_cordis_catalog_before_execution(
    tmp_path,
    monkeypatch,
    capsys,
):
    directory = tmp_path / "echo"
    directory.mkdir()
    marker = tmp_path / "executed"
    (directory / "handler.py").write_text(
        "def build_command(name, arguments):\n"
        f"    return ['touch', {str(marker)!r}]\n"
    )
    (directory / "manifest.json").write_text(json.dumps({
        "plugin": "echo",
        "version": "1.0.0",
        "handler": "handler.py",
        "tools": [{"name": "echo_value"}],
    }))
    monkeypatch.setenv("AI_DATASEEK_TOOLS_DIR", str(tmp_path))
    payload = base64.urlsafe_b64encode(b"{}").decode()
    _, actual_digest = load_registry_snapshot(tmp_path)

    with pytest.raises(RuntimeError, match="does not match"):
        run_tool("echo_value", payload, "not-a-sha256")
    with pytest.raises(RuntimeError, match="does not match"):
        run_tool("echo_value", payload, "0" * 64)
    assert not marker.exists()

    assert run_tool("echo_value", payload, actual_digest) == 0
    assert marker.is_file()
    assert capsys.readouterr().err == ""


def test_manifest_digest_matches_the_cordis_cross_language_protocol_vector(tmp_path):
    alpha = '{"plugin":"alpha","version":"1.0.0","handler":"handler.py","tools":[{"name":"alpha_read","description":"Read alpha","parameters":{"type":"object","properties":{}}}]}'
    beta = '{"plugin":"beta","version":"2.1.0-rc.1","handler":"handler.py","tools":[{"name":"beta_read","description":"Read beta","parameters":{"type":"object","properties":{}}}]}'
    for directory_name, raw_manifest in (
        ("a-directory", beta),
        ("z-directory", alpha),
    ):
        directory = tmp_path / directory_name
        directory.mkdir()
        (directory / "handler.py").write_text(
            "def build_command(name, arguments): return ['true']\n"
        )
        (directory / "manifest.json").write_text(raw_manifest)

    registry, digest = load_registry_snapshot(tmp_path)
    assert set(registry) == {"alpha_read", "beta_read"}
    assert digest == "f986ce181c99df9a05172b01f222b6adf5d67d0e191849b3da76e270c46982ad"


def test_execution_bundle_digest_rejects_changed_handler_code(
    tmp_path,
    monkeypatch,
):
    tools_root = tmp_path / "tools"
    plugin_dir = tools_root / "echo"
    plugin_dir.mkdir(parents=True)
    handler = plugin_dir / "handler.py"
    handler.write_text('def build_command(name, arguments): return ["true"]\n')
    (plugin_dir / "manifest.json").write_text(
        '{"plugin":"echo","version":"1.0.0","handler":"handler.py",'
        '"tools":[{"name":"echo_value"}]}'
    )

    contract_root = tmp_path / "sandbox"
    for filename in ("Dockerfile", "pyproject.toml", "supervisord.conf", "uv.lock"):
        contract_root.mkdir(exist_ok=True)
        (contract_root / filename).write_text(f"fixture:{filename}\n")
    for directory in ("app", "scientific_operators", "scripts"):
        source_dir = contract_root / directory
        source_dir.mkdir()
        (source_dir / "runtime.py").write_text(f'SOURCE = "{directory}"\n')

    _, manifest_digest, bundle_digest = load_execution_registry_snapshot(
        tools_root,
        contract_root,
    )
    assert bundle_digest == (
        "823bc42462e71e25db409afbf989b910d5e5e2e93ccbadc8f19ac9f997dd5ccd"
    )
    monkeypatch.setenv("AI_DATASEEK_TOOLS_DIR", str(tools_root))
    monkeypatch.setenv("AI_DATASEEK_EXECUTION_CONTRACT_DIR", str(contract_root))
    payload = base64.urlsafe_b64encode(b"{}").decode()

    assert run_tool(
        "echo_value",
        payload,
        manifest_digest,
        bundle_digest,
    ) == 0
    handler.write_text("def build_command(name, arguments): return ['false']\n")
    with pytest.raises(RuntimeError, match="execution bundle does not match"):
        run_tool(
            "echo_value",
            payload,
            manifest_digest,
            bundle_digest,
        )


@pytest.mark.parametrize(("name", "arguments"), [
    ("scientific_inspect", {}),
    ("scientific_statistics", {"variable": "rain", "band": 1, "dimension_indices": {"time": 0}}),
    ("scientific_aggregate", {"dimension": "time", "method": "mean", "output_path": "/home/ubuntu/output/a.nc"}),
    ("scientific_subset", {"output_path": "/home/ubuntu/output/a.nc", "bbox": [0, 1, 2, 3]}),
    ("scientific_convert_netcdf_to_geotiff", {"output_path": "/home/ubuntu/output/a.tif"}),
    ("scientific_transform_raster", {"output_path": "/home/ubuntu/output/a.tif", "target_crs": "EPSG:4326"}),
    ("scientific_raster_index", {"output_path": "/home/ubuntu/output/a.tif", "index_name": "ndvi", "bands": {"nir": 4, "red": 3}}),
    ("scientific_terrain", {"output_path": "/home/ubuntu/output/a.tif", "operation": "slope"}),
    ("scientific_visualize", {"output_path": "/home/ubuntu/output/a.png"}),
    ("scientific_netcdf_visualize", {"output_dir": "/home/ubuntu/output/plots", "max_plots": 4}),
    ("scientific_point_timeseries", {"latitude": 30.0, "longitude": 110.0}),
    ("scientific_region_timeseries", {"method": "mean", "bbox": [100, 20, 110, 30]}),
    ("scientific_region_statistics", {"method": "max", "bbox": [100, 20, 110, 30]}),
    ("scientific_last_dimension_profile", {"dimension": "level"}),
])
def test_scientific_plugin_commands_match_existing_cli_contract(name, arguments):
    registry = load_registry(ROOT / "tools")
    _, handler_path = registry[name]
    command = load_handler(handler_path)(name, {
        "input_path": "/home/ubuntu/datasets/example.nc",
        **arguments,
    })

    parser = build_recipe_parser() if name in {
        "scientific_point_timeseries",
        "scientific_region_timeseries",
        "scientific_region_statistics",
        "scientific_last_dimension_profile",
    } else build_scientific_parser()
    parsed = parser.parse_args(command[1:])
    assert parsed.input_path == Path("/home/ubuntu/datasets/example.nc")
