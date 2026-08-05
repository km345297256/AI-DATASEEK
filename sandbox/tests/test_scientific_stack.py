import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_common_excel_and_visualization_stack_is_preinstalled():
    import matplotlib
    import numpy
    import openpyxl
    import pandas
    import seaborn
    import xlrd
    from PIL import Image

    assert int(numpy.__version__.split(".", 1)[0]) < 2
    assert all(
        module is not None
        for module in (matplotlib, openpyxl, pandas, seaborn, xlrd, Image)
    )


def test_python_and_pip_use_the_same_virtual_environment():
    venv_path = Path(os.environ.get("VIRTUAL_ENV") or sys.prefix)
    assert venv_path != Path(sys.base_prefix)
    assert Path(sys.executable).is_relative_to(venv_path)
    assert Path(shutil.which("python") or "").is_relative_to(venv_path)
    assert Path(shutil.which("python3") or "").is_relative_to(venv_path)
    assert Path(shutil.which("pip") or "").is_relative_to(venv_path)
    assert Path(shutil.which("pip3") or "").is_relative_to(venv_path)

    pip_version = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(venv_path) in pip_version


def test_gdal_remains_importable_with_locked_numpy():
    gdal = pytest.importorskip("osgeo.gdal")

    assert gdal.VersionInfo()
