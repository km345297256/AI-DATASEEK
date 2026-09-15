"""Independent, deterministic development gold for the 18 bundled sources.

This module does not import DataSeek readers or call a model. Run it in the
existing scientific sandbox image (numpy, Pillow, xlrd, netCDF4, astropy, GDAL,
pypdf). Its output contains HIDDEN answers and must stay outside agent inputs.
Only public_task() from the benchmark protocol may cross that boundary.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
import xml.etree.ElementTree as ET


DATASET_IDS = (
    "bbbc007-drosophila-cells", "bbbc039-nuclei-preview",
    "mendeley-calcium-carbonate", "mendeley-high-entropy-alloys",
    "nasa-hst-fos", "nasa-hst-wfpc2", "ncbi-arabidopsis-chloroplast",
    "ncbi-lambda-reference", "open-natural-earth-countries",
    "open-noaa-air-climatology", "open-uci-concrete", "open-uci-iris",
    "open-uci-wine", "open-uci-wine-quality", "pdb-crambin", "pdb-ubiquitin",
    "plos-jupyter-notebooks", "plos-reproducible-research",
)


def _mean(values):
    values = list(values)
    if not values:
        raise ValueError("Cannot compute a mean without observations")
    return math.fsum(float(v) for v in values) / len(values)


def _task(dataset, root, number, instruction, schema, answer, files, *, atol=1e-6):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    # Input identity is computed from actual bytes, never copied from metadata.
    # No host path is included in the task or in the prompt.
    input_files = sorted(files)
    identities = []
    for name in input_files:
        path = root / name
        if path.is_symlink() or not path.is_file() or path.parent != root:
            raise ValueError("Oracle input must be a regular dataset file")
        content = path.read_bytes()
        identities.append({"name": name, "size": len(content),
                           "sha256": hashlib.sha256(content).hexdigest()})
    return {
        "id": f"{dataset}--{'structure' if number == 1 else 'analysis'}",
        "dataset_id": dataset,
        "domain": manifest["domain"],
        "source_family_id": dataset,
        "task_type": "structure" if number == 1 else "analysis",
        "prompt": (
            f"使用数据集 {dataset} 的原始文件：{', '.join(input_files)}。\n"
            + instruction
            + "\n将结果写入 answer.json；文件必须是一个 JSON 对象，顶层只有 answer 键。"
            + "不要从目录名称、SOURCE.md 或 manifest 中推测数值；读取实际输入计算。"
            + "\nanswer 的结构（这里只描述类型，不给出答案）为：" + schema
            + "。保留足够数值精度，禁止 NaN/Infinity。不要改写原始数据。"
        ),
        "expected": {"answer": answer},
        "tolerance": {"atol": atol, "rtol": 1e-6},
        "required_artifacts": [{"name": "answer.json", "kind": "json"}],
        "input_files": input_files,
        "metadata": {"input_identities": identities,
                     "oracle_version": "bundled-independent-v1"},
    }


def _csv_table(path, delimiter=","):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream, delimiter=delimiter))
    if not rows or any(len(row) != len(rows[0]) for row in rows[1:]):
        raise ValueError("Unexpected CSV table shape")
    return rows[0], rows[1:]


def _csv_tasks(dataset, root):
    names = sorted(p.name for p in root.glob("*.csv"))
    delimiter = ";" if dataset == "open-uci-wine-quality" else ","
    tables = {name: _csv_table(root / name, delimiter) for name in names}
    structure = {"tables": [{"filename": name, "row_count": len(rows),
                              "columns": columns}
                             for name, (columns, rows) in tables.items()]}
    first = _task(dataset, root, 1,
        "读取 CSV，分别报告数据行数（不含表头）和按原顺序的列名。tables 按文件名字典序排序。",
        '{"tables":[{"filename":字符串,"row_count":整数,"columns":[字符串,...]},...]}',
        structure, names)
    if dataset == "open-uci-wine-quality":
        stats = []
        for name, (columns, rows) in tables.items():
            quality = [int(row[columns.index("quality")]) for row in rows]
            alcohol = [float(row[columns.index("alcohol")]) for row in rows]
            stats.append({"filename": name, "mean_quality": _mean(quality),
                          "mean_alcohol": _mean(alcohol),
                          "quality_ge_7_count": sum(q >= 7 for q in quality)})
        instruction = "分别计算每张表 quality 和 alcohol 的算术平均值，以及 quality >= 7 的行数。tables 按文件名字典序排序。"
        schema = '{"tables":[{"filename":字符串,"mean_quality":数值,"mean_alcohol":数值,"quality_ge_7_count":整数},...]}'
        answer = {"tables": stats}
    else:
        columns, rows = next(iter(tables.values()))
        label = "species" if dataset == "open-uci-iris" else "class"
        measure = "sepal_length_cm" if label == "species" else "alcohol"
        groups = sorted(set(row[columns.index(label)] for row in rows))
        answer = {"groups": [{"label": group, "count": len(selected),
                               "mean": _mean(row[columns.index(measure)] for row in selected)}
                              for group in groups
                              for selected in [[row for row in rows if row[columns.index(label)] == group]]]}
        instruction = f"按 {label} 分组，输出每组行数及 {measure} 的算术平均值。label 用字符串，即使源标签为数值；groups 按 label 字典序排序。"
        schema = '{"groups":[{"label":字符串,"count":整数,"mean":数值},...]}'
    return [first, _task(dataset, root, 2, instruction, schema, answer, names)]


def _excel_tasks(dataset, root):
    import xlrd
    name = "Concrete_Data.xls"
    book = xlrd.open_workbook(str(root / name))
    sheets = [{"name": sheet.name, "row_count": sheet.nrows, "column_count": sheet.ncols}
              for sheet in book.sheets()]
    nonempty = [sheet for sheet in book.sheets() if sheet.nrows]
    if len(nonempty) != 1 or nonempty[0].ncols != 9:
        raise ValueError("Concrete workbook schema changed; review the oracle")
    sheet = nonempty[0]
    ages = sheet.col_values(7, start_rowx=1)
    strength = sheet.col_values(8, start_rowx=1)
    return [
        _task(dataset, root, 1, "报告全部工作表（包括空表）的名称、行数和列数，按工作簿顺序输出。非空表行数包含表头，空表行列数为零。",
              '{"sheets":[{"name":字符串,"row_count":整数,"column_count":整数},...]}',
              {"sheets": sheets}, [name]),
        _task(dataset, root, 2, "读取唯一非空工作表，去除表头；对第 8 列 Age (day) 和第 9 列抗压强度计算指定统计，不筛选样本。强度单位为 MPa。",
              '{"data_row_count":整数,"mean_age_days":数值,"mean_strength_mpa":数值,"min_strength_mpa":数值,"max_strength_mpa":数值}',
              {"data_row_count": len(strength), "mean_age_days": _mean(ages),
               "mean_strength_mpa": _mean(strength), "min_strength_mpa": min(strength),
               "max_strength_mpa": max(strength)}, [name]),
    ]


def _fasta_records(path):
    records = []
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(">"):
            current = [line[1:].split()[0], ""]
            records.append(current)
        elif line.strip():
            if current is None:
                raise ValueError("FASTA sequence before header")
            current[1] += "".join(line.split()).upper()
    if not records:
        raise ValueError("Empty FASTA")
    return records


def _fasta_tasks(dataset, root):
    name = next(root.glob("*.fasta")).name
    records = _fasta_records(root / name)
    counts = Counter("".join(seq for _, seq in records))
    valid = sum(counts[base] for base in "ACGT")
    if not valid:
        raise ValueError("No canonical FASTA bases")
    return [
        _task(dataset, root, 1, "按 FASTA 文件顺序列出所有记录的 ID（标题中第一个空白之前的部分）和序列长度。长度排除换行及空白，不排除歧义字符。",
              '{"records":[{"id":字符串,"length":整数},...]}',
              {"records": [{"id": ident, "length": len(seq)} for ident, seq in records]}, [name]),
        _task(dataset, root, 2, "合并文件内所有序列，忽略大小写与空白；分别统计 A/C/G/T 和其他字符。gc_fraction=(G+C)/(A+C+G+T)，为 0 到 1 的比例而非百分数。",
              '{"A":整数,"C":整数,"G":整数,"T":整数,"other":整数,"gc_fraction":数值}',
              {**{base: counts[base] for base in "ACGT"}, "other": sum(counts.values()) - valid,
               "gc_fraction": (counts["G"] + counts["C"]) / valid}, [name]),
    ]


def _pdb_tasks(dataset, root):
    name = next(root.glob("*.pdb")).name
    lines = (root / name).read_text(encoding="ascii").splitlines()
    atoms = [line for line in lines if line.startswith("ATOM  ")]
    if not atoms:
        raise ValueError("No ATOM records")
    xyz = [[float(line[start:start + 8]) for start in (30, 38, 46)] for line in atoms]
    return [
        _task(dataset, root, 1, "以 PDB 固定宽度记录为准，统计 ATOM 与 HETATM 行数。chains 为 ATOM 行链标识去重后按字典序；atom_residue_count 按 (chainID,resSeq,iCode) 对 ATOM 行去重。不要将水/其他 HETATM 算进 ATOM 残基数。",
              '{"atom_record_count":整数,"hetatm_record_count":整数,"chains":[字符串,...],"atom_residue_count":整数}',
              {"atom_record_count": len(atoms), "hetatm_record_count": sum(line.startswith("HETATM") for line in lines),
               "chains": sorted({line[21:22].strip() for line in atoms}),
               "atom_residue_count": len({(line[21:22], line[22:26], line[26:27]) for line in atoms})}, [name]),
        _task(dataset, root, 2, "仅使用所有 ATOM 行的原始 xyz 坐标（Å），每行等权，不按元素质量或 occupancy 加权，不包含 HETATM。报告 xyz 算术平均坐标及逐轴最小值/最大值；三个数组顺序均为 x,y,z。",
              '{"centroid_xyz":[数值,数值,数值],"min_xyz":[数值,数值,数值],"max_xyz":[数值,数值,数值]}',
              {"centroid_xyz": [_mean(row[i] for row in xyz) for i in range(3)],
               "min_xyz": [min(row[i] for row in xyz) for i in range(3)],
               "max_xyz": [max(row[i] for row in xyz) for i in range(3)]}, [name]),
    ]


def _xrd_scan(path):
    root = ET.parse(path).getroot()
    local = lambda elem: elem.tag.rsplit("}", 1)[-1]
    scan = next(elem for elem in root.iter() if local(elem) == "scan")
    position = next(elem for elem in scan.iter() if local(elem) == "positions" and elem.get("axis") == "2Theta")
    intensity = next(elem for elem in scan.iter() if local(elem) in ("intensities", "counts"))
    start = float(next(elem.text for elem in position if local(elem) == "startPosition"))
    end = float(next(elem.text for elem in position if local(elem) == "endPosition"))
    values = [float(value) for value in intensity.text.split()]
    if len(values) < 2 or position.get("unit") != "deg":
        raise ValueError("Unexpected XRD grid")
    return start, end, values


def _xrd_tasks(dataset, root):
    names = sorted(p.name for p in root.glob("*.xrdml"))
    scans = {name: _xrd_scan(root / name) for name in names}
    structure = {"files": [{"filename": name, "sample_count": len(values),
                             "start_2theta_deg": start, "end_2theta_deg": end}
                            for name, (start, end, values) in scans.items()]}
    stats = []
    for name, (start, end, values) in scans.items():
        index = values.index(max(values))
        stats.append({"filename": name, "max_index_zero_based": index,
                      "max_raw_intensity": values[index],
                      "angle_at_max_deg": start + index * (end - start) / (len(values) - 1)})
    return [
        _task(dataset, root, 1, "针对每个 XRDML 的第一个 scan，读取 2Theta 的 startPosition/endPosition 和原始 intensities/counts 数组长度。角度按文件头解释，files 按文件名排序。",
              '{"files":[{"filename":字符串,"sample_count":整数,"start_2theta_deg":数值,"end_2theta_deg":数值},...]}',
              structure, names),
        _task(dataset, root, 2, "针对每个文件第一个 scan，找原始 intensities/counts 的全局最大值；如并列取首个索引（从 0 起）。用含两端点的等间距 2Theta 网格计算该索引角度；不平滑、不做背景/计数时间修正。files 按文件名排序。",
              '{"files":[{"filename":字符串,"max_index_zero_based":整数,"max_raw_intensity":数值,"angle_at_max_deg":数值},...]}',
              {"files": stats}, names),
    ]


def _image_tasks(dataset, root):
    import numpy as np
    from PIL import Image
    names = sorted(p.name for p in root.iterdir() if p.suffix.lower() in (".tif", ".png"))
    structures, stats = [], []
    grayscale = dataset == "bbbc007-drosophila-cells"
    for name in names:
        with Image.open(root / name) as image:
            structures.append({"filename": name, "width": image.width, "height": image.height,
                               "channel_count": len(image.getbands())})
            values = np.asarray(image)
            if grayscale:
                if values.ndim != 2 or values.dtype != np.uint8:
                    raise ValueError("BBBC007 source representation changed")
                stats.append({"filename": name, "min": int(values.min()), "max": int(values.max()),
                              "mean": float(values.mean(dtype=np.float64))})
            else:
                if values.ndim != 3 or values.shape[2] != 4 or values.dtype != np.uint8:
                    raise ValueError("BBBC039 preview representation changed")
                rgb = values[:, :, :3]
                stats.append({"filename": name, "mean_rgb": [float(rgb[:, :, i].mean(dtype=np.float64)) for i in range(3)],
                              "nonwhite_rgb_pixel_count": int(np.any(rgb != 255, axis=2).sum())})
    instruction = ("对每张原始 8-bit 灰度图的全部像素计算 min/max/算术 mean，不重采样、不归一化。"
                   if grayscale else
                   "对每张官方 PNG 预览图，只使用 RGB 三通道、忽略 alpha；保留白边及全部像素，计算各通道原始 0–255 值的算术平均，以及 RGB 不同时等于 255 的像素数。此数不是细胞核数量，不做分割。")
    schema = ('{"files":[{"filename":字符串,"min":整数,"max":整数,"mean":数值},...]}' if grayscale else
              '{"files":[{"filename":字符串,"mean_rgb":[数值,数值,数值],"nonwhite_rgb_pixel_count":整数},...]}')
    return [
        _task(dataset, root, 1, "读取每张图片的原始宽、高和通道数（含 alpha；不将图像转换为 RGB），files 按文件名排序。",
              '{"files":[{"filename":字符串,"width":整数,"height":整数,"channel_count":整数},...]}',
              {"files": structures}, names),
        _task(dataset, root, 2, instruction + " files 按文件名排序。", schema, {"files": stats}, names),
    ]


def _fits_tasks(dataset, root):
    import numpy as np
    from astropy.io import fits
    name = next(root.glob("*.fits")).name
    with fits.open(root / name, memmap=False) as hdus:
        values = np.asarray(hdus[0].data, dtype=np.float64)
        structure = {"hdu_count": len(hdus), "primary_bitpix": int(hdus[0].header["BITPIX"]),
                     "primary_shape": list(values.shape)}
        finite = values[np.isfinite(values)]
        if not finite.size:
            raise ValueError("FITS primary array has no finite values")
        stats = {"finite_count": int(finite.size), "min": float(finite.min()),
                 "max": float(finite.max()), "mean": float(finite.mean(dtype=np.float64))}
    return [
        _task(dataset, root, 1, "报告 HDU 总数、主 HDU 的 BITPIX 和主数组 shape。shape 使用 NumPy/astropy 顺序，即 FITS 轴声明的逆序；后续表 HDU 不计入主数组。",
              '{"hdu_count":整数,"primary_bitpix":整数,"primary_shape":[整数,...]}', structure, [name]),
        _task(dataset, root, 2, "仅主 HDU 数组，按 FITS 标准应用 BSCALE/BZERO（若存在），排除非有限值；计算有限元素数、min/max 和 float64 算术 mean。不得把表扩展混入，不重采样、不更改物理量单位。",
              '{"finite_count":整数,"min":数值,"max":数值,"mean":数值}', stats, [name],
              atol=1e-22 if dataset == "nasa-hst-fos" else 1e-6),
    ]


def _netcdf_tasks(dataset, root):
    import numpy as np
    from netCDF4 import Dataset
    name = next(root.glob("*.nc")).name
    with Dataset(str(root / name)) as source:
        air = source.variables["air"]
        structure = {"air_dimensions": list(air.dimensions), "air_shape": list(air.shape), "air_units": air.units,
                     "lat_min": float(source["lat"][:].min()), "lat_max": float(source["lat"][:].max()),
                     "lon_min": float(source["lon"][:].min()), "lon_max": float(source["lon"][:].max())}
        values = np.ma.asarray(air[:], dtype=np.float64)
        values = np.ma.masked_invalid(values)
        if air.dimensions != ("time", "lat", "lon") or np.any(values.count(axis=(1, 2)) == 0):
            raise ValueError("Unexpected monthly air variable")
        stats = {"monthly_spatial_mean": [float(np.ma.mean(values[i], dtype=np.float64)) for i in range(values.shape[0])],
                 "valid_value_count": int(values.count())}
    return [
        _task(dataset, root, 1, "读取 NetCDF 的 air 变量，报告其维度名称及顺序、shape、原文件单位字符串，以及 lat/lon 坐标各自的最小/最大值。",
              '{"air_dimensions":[字符串,...],"air_shape":[整数,...],"air_units":字符串,"lat_min":数值,"lat_max":数值,"lon_min":数值,"lon_max":数值}',
              structure, [name]),
        _task(dataset, root, 2, "按原 time 索引顺序，计算每个时间切片 air 的空间算术平均：每个有效格点等权，不按纬度/面积加权；遵守缺测标记及 scale_factor/add_offset，不把单位转换为 K。另报告全部时间切片有效值总数。这是月气候态而非逐年观测。",
              '{"monthly_spatial_mean":[数值,...],"valid_value_count":整数}', stats, [name]),
    ]


def _shapefile_tasks(dataset, root):
    from osgeo import ogr
    names = sorted(p.name for p in root.iterdir() if p.suffix.lower() in (".shp", ".shx", ".dbf", ".prj", ".cpg"))
    source = ogr.Open(str(next(root.glob("*.shp"))))
    if source is None:
        raise ValueError("Could not open country shapefile")
    layer = source.GetLayer(0)
    min_x, max_x, min_y, max_y = layer.GetExtent()
    srs = layer.GetSpatialRef()
    epsg = srs.GetAuthorityCode(None)
    if epsg is None:
        raise ValueError("Country CRS has no authority code")
    structure = {"feature_count": layer.GetFeatureCount(), "epsg": int(epsg),
                 "bounds_xyxy": [min_x, min_y, max_x, max_y]}
    groups = Counter(feature.GetField("CONTINENT") for feature in layer)
    if None in groups:
        raise ValueError("Unexpected null CONTINENT")
    source = None
    return [
        _task(dataset, root, 1, "打开完整 Shapefile 配套文件，报告要素总数、原坐标系 EPSG 编号及全图层包围盒 [min_x,min_y,max_x,max_y]。保留原坐标单位，不投影/修复几何。",
              '{"feature_count":整数,"epsg":整数,"bounds_xyxy":[数值,数值,数值,数值]}', structure, names),
        _task(dataset, root, 2, "以属性表 CONTINENT 原始字符串分组计数，每个要素计一次，不按几何部分拆分。groups 按 continent 字典序；保留全部分类，不合并海洋/南极等标签。",
              '{"groups":[{"continent":字符串,"feature_count":整数},...]}',
              {"groups": [{"continent": continent, "feature_count": count} for continent, count in sorted(groups.items())]}, names),
    ]


def _pdf_tasks(dataset, root):
    from pypdf import PdfReader
    name = next(root.glob("*.pdf")).name
    pages = [page.extract_text() for page in PdfReader(root / name).pages]
    first_page = re.sub(r"\s+", "", pages[0])
    doi_match = re.search(r"10\.1371/journal\.pcbi\.\d{7}", first_page)
    if not doi_match:
        raise ValueError("Could not independently locate article DOI")
    rule_pages = []
    for index, text in enumerate(pages, 1):
        for match in re.finditer(r"(?m)^Rule\s+(\d+)\s*:", text):
            rule_pages.append({"rule": int(match[1]), "page": index})
    rule_pages.sort(key=lambda row: row["rule"])
    if [row["rule"] for row in rule_pages] != list(range(1, 11)):
        raise ValueError("Rule headings changed or extraction is incomplete")
    return [
        _task(dataset, root, 1, "读取 PDF，报告 PDF 实际总页数及该论文本身在首页列出的 DOI（不是所引用论文的 DOI）。DOI 用 10. 开头的标准字符串，不带 URL、空白或尾标点。",
              '{"page_count":整数,"article_doi":字符串}',
              {"page_count": len(pages), "article_doi": doi_match[0]}, [name]),
        _task(dataset, root, 2, "定位正文各条 Rule N: 标题的起始页（PDF 物理页从 1 编号）。只计正文标题，不计行文引用或参考文献；按 rule 整数升序列出。",
              '{"rule_start_pages":[{"rule":整数,"page":整数},...]}',
              {"rule_start_pages": rule_pages}, [name]),
    ]


def build_gold_tasks(dataset_root: str | Path) -> list[dict]:
    """Read all actual bundled source bytes and create 36 hidden gold tasks.

    Missing dependencies, missing files, and changed source structures fail
    explicitly. There are no canned answers or metadata-derived measurements.
    Source-family IDs are development labels, not a claim of unseen test data.
    """
    dataset_root = Path(dataset_root)
    tasks = []
    for dataset in DATASET_IDS:
        root = dataset_root / dataset
        if dataset.startswith("open-uci-"):
            builder = _excel_tasks if dataset == "open-uci-concrete" else _csv_tasks
        elif dataset.startswith("ncbi-"):
            builder = _fasta_tasks
        elif dataset.startswith("pdb-"):
            builder = _pdb_tasks
        elif dataset.startswith("mendeley-"):
            builder = _xrd_tasks
        elif dataset.startswith("bbbc"):
            builder = _image_tasks
        elif dataset.startswith("nasa-"):
            builder = _fits_tasks
        elif dataset.startswith("plos-"):
            builder = _pdf_tasks
        elif dataset == "open-noaa-air-climatology":
            builder = _netcdf_tasks
        elif dataset == "open-natural-earth-countries":
            builder = _shapefile_tasks
        else:
            raise ValueError("No independent oracle for dataset")
        tasks.extend(builder(dataset, root))
    # Enforce strict JSON and reject non-finite values before gold is persisted.
    json.dumps(tasks, ensure_ascii=False, allow_nan=False)
    if len(tasks) != 36 or len({task["id"] for task in tasks}) != 36:
        raise AssertionError("Development oracle catalog must have 36 unique tasks")
    return tasks


def main(argv=None):
    parser = argparse.ArgumentParser(description="Emit HIDDEN independent development gold; never provide it to agents")
    parser.add_argument("dataset_root", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(build_gold_tasks(args.dataset_root), ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
