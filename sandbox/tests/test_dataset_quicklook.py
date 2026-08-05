import json
import io
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import numpy as np
import pytest

from scripts.dataset_quicklook import (
    Limits,
    QuicklookError,
    _quicklook_evidence,
    generate_quicklook,
)


def _artifact_paths(output: Path, manifest: dict) -> list[Path]:
    return [output / item["path"] for item in manifest["artifacts"]]


def test_csv_quicklook_is_bounded_and_writes_complete_artifact_manifest(tmp_path):
    source = tmp_path / "碳收支.csv"
    rows = ["年份,区域,碳排放,碳吸收"]
    for offset in range(80):
        absorption = "" if offset % 4 == 0 else str(60 + offset)
        rows.append(f"{1981 + offset},华北,{100 + offset},{absorption}")
    source.write_text("\n".join(rows) + "\n", encoding="utf-8")
    output = tmp_path / "quicklook"

    manifest = generate_quicklook(
        source,
        output,
        Limits(max_rows_per_table=12, max_text_bytes=2_048, max_plot_points=8),
    )

    assert manifest["success"] is True
    assert manifest["summary"]["files_analyzed"] == 1
    assert 1 <= manifest["summary"]["plot_count"] <= 4
    table = manifest["datasets"][0]["table"]
    assert table["rows_sampled"] == 12
    assert table["truncated"] is True
    assert [column["name"] for column in table["columns"]] == [
        "年份",
        "区域",
        "碳排放",
        "碳吸收",
    ]
    assert all(path.is_file() and path.stat().st_size > 0 for path in _artifact_paths(output, manifest))

    persisted = json.loads((output / "quicklook_manifest.json").read_text(encoding="utf-8"))
    assert persisted == manifest
    markdown = (output / "quicklook_summary.md").read_text(encoding="utf-8")
    assert "数据集快速探查" in markdown
    assert "## 可核验数据证据" in markdown
    assert "## 方法与适用边界" in markdown
    assert "碳排放" in markdown

    evidence = _quicklook_evidence(manifest)
    assert evidence["datasets"][0]["table"]["columns"][0]["name"] == "年份"
    assert evidence["capabilities"]["explicit_temporal_dimensions"][0]["field"] == "年份"


