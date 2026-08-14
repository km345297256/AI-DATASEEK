from app.domain.services.prompts.execution import EXECUTION_PROMPT
from app.domain.services.prompts.system import SYSTEM_PROMPT


def _rendered_prompts():
    execution_prompt = EXECUTION_PROMPT.format(
        step="Create a chart",
        message="Visualize the dataset",
        attachments="[]",
        language="Chinese",
        dataset_contract="(No mounted-dataset fast-path contract applies to this step.)",
    )
    return SYSTEM_PROMPT, execution_prompt


def test_visualization_prompts_define_the_chinese_font_policy():
    for prompt in _rendered_prompts():
        assert "global sans-serif default" in prompt
        assert "Noto Sans CJK SC" in prompt
        assert "SimHei" in prompt
        assert "Microsoft YaHei" in prompt
        assert "unavailable fonts" in prompt
        assert "monospace" in prompt


def test_execution_prompt_prefers_bounded_shell_and_preinstalled_archives():
    prompt = _rendered_prompts()[1]
    assert "shell_run" in prompt
    assert "dataset_unpack" in prompt
    for command in ("zip", "unzip", "unrar", "7z"):
        assert command in prompt
    assert "Runtime dependency installation is forbidden" in prompt


def test_analysis_prompts_forbid_runtime_dependency_installation_and_advertise_raster_stack():
    system_prompt, execution_prompt = _rendered_prompts()
    for command in ("apt", "pip", "uv add", "npm install"):
        assert command in execution_prompt
    assert "Runtime dependency installation is forbidden" in execution_prompt
    assert "from osgeo import gdal" in execution_prompt
    assert "preinstalled rasterio or GDAL" in execution_prompt
    assert "rasterio" in system_prompt
    assert "Never run `apt`" in system_prompt
    assert "Install only uncommon missing dependencies" not in system_prompt


def test_analysis_prompts_advertise_the_offline_geoscience_stack():
    system_prompt, execution_prompt = _rendered_prompts()
    for package in (
        "xarray",
        "Dask",
        "netCDF4",
        "h5netcdf",
        "Zarr",
        "SciPy",
        "GeoPandas",
        "Pyogrio",
        "Shapely",
        "PyProj",
        "rioxarray",
    ):
        assert package in system_prompt
        assert package in execution_prompt
    for command in ("ncdump", "h5dump", "projinfo"):
        assert command in system_prompt
        assert command in execution_prompt


def test_analysis_prompts_preserve_zero_nodata_and_unit_semantics():
    system_prompt, execution_prompt = _rendered_prompts()
    for prompt in (system_prompt, execution_prompt):
        assert "numeric zero" in prompt
        assert "source metadata" in prompt
        assert "explicit user rule" in prompt
        assert "Never infer units" in prompt
    assert "raw/unit-not-declared" in execution_prompt


def test_quicklook_policy_requires_matching_scope_and_uses_returned_evidence():
    system_prompt, execution_prompt = _rendered_prompts()
    for prompt in (system_prompt, execution_prompt):
        assert "optional bounded profiling and visualization tool" in prompt
        assert "scope matches the user's request" in prompt
        assert "evidence, not" in prompt
    assert "Do not manually unpack" in execution_prompt
    assert "recreate quicklook charts" in execution_prompt


def test_visualization_prompts_define_unicode_and_png_output_policy():
    for prompt in _rendered_prompts():
        assert 'matplotlib.rcParams["axes.unicode_minus"] = False' in prompt
        assert "UTF-8" in prompt
        assert "PNG" in prompt
        assert "/home/ubuntu/output" in prompt


def test_visualization_prompts_avoid_unicode_superscript_units():
    for prompt in _rendered_prompts():
        assert "U+207B" in prompt
        assert "$m^{-2}$" in prompt
        assert "m^-2" in prompt


def test_prompts_prefer_deterministic_scientific_operators():
    system_prompt, execution_prompt = _rendered_prompts()
    assert "scientific_*" in system_prompt
    for prompt in (system_prompt, execution_prompt):
        assert "dynamically registered" in prompt
        assert "schema and description" in prompt
    assert "newly installed" in execution_prompt
    assert "without prompt edits" in system_prompt
