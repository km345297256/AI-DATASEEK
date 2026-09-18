"""Offline column-only Natural Earth DBF projection with original geometry.

Only already reviewed repository originals are read. No network, database,
source scripts, geometry synthesis, imputation or coordinate conversion occurs.
The original 168-field DBF and every bundled sidecar remain in original/.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct

FIELDS = ("NAME_EN", "ISO_A3", "SOV_A3", "CONTINENT", "POP_EST", "POP_YEAR", "GDP_MD", "GDP_YEAR")
PINNED_DBF = "1fee677cd4e03b367876e03861eb10197e4022a846bf92060e0313432863785b"
DATASET = "viz-test-dbf-natural-earth"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def project_dbf(raw):
    if digest(raw) != PINNED_DBF:
        raise ValueError("Not the reviewed Natural Earth DBF snapshot")
    count, header_size, record_size = struct.unpack_from("<IHH", raw, 4)
    assert raw[0] == 3 and raw[29] == 0 and count == 177
    assert raw[header_size-1] == 13 and len(raw) == header_size + count*record_size + 1 and raw[-1] == 26
    definitions, offset = {}, 1
    for index in range(32, header_size-1, 32):
        definition = raw[index:index+32]
        name = definition[:11].split(b"\0", 1)[0].decode("ascii")
        definitions[name] = (definition, offset, definition[16])
        offset += definition[16]
    assert offset == record_size and len(definitions) == 168
    chosen = [definitions[name] for name in FIELDS]
    target_width = 1 + sum(width for _, _, width in chosen)
    target_header = bytearray(raw[:32])
    struct.pack_into("<HH", target_header, 8, 33+32*len(FIELDS), target_width)
    out = bytearray(target_header)
    for definition, _, _ in chosen:
        assert definition[18:] == bytes(14)
        out.extend(definition)
    out.extend(b"\r")
    padding_cells = 0
    for i in range(count):
        record = raw[header_size+i*record_size:header_size+(i+1)*record_size]
        assert record[:1] == b" "
        out.extend(record[:1])
        for definition, start, width in chosen:
            cell = record[start:start+width]
            if definition[11] == ord("C"):
                value = cell.rstrip(b" \0")
                assert b"\0" not in value
                value.decode("ascii")  # Selected names/codes are all ASCII.
                padding_cells += b"\0" in cell
                normalized = value.ljust(width, b" ")
                assert normalized.rstrip(b" ") == cell.rstrip(b" \0")
                out.extend(normalized)
            else:
                assert definition[11] == ord("N")
                cell.decode("ascii")
                out.extend(cell)  # Preserve exact decimal text, including -99.
    out.extend(b"\x1a")
    assert len(out) == 33+32*len(FIELDS)+count*target_width+1
    return bytes(out), padding_cells


def build(source, staging):
    if source.resolve() != source or staging.resolve() != staging or not source.is_dir():
        raise ValueError("Explicit nonsymlink source and staging paths required")
    target = staging / DATASET
    if target.exists():
        raise ValueError("Use a new staging dataset directory; no overwrites")
    manifest = json.loads((source / "manifest.json").read_text())
    provenance = {f["path"]: f for f in manifest["metadata"]["provenance"]}
    originals = {}
    for spec in manifest["files"]:
        name = spec["path"]
        assert Path(name).name == name
        p = source / name
        assert p.resolve() == p
        raw = p.read_bytes()
        assert len(raw) == spec["size"] and digest(raw) == spec["sha256"]
        originals[name] = raw
    dbf, padding = project_dbf(originals["ne_110m_admin_0_countries.dbf"])
    target.mkdir(parents=True)
    files = []

    def put(name, raw, **attributes):
        p = target / name
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("xb") as stream:
            stream.write(raw)
        files.append({"path": name, "size": len(raw), "sha256": digest(raw), "role": "data", **attributes})

    for name, raw in originals.items():
        put("original/"+name, raw, url=provenance[name]["download_url"], archive_member=name,
            role="documentation" if name.endswith(".txt") else "data")
    for extension in ("shp", "shx", "prj", "cpg"):
        original = "ne_110m_admin_0_countries."+extension
        put("countries."+extension, originals[original], derived_from=["original/"+original])
    put("countries.dbf", dbf, derived_from=["original/ne_110m_admin_0_countries.dbf"])
    generator = "backend/scripts/plugin_samples_natural_earth_dbf.py"
    generator_sha = digest(Path(__file__).read_bytes())
    params = {"generator": generator, "generator_sha256": generator_sha, "source_dbf_sha256": PINNED_DBF,
              "features": 177, "source_columns": 168, "selected_columns": list(FIELDS),
              "character_padding": "Trailing NUL/space padding normalized to DBF spaces; decoded text unchanged",
              "character_cells_with_nul_padding_normalized": padding,
              "numeric_fields": "Exact raw decimal bytes unchanged, including missing-value sentinels",
              "geometries": "SHP, SHX, PRJ and CPG copied byte-for-byte in the same record order",
              "area": "Source has no area column; no area was calculated or synthesized"}
    put("CONVERSION.json", (json.dumps(params, ensure_ascii=False, indent=2)+"\n").encode(),
        derived_from=["original/ne_110m_admin_0_countries.dbf"], role="documentation")
    metadata = manifest["metadata"]
    steps = ["打开 countries.dbf；同目录完整 SHP/SHX/PRJ/CPG 使默认地图预览可以先打开。",
             "在可视化选择器选择 viz-dbf-table，点击读取数据库分页，核对 8 列、177 行国家/地区属性。",
             "切换下一页；POP_EST/POP_YEAR/GDP_MD/GDP_YEAR 保留来源值和缺失哨兵，不推定当前年份。",
             "需要完整168字段时查看 original/；小比例尺边界不用于精确面积或法律边界认定。"]
    item = {"dataset_id": DATASET, "name": "插件测试 · DBF 国家属性表与原始边界",
            "description": "Natural Earth 1:110m 177个国家/地区要素；保留所有原始文件，另提供8字段DBF与字节一致的几何配套。只做字段投影和字符尾部填充规范化，不改观测或制图数值。",
            "domain": "geoscience", "view_kind": "table", "plugin_id": "viz-dbf-table", "entry_file": "countries.dbf",
            "source_url": metadata["source_url"], "publisher": metadata["publisher"],
            "license": "Public domain", "license_url": metadata["license_url"],
            "license_details": "Natural Earth 官方条款声明其栅格和矢量地图数据均属公有领域，允许修改和传播。保留作者与上游出处，不宣称这些边界具有法律效力。",
            "authors": ["Tom Patterson", "Nathaniel Vaughn Kelso", "Natural Earth contributors"],
            "source_version": originals["ne_110m_admin_0_countries.VERSION.txt"].decode().strip(),
            "sample_scope": "完整177要素及其原始几何；DBF从168列选8列，英文名称、ISO/主权代码、洲、人口/GDP及各自年份；源没有面积字段，不合成面积。",
            "sample_kind": "scientific_derived", "test_steps": steps, "files": files,
            "transformation": generator+" (SHA256 "+generator_sha+")：按原记录顺序选 "+", ".join(FIELDS)+"；C字段仅将尾部NUL填充换为空格，解码文本不变；N字段原始字节不变。SHP/SHX/PRJ/CPG原样复制。CONVERSION.json记录参数，original/保留全部源件。"}
    mapping = {"plugin_id": "viz-dbf-table", "dataset_id": DATASET, "entry_file": "countries.dbf",
               "sample_kind": "scientific_derived", "test_steps": steps,
               "expected": "完整177行、8列；同名SHP配套保证默认地图可打开，再切DBF表格；所有原数值和坐标未修改。",
               "expected_file_sha256": digest(dbf)}
    for filename, value in (("natural-earth-dbf-descriptor.json", [item]), ("natural-earth-dbf-mapping.json", [mapping])):
        with (staging / filename).open("x") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    return {"dataset_id": DATASET, "rows": 177, "columns": 8, "files": len(files), "bytes": sum(f["size"] for f in files), "dbf_bytes": len(dbf), "padding_cells_normalized": padding}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--staging", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.staging)))