def test_directory_quicklook_recognizes_excel_and_geotiff_without_full_raster_read(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    source = tmp_path / "dataset"
    source.mkdir()
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "年度数据"
    sheet.append(["年份", "降水量"])
    for year in range(2000, 2020):
        sheet.append([year, year - 1900])
    workbook.save(source / "降水.xlsx")

    raster_path = source / "降水.tif"
    raster = np.arange(120 * 160, dtype=np.float32).reshape(120, 160)
    with rasterio.open(
        raster_path,
        "w",
        driver="GTiff",
        width=160,
        height=120,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(100, 40, 0.01, 0.01),
    ) as dataset:
        dataset.write(raster, 1)

    output = tmp_path / "quicklook"
    manifest = generate_quicklook(
        source,
        output,
        Limits(max_raster_pixels=400, max_rows_per_table=7),
    )

    assert manifest["summary"]["files_analyzed"] == 2
    assert {item["format"] for item in manifest["datasets"]} == {"excel", "geotiff"}
    raster_profile = next(item for item in manifest["datasets"] if item["format"] == "geotiff")
    assert raster_profile["sampling"]["pixels_per_band"] <= 400
    assert raster_profile["sampling"]["truncated"] is True
    excel_profile = next(item for item in manifest["datasets"] if item["format"] == "excel")
    assert excel_profile["sheets"][0]["table"]["rows_sampled"] == 7


def test_zip_input_uses_recursive_unpacker_then_analyzes_dataset(tmp_path):
    archive = tmp_path / "dataset.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as writer:
        writer.writestr("nested/values.tsv", "时间\t值\n2020\t1\n2021\t2\n")

    output = tmp_path / "quicklook"
    manifest = generate_quicklook(archive, output, Limits())

    assert manifest["source"]["type"] == "archive"
    assert manifest["source"]["archive"]["archive_count"] == 1
    assert manifest["datasets"][0]["path"] == "dataset.zip!/nested/values.tsv"
    assert manifest["datasets"][0]["format"] == "tsv"
    organization = manifest["file_organization"]
    assert organization["root"] == {
        "name": "dataset.zip",
        "type": "archive",
        "format": "zip",
        "size": archive.stat().st_size,
    }
    assert organization["archive_layers"] == [
        {
            "path": "dataset.zip",
            "format": "zip",
            "level": 1,
            "extracted_to": "dataset.zip!/",
        }
    ]
    assert organization["extracted_tree"]["entries"] == [
        {
            "path": "dataset.zip!/nested/values.tsv",
            "type": "file",
            "size": len("时间\t值\n2020\t1\n2021\t2\n".encode("utf-8")),
            "format": "tsv",
        }
    ]
    markdown = (output / "quicklook_summary.md").read_text(encoding="utf-8")
    assert "## 文件组织结构" in markdown
    assert "### 原始目录" in markdown
    assert "### 压缩包层级" in markdown
    assert "### 解压后文件树" in markdown
    assert "dataset.zip!/nested/values.tsv" in markdown
    assert str(tmp_path) not in markdown
    assert not (output / "unpacked").exists()
    assert {path.name for path in output.iterdir()} == {
        *(item["path"] for item in manifest["artifacts"]),
    }


def test_directory_quicklook_discovers_deep_archive_and_removes_unpack_workspace(tmp_path):
    source = tmp_path / "mounted-root"
    archive_directory = source / "sources" / "2020" / "raw"
    archive_directory.mkdir(parents=True)
    archive = archive_directory / "tables.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as writer:
        writer.writestr("inside/观测.csv", "年份,温度\n2020,12.5\n2021,13.1\n")
    output = tmp_path / "quicklook"

    manifest = generate_quicklook(source, output, Limits())

    archive_report = manifest["source"]["directory_archives"]
    assert archive_report["discovered"] == 1
    assert archive_report["processed"] == 1
    assert archive_report["failed"] == 0
    assert archive_report["archive_count"] == 1
    assert archive_report["expanded_bytes"] > 0
    assert archive_report["items"][0]["path"] == "sources/2020/raw/tables.zip"
    assert manifest["datasets"][0]["path"] == (
        "sources/2020/raw/tables.zip!/inside/观测.csv"
    )
    organization = manifest["file_organization"]
    original_paths = {
        entry["path"] for entry in organization["original_tree"]["entries"]
    }
    assert {
        "sources",
        "sources/2020",
        "sources/2020/raw",
        "sources/2020/raw/tables.zip",
    } <= original_paths
    assert organization["archive_layers"][0]["path"] == (
        "sources/2020/raw/tables.zip"
    )
    assert organization["extracted_tree"]["entries"][0]["path"] == (
        "sources/2020/raw/tables.zip!/inside/观测.csv"
    )
    assert str(tmp_path) not in json.dumps(manifest, ensure_ascii=False)
    assert not (output / "unpacked_archives").exists()
    assert all(path.is_file() for path in _artifact_paths(output, manifest))
    assert {path.name for path in output.iterdir()} == {
        *(item["path"] for item in manifest["artifacts"]),
    }


def test_nested_archive_hierarchy_and_extracted_tree_are_public_logical_paths(tmp_path):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", compression=zipfile.ZIP_DEFLATED) as writer:
        writer.writestr("tables/年度.csv", "年份,降水量\n2020,120\n2021,135\n")
    archive = tmp_path / "气候数据.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as writer:
        writer.writestr("nested/年度数据.zip", inner.getvalue())

    output = tmp_path / "quicklook"
    manifest = generate_quicklook(archive, output, Limits())

    organization = manifest["file_organization"]
    assert [item["path"] for item in organization["archive_layers"]] == [
        "气候数据.zip",
        "气候数据.zip!/nested/年度数据.zip",
    ]
    assert [item["level"] for item in organization["archive_layers"]] == [1, 2]
    assert organization["extracted_tree"] == {
        "entries": [
            {
                "path": "气候数据.zip!/nested/archive_contents/tables/年度.csv",
                "type": "file",
                "size": len("年份,降水量\n2020,120\n2021,135\n".encode("utf-8")),
                "format": "csv",
            }
        ],
        "entries_listed": 1,
        "truncated": False,
    }
    serialized = json.dumps(manifest, ensure_ascii=False)
    markdown = (output / "quicklook_summary.md").read_text(encoding="utf-8")
    assert str(tmp_path) not in serialized
    assert str(tmp_path) not in markdown
    assert "第 2 层" in markdown
    assert "archive_contents/" in markdown
    assert not (output / "unpacked").exists()


def test_original_tree_listing_has_an_independent_display_boundary(tmp_path):
    source = tmp_path / "large-directory"
    source.mkdir()
    source.joinpath("values.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    for index in range(8):
        source.joinpath(f"说明-{index}.txt").write_text("metadata", encoding="utf-8")

    output = tmp_path / "quicklook"
    manifest = generate_quicklook(
        source,
        output,
        Limits(max_tree_entries=3),
    )

    original = manifest["file_organization"]["original_tree"]
    assert original["entries_listed"] == 3
    assert original["truncated"] is True
    assert len(original["entries"]) == 3
    markdown = (output / "quicklook_summary.md").read_text(encoding="utf-8")
    assert "原始目录内容较多，仅展示前 3 项" in markdown


def test_analysis_errors_redact_private_working_paths(tmp_path):
    source = tmp_path / "mounted-dataset"
    source.mkdir()
    source.joinpath("values.csv").write_text("x,y\n1,2\n2,3\n", encoding="utf-8")
    source.joinpath("损坏.tif").write_bytes(b"not-a-valid-geotiff")
    output = tmp_path / "quicklook"

    manifest = generate_quicklook(source, output, Limits())

    assert manifest["summary"]["files_analyzed"] == 1
    assert manifest["summary"]["files_failed"] == 1
    assert manifest["errors"][0]["path"] == "损坏.tif"
    assert str(tmp_path) not in json.dumps(manifest, ensure_ascii=False)
    assert str(tmp_path) not in (output / "quicklook_summary.md").read_text(
        encoding="utf-8"
    )


def test_single_geotiff_generates_three_high_value_plots_from_sampled_pixels(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    source = tmp_path / "elevation.tif"
    raster = np.linspace(10, 1000, 300 * 400, dtype=np.float32).reshape(300, 400)
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=400,
        height=300,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(100, 40, 0.01, 0.01),
    ) as dataset:
        dataset.write(raster, 1)
    output = tmp_path / "quicklook"

    manifest = generate_quicklook(
        source,
        output,
        Limits(max_raster_pixels=600, max_plot_points=120, max_plots=4),
    )

    assert manifest["summary"]["plot_count"] == 3
    images = [
        item for item in manifest["artifacts"] if item["media_type"] == "image/png"
    ]
    assert [item["title"].split(" · ")[-1] for item in images] == [
        "栅格快视图",
        "数值分布与 CDF",
        "行列剖面",
    ]
    sampling = manifest["datasets"][0]["sampling"]
    assert sampling["pixels_per_band"] <= 600
    assert sampling["truncated"] is True
    spatial_profile = manifest["datasets"][0]["spatial_profile"]
    assert spatial_profile["valid_fraction_percent"] == 100.0
    assert spatial_profile["quantiles"]["p50"] == pytest.approx(505, rel=0.02)
    assert set(spatial_profile["zone_means"]) == {
        "upper_left",
        "upper_right",
        "lower_left",
        "lower_right",
        "center",
    }
    assert spatial_profile["maximum_location"]["row"] < 300
    assert spatial_profile["maximum_location"]["column"] < 400
    assert {path.name for path in output.iterdir()} == {
        *(item["path"] for item in manifest["artifacts"]),
    }
    evidence = _quicklook_evidence(manifest)
    raster_evidence = evidence["datasets"][0]
    assert raster_evidence["crs"] == "EPSG:4326"
    assert raster_evidence["bands"][0]["mean"] == pytest.approx(505, rel=0.02)
    assert evidence["capabilities"]["explicit_temporal_dimensions"] == []
    assert evidence["capabilities"]["explicit_spatial_dimensions"][0]["type"] == "raster_grid"
    markdown = (output / "quicklook_summary.md").read_text(encoding="utf-8")
    assert "不能仅依据文件名" in markdown
    assert "最小/均值/最大/标准差" in markdown
    assert "栅格分区均值" in markdown


def test_existing_output_is_not_overwritten(tmp_path):
    source = tmp_path / "values.csv"
    source.write_text("x,y\n1,2\n", encoding="utf-8")
    output = tmp_path / "quicklook"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(QuicklookError, match="already exists"):
        generate_quicklook(source, output)

    assert marker.read_text(encoding="utf-8") == "keep"


def test_integer_raster_spatial_profile_handles_declared_nodata(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    source = tmp_path / "integer.tif"
    values = np.arange(48, dtype=np.int16).reshape(6, 8)
    values[0, 0] = -9999
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=8,
        height=6,
        count=1,
        dtype="int16",
        nodata=-9999,
        transform=from_origin(0, 6, 1, 1),
    ) as dataset:
        dataset.write(values, 1)

    manifest = generate_quicklook(
        source,
        tmp_path / "out",
        Limits(max_raster_pixels=100, max_plots=1),
    )

    profile = manifest["datasets"][0]["spatial_profile"]
    assert profile["valid_pixels"] == 47
    assert profile["valid_fraction_percent"] == pytest.approx(97.917, abs=0.001)


def test_raster_without_declared_nodata_keeps_zeroes_in_authoritative_statistics(
    tmp_path,
):
    rasterio = pytest.importorskip("rasterio")

    source = tmp_path / "zeroes_are_data.tif"
    values = np.zeros((4, 5), dtype=np.float32)
    values[-1, -2:] = [2, 4]
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=5,
        height=4,
        count=1,
        dtype="float32",
    ) as dataset:
        dataset.write(values, 1)

    manifest = generate_quicklook(
        source,
        tmp_path / "out-no-nodata",
        Limits(max_raster_pixels=100, max_plots=1),
    )

    raster = manifest["datasets"][0]
    band = raster["bands"][0]
    assert raster["declared_nodata"] is None
    assert raster["declared_unit"] is None
    assert raster["mask_provenance"] == ["all_valid"]
    assert raster["masked_count"] == 0
    assert raster["nan_count"] == 0
    assert raster["zero_count"] == 18
    assert raster["valid_zero_count"] == 18
    assert band["valid_pixels_sampled"] == 20
    assert band["min"] == 0
    assert band["mean"] == pytest.approx(0.3)

    evidence = _quicklook_evidence(manifest)["datasets"][0]
    assert evidence["declared_nodata"] is None
    assert evidence["declared_unit"] is None
    assert evidence["bands"][0]["declared_unit"] is None
    assert evidence["mask_provenance"] == ["all_valid"]
    assert evidence["masked_count"] == 0
    assert evidence["zero_count"] == 18
    markdown = (tmp_path / "out-no-nodata" / "quicklook_summary.md").read_text(
        encoding="utf-8"
    )
    assert "声明的单位为 `null（未声明）`" in markdown
    assert "其中未被掩膜、保留为有效值的零值 18 个" in markdown


def test_declared_raster_band_unit_is_reported_from_metadata(tmp_path):
    rasterio = pytest.importorskip("rasterio")

    source = tmp_path / "declared-unit.tif"
    values = np.arange(12, dtype=np.float32).reshape(3, 4)
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=4,
        height=3,
        count=1,
        dtype="float32",
    ) as dataset:
        dataset.write(values, 1)
        dataset.set_band_unit(1, "m s-1")

    output = tmp_path / "out-declared-unit"
    manifest = generate_quicklook(
        source,
        output,
        Limits(max_raster_pixels=100, max_plots=1),
    )

    raster = manifest["datasets"][0]
    assert raster["declared_unit"] == "m s-1"
    assert raster["bands"][0]["declared_unit"] == "m s-1"
    evidence = _quicklook_evidence(manifest)["datasets"][0]
    assert evidence["declared_unit"] == "m s-1"
    assert evidence["bands"][0]["declared_unit"] == "m s-1"
    assert "声明的单位为 `m s-1`" in (
        output / "quicklook_summary.md"
    ).read_text(encoding="utf-8")


def test_raster_with_zero_as_declared_nodata_excludes_zeroes_from_statistics(
    tmp_path,
):
    rasterio = pytest.importorskip("rasterio")

    source = tmp_path / "zero_is_nodata.tif"
    values = np.zeros((4, 5), dtype=np.float32)
    values[-1, -2:] = [2, 4]
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=5,
        height=4,
        count=1,
        dtype="float32",
        nodata=0,
    ) as dataset:
        dataset.write(values, 1)

    manifest = generate_quicklook(
        source,
        tmp_path / "out-zero-nodata",
        Limits(max_raster_pixels=100, max_plots=1),
    )

    raster = manifest["datasets"][0]
    band = raster["bands"][0]
    assert raster["declared_nodata"] == 0
    assert raster["mask_provenance"] == ["nodata"]
    assert raster["masked_count"] == 18
    assert raster["nan_count"] == 0
    assert raster["zero_count"] == 18
    assert raster["valid_zero_count"] == 0
    assert band["valid_pixels_sampled"] == 2
    assert band["min"] == 2
    assert band["mean"] == pytest.approx(3)


def test_nonzero_declared_nodata_does_not_discard_legitimate_zeroes(tmp_path):
    rasterio = pytest.importorskip("rasterio")

    source = tmp_path / "legitimate_zeroes.tif"
    values = np.zeros((4, 5), dtype=np.float32)
    values[0, :2] = -9999
    values[-1, -2:] = [2, 4]
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=5,
        height=4,
        count=1,
        dtype="float32",
        nodata=-9999,
    ) as dataset:
        dataset.write(values, 1)

    manifest = generate_quicklook(
        source,
        tmp_path / "out-negative-nodata",
        Limits(max_raster_pixels=100, max_plots=1),
    )

    raster = manifest["datasets"][0]
    band = raster["bands"][0]
    assert raster["declared_nodata"] == -9999
    assert raster["mask_provenance"] == ["nodata"]
    assert raster["masked_count"] == 2
    assert raster["nan_count"] == 0
    assert raster["zero_count"] == 16
    assert raster["valid_zero_count"] == 16
    assert band["valid_pixels_sampled"] == 18
    assert band["min"] == 0
    assert band["mean"] == pytest.approx(1 / 3)


def test_per_dataset_mask_provenance_is_reported_and_used(tmp_path):
    rasterio = pytest.importorskip("rasterio")

    source = tmp_path / "dataset_mask.tif"
    values = np.arange(12, dtype=np.uint8).reshape(3, 4)
    mask = np.full((3, 4), 255, dtype=np.uint8)
    mask[0, 0] = 0
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=4,
        height=3,
        count=1,
        dtype="uint8",
    ) as dataset:
        dataset.write(values, 1)
        dataset.write_mask(mask)

    manifest = generate_quicklook(
        source,
        tmp_path / "out-dataset-mask",
        Limits(max_raster_pixels=100, max_plots=1),
    )

    raster = manifest["datasets"][0]
    band = raster["bands"][0]
    assert raster["declared_nodata"] is None
    assert "per_dataset" in raster["mask_provenance"]
    assert raster["masked_count"] == 1
    assert raster["zero_count"] == 1
    assert raster["valid_zero_count"] == 0
    assert band["valid_pixels_sampled"] == 11
    assert band["min"] == 1


def test_cli_returns_nonzero_and_json_error_for_unsupported_input(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("not a dataset", encoding="utf-8")
    command = Path(__file__).parents[1] / "scripts/dataset_quicklook.py"

    result = subprocess.run(
        [sys.executable, str(command), str(source), "--output", str(tmp_path / "out")],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONUTF8": "1", "MPLBACKEND": "Agg"},
    )

    assert result.returncode != 0
    payload = json.loads(result.stderr)
    assert payload["success"] is False
    assert "unsupported input" in payload["error"]


def test_cli_returns_compact_evidence_for_one_turn_model_answer(tmp_path):
    source = tmp_path / "values.csv"
    source.write_text(
        "年份,区域,降水量\n2019,北,10\n2020,北,20\n2021,南,30\n",
        encoding="utf-8",
    )
    command = Path(__file__).parents[1] / "scripts/dataset_quicklook.py"

    result = subprocess.run(
        [
            sys.executable,
            str(command),
            str(source),
            "--output",
            str(tmp_path / "out"),
            "--max-plots",
            "2",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONUTF8": "1", "MPLBACKEND": "Agg"},
    )

    assert result.returncode == 0
    assert len(result.stdout.encode("utf-8")) < 24 * 1024
    payload = json.loads(result.stdout)
    assert payload["artifacts"]
    assert payload["artifacts"][0]["path"] in payload["files"]
    assert payload["artifacts"][0]["title"]
    assert payload["artifacts"][0]["media_type"] == "image/png"
    assert payload["evidence"]["datasets"][0]["table"]["columns"][2][
        "statistics"
    ]["mean"] == 20.0
    assert payload["evidence"]["capabilities"]["explicit_temporal_dimensions"][0][
        "field"
    ] == "年份"
