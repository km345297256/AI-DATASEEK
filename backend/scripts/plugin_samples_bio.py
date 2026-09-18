"""Reproducible offline bio/imagery plugin examples; never fetch or register.

Run only against an explicit disposable staging directory after separately
reviewing public source files. Scientific conversions preserve originals;
new numerical phantoms are explicitly synthetic, not clinical/observational.
Dependencies are the already installed sandbox packages. No downloaded code,
notebook, macro, model API, database, or network is executed by this script.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import wave

REPOSITORY = Path(__file__).resolve().parents[2]
GENERATOR = "backend/scripts/plugin_samples_bio.py"
PROJECT_URL = "https://github.com/km345297256/AI-DATASEEK"
PROJECT_LICENSE = PROJECT_URL + "/blob/v2/LICENSE"
DATE = "2026-09-18"


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError("Refusing to replace a changed staged artifact: " + path.name)
        return
    with path.open("xb") as stream:
        stream.write(data)


def generated_origin(description):
    return {"kind": "generated_fixture", "generator": GENERATOR,
            "generator_sha256": digest(Path(__file__)), "description": description}


def declared(directory, relative, *, url=None, parents=None, role="data", description=None):
    path = directory / relative
    result = {"path": relative, "size": path.stat().st_size, "sha256": digest(path), "role": role}
    if url:
        result["url"] = url
    elif parents:
        result["derived_from"] = parents
    else:
        result["origin"] = generated_origin(description or "明确标注的原创功能测试数据，并非真实观测。")
    return result


def generation_spec(directory, values):
    put(directory / "GENERATION_SPEC.json", encode(values))
    return declared(directory, "GENERATION_SPEC.json", role="documentation",
                    description="数据生成/格式转换参数记录；不是上游下载文件。")


def project_descriptor(identifier, name, description, domain, kind, plugin, entry, scope, steps):
    return {"dataset_id": identifier, "name": name, "description": description,
            "domain": domain, "view_kind": kind, "plugin_id": plugin, "entry_file": entry,
            "source_url": PROJECT_URL, "publisher": "AI-DataSeek 原创功能测试样例",
            "license": "MIT (project-generated fixture)", "license_url": PROJECT_LICENSE,
            "license_details": "本项目原创算法生成的功能样例，按项目 MIT 许可提供；不是来自医院、受试者或真实实验的观测。保留随附项目 LICENSE.txt。",
            "authors": ["AI-DataSeek contributors"], "source_version": "original deterministic fixture " + DATE,
            "sample_scope": scope, "sample_kind": "project_fixture", "test_steps": steps}


def project_files(directory, names, spec):
    put(directory / "LICENSE.txt", (REPOSITORY / "LICENSE").read_bytes())
    return [declared(directory, name, description=spec) for name in names] + [
        declared(directory, "LICENSE.txt", role="documentation", description="项目 MIT 许可证原文的本地副本。")]


def build(root):
    import numpy as np
    from PIL import Image
    import h5py
    import pysam
    from pylibCZIrw import czi

    sys.path.insert(0, str(REPOSITORY / "sandbox"))
    sys.path.insert(0, str(REPOSITORY / "sandbox/tests"))
    from dicom_window_fixtures import fixture as dicom_fixture
    from alignment_browser_fixtures import records as alignment_records
    from fcs_window_fixtures import fcs_bytes
    from app.services.fcs_window_reader import _pairs as fcs_pairs

    items, mapping = [], []

    def register(item, coverage):
        items.append(item)
        for plugin, entry, steps in coverage:
            mapping.append({"plugin_id": plugin, "dataset_id": item["dataset_id"],
                            "entry_file": entry, "sample_kind": item["sample_kind"], "test_steps": steps})

    reused = {item["dataset_id"]: item for item in json.loads((root / "sources/reuse-descriptors.json").read_text())}
    item = copy.deepcopy(reused["viz-lightmycells-nucleus"])
    item.update(dataset_id="viz-bio-lightmycells-formats", name="真实犬细胞核图像：七种影像格式与阅读器对照",
                sample_kind="scientific_derived", plugin_id="viz-viv")
    directory = root / item["dataset_id"]
    original = item["entry_file"]
    assert digest(directory / original) == item["files"][0]["sha256"]
    image = Image.open(directory / original)
    pixels = np.asarray(image, dtype="<u2")
    assert pixels.shape == (1200, 1200)
    out = io.BytesIO(); Image.fromarray(pixels).save(out, format="PNG")
    put(directory / "nucleus-uint16.png", out.getvalue())
    out = io.BytesIO(); np.save(out, pixels, allow_pickle=False)
    put(directory / "nucleus-uint16.npy", out.getvalue())
    czi_path = directory / "nucleus-uncompressed.czi"
    if not czi_path.exists():
        with czi.create_czi(str(czi_path)) as writer:
            writer.write(pixels[:, :, None], plane={"C": 0, "Z": 0, "T": 0}, scene=0)
    zarr_files = {
        "nucleus.zarr/.zgroup": encode({"zarr_format": 2}),
        "nucleus.zarr/.zattrs": encode({"multiscales": [{"version": "0.4",
            "axes": [{"name": "y", "type": "space"}, {"name": "x", "type": "space"}],
            "datasets": [{"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [1, 1]}]}]}]}),
        "nucleus.zarr/0/.zarray": encode({"zarr_format": 2, "shape": [1200, 1200],
            "chunks": [1200, 1200], "dtype": "<u2", "compressor": None, "fill_value": 0,
            "order": "C", "filters": None, "dimension_separator": "."}),
        "nucleus.zarr/0/0.0": pixels.tobytes(order="C"),
    }
    for name, data in zarr_files.items():
        put(directory / name, data)
    conversion_names = ["nucleus-uint16.png", "nucleus-uint16.npy", "nucleus-uncompressed.czi", *zarr_files]
    item["files"] += [declared(directory, name, parents=[original]) for name in conversion_names]
    item["files"].append(generation_spec(directory, {
        "kind": "scientific_format_conversion", "original": original, "array_shape_yx": [1200, 1200],
        "dtype": "uint16", "pixel_values_changed": False, "resampled": False,
        "czi": "single scene/channel/Z/time; uncompressed; quantitative pixels preserved",
        "ome_zarr": "NGFF 0.4 / Zarr v2, one uncompressed full-image chunk, pixel-index scale; no physical calibration inferred",
        "outputs": conversion_names,
    }))
    item["description"] = "来自 Light My Cells 的真实犬细胞核显微观察，原始 OME-TIFF 完整保留；额外提供保留全部 uint16 像素的 PNG、NPY、未压缩 CZI 和 OME-Zarr 格式。用于比较阅读器结果，不是七次独立实验。"
    item["transformation"] = "只转换容器，不裁剪、不重采样、不修改1200×1200 uint16像素。OME-Zarr采用像素索引坐标，不伪造物理标定；原OME元数据保留于原件。CZI由已安装官方pylibCZIrw写入器生成。"
    item["license_details"] += " 本轮产生容器转换文件，已明确记录改动，原文件保留。"
    item["test_steps"] = ["打开原始 OME-TIFF 并选择 Viv；确认单通道1200×1200。", "比较同集 PNG、NPY、CZI 与 OME-Zarr 的相同 ROI；数值未修改。"]
    register(item, [(plugin, entry, [step]) for plugin, entry, step in [
        ("image", "nucleus-uint16.png", "打开PNG，确认细胞核影像可见；16位PNG仅作显示，不以显示亮度代替原始强度。"),
        ("tiff", original, "选择TIFF阅读器，确认原图1200×1200可解码。"),
        ("viz-viv", original, "选择Viv，显示单通道原始OME-TIFF。"),
        ("viz-czi", "nucleus-uncompressed.czi", "打开CZI，选择C/Z/T均0和128×128 ROI；比较原图。"),
        ("viz-czi-window", "nucleus-uncompressed.czi", "选择CZI区域阅读器，C/Z/T均0，从[0,0,128,128]开始。"),
        ("viz-ome-zarr", "nucleus.zarr/.zattrs", "打开同目录.zattrs，level=0、indices=[]、ROI=[0,0,128,128]，保持全部目录文件。"),
        ("viz-matrix-workbench", "nucleus-uint16.npy", "选择矩阵工作台，确认二维uint16矩阵与原始像素相同；不启用pickle。"),
    ]])

    item = copy.deepcopy(reused["viz-elife-ftsz-timelapse"])
    item.update(dataset_id="viz-bio-elife-video", sample_kind="scientific_original",
                test_steps=["打开MP4并选择视频插件，播放并拖动进度条。", "约11.75秒播放时间不等于生物学实验时间，不据此计算细胞分裂速率。"])
    register(item, [("viz-video-player", item["entry_file"], item["test_steps"])])

    identifier = "viz-bio-dicom-phantom"
    directory = root / identifier
    values = [int(600 + 300 * (x / 63) + 100 * (y / 63)) for y in range(64) for x in range(64)]
    data, _ = dicom_fixture(signed=True, rows=64, columns=64, pixel_values=values,
        drop=(0x00100010, 0x00100020, 0x00281054),
        overrides={0x00281052: (b"DS", "0"), 0x00281053: (b"DS", "1"),
                   0x00281050: (b"DS", "800"), 0x00281051: (b"DS", "500")})
    put(directory / "synthetic-phantom.dcm", data)
    scope = "原创64×64二维数值梯度；没有病人、医院采集或真实CT；DICOM去标识标记用于演示安全读取门槛，不能证明任何其他文件匿名。"
    item = project_descriptor(identifier, "原创DICOM数值体模（非临床）", scope, "image_science", "image", "viz-dicom-window", "synthetic-phantom.dcm", scope,
        ["打开DICOM元数据，确认单帧64×64。", "仅对本明确合成文件勾选已去标识确认，再选择frame=0与ROI=[0,0,64,64]；不是诊断影像。"])
    item["files"] = project_files(directory, [item["entry_file"]], scope)
    item["files"].append(generation_spec(directory, {"synthetic": True, "shape": [1,64,64], "formula": "int(600+300*x/63+100*y/63)", "patient_data": False, "units": "arbitrary"}))
    register(item, [(item["plugin_id"], item["entry_file"], item["test_steps"])])

    identifier = "viz-bio-niivue-phantom"
    directory = root / identifier
    zz, yy, xx = np.indices((48,48,48)); radius = ((xx-23.5)**2 + (yy-23.5)**2 + (zz-23.5)**2)**0.5
    volume = np.where(radius < 18, np.maximum(0, (18-radius)*100), 0).astype("<u2")
    header = b"NRRD0005\ntype: ushort\ndimension: 3\nsizes: 48 48 48\nencoding: raw\nendian: little\nspacings: 1 1 1\n\n"
    put(directory / "synthetic-radial-volume.nrrd", header + volume.tobytes())
    scope = "48×48×48原创径向球体数组，单位是任意数值/体素索引，不是MRI、CT或真实组织。"
    item = project_descriptor(identifier, "原创三维球体体模：NiiVue正交切片", scope, "image_science", "image", "viz-niivue", "synthetic-radial-volume.nrrd", scope,
        ["打开内嵌raw NRRD，选择NiiVue。", "切换正交切片与体渲染；中心强度高、外围为0，确认轴向一致；不要作医学解释。"])
    item["files"] = project_files(directory, [item["entry_file"]], scope)
    item["files"].append(generation_spec(directory, {"synthetic": True, "shape_zyx": [48,48,48], "dtype": "uint16", "formula": "max(0,(18-distance_to_23.5_center)*100), truncated to uint16", "units": "arbitrary"}))
    register(item, [(item["plugin_id"], item["entry_file"], item["test_steps"])])

    identifier = "viz-bio-spatial-phantom"
    directory = root / identifier
    grid_y, grid_x = np.indices((16,16)); coords = np.column_stack((grid_x.ravel(), grid_y.ravel())).astype("<f8")
    matrix = np.column_stack((coords[:,0], coords[:,1], coords[:,0]+coords[:,1])).astype("<f4")
    out = io.BytesIO()
    with h5py.File(out, "w") as handle:
        def encoded(obj, encoding, version):
            obj.attrs.update({"encoding-type": encoding, "encoding-version": version})
        encoded(handle, "anndata", "0.1.0")
        for name, count in (("obs",256),("var",3)):
            group = handle.create_group(name); encoded(group,"dataframe","0.2.0")
            group.attrs["_index"] = "_index"
            group.attrs["column-order"] = np.array([],dtype=h5py.string_dtype())
            encoded(group.create_dataset("_index", data=np.array(["synthetic-"+str(i) for i in range(count)],dtype=object),dtype=h5py.string_dtype()),"string-array","0.2.0")
        group = handle.create_group("obsm"); encoded(group,"dict","0.1.0")
        encoded(group.create_dataset("spatial",data=coords),"array","0.2.0")
        encoded(handle.create_dataset("X",data=matrix),"array","0.2.0")
    put(directory / "synthetic-spatial.h5ad", out.getvalue())
    scope = "256个规则网格位置及3个人工数值特征[x,y,x+y]，没有真实基因/细胞/受试者；用于AnnData空间窗口与坐标数值一致性检查。"
    item = project_descriptor(identifier, "原创AnnData空间网格（非转录组观测）", scope, "image_science", "map", "viz-spatial-window", "synthetic-spatial.h5ad", scope,
        ["选择空间窗口，feature=2、observation_start=0、observation_count=256、decode=raw。", "核对右上方向梯度以及每个点value=x+y；没有基因表达生物学含义。"])
    item["files"] = project_files(directory, [item["entry_file"]], scope)
    item["files"].append(generation_spec(directory, {"synthetic": True,"observations":256,"coordinates":"16x16 integer grid","features":["x","y","x+y"],"normalization":None,"biological_labels":False}))
    register(item, [(item["plugin_id"], item["entry_file"], item["test_steps"])])

    identifier = "viz-bio-alignments-and-tracks"
    directory = root / identifier
    header, records = alignment_records({"HD":{"VN":"1.6","SO":"coordinate"},"SQ":[{"SN":"chr_demo","LN":5000}],"RG":[{"ID":"rg1","SM":"synthetic"}]})
    sam = str(header) + "".join(record.to_string()+"\n" for record in records)
    put(directory / "synthetic-alignments.sam", sam.encode())
    put(directory / "synthetic-features.bed", b"chr_demo\t100\t160\tfeature_A\t100\t+\nchr_demo\t140\t210\tfeature_B\t200\t-\nchr_demo\t300\t420\tfeature_C\t300\t+\n")
    put(directory / "demo.chrom.sizes", b"chr_demo\t5000\n")
    put(directory / "synthetic-hits.blast6", b"query_demo\tsubject_forward\t99\t100\t1\t0\t1\t100\t20\t119\t1e-20\t150\nquery_demo\tsubject_reverse\t80\t100\t20\t0\t301\t400\t900\t801\t3e-12\t80\n")
    scope = "原创合成chr_demo（长度5000）、5条SAM比对、3条BED特征和2条BLAST12列记录；不是人类/动物个体测序，不是实际BLAST运行结果。"
    item = project_descriptor(identifier, "原创基因组阅读器功能样例：SAM/BED/BLAST", scope, "sequence", "map", "viz-alignment-browser", "synthetic-alignments.sam", scope,
        ["SAM选择reference=0,start=100,end=180，观察匹配、插入、缺失与成对关系。", "BED选择chr_demo区间0–500；IGV需要粘贴demo.chrom.sizes正文chr_demo TAB 5000。", "BLAST文件有正向与反向命中，数值仅为功能测试；不推断真实同源关系。"])
    item["files"] = project_files(directory, ["synthetic-alignments.sam","synthetic-features.bed","demo.chrom.sizes","synthetic-hits.blast6"], scope)
    item["files"].append(generation_spec(directory, {"synthetic": True,"reference":{"chr_demo":5000},"sam_records":5,"bed_records":3,"blast_records":2,"clinical_or_individual_genetic_data":False}))
    register(item, [("viz-alignment-browser","synthetic-alignments.sam",[item["test_steps"][0]]),
                    ("viz-genome-tracks","synthetic-features.bed",["染色体0，start=0，end=500；检查三条BED区间。"]),
                    ("viz-igv","synthetic-features.bed",["参考文本填写chr_demo TAB 5000（来自demo.chrom.sizes），确认后本地浏览三条BED特征；不自动联网加载参考。"]),
                    ("viz-blast-hits","synthetic-hits.blast6",[item["test_steps"][2]])])

    identifier = "viz-bio-biopython-fastq"
    directory = root / identifier
    base = "https://raw.githubusercontent.com/biopython/biopython/5bbc6c12c505301f2d681f932c30fdb8fcbe9a6e/"
    item = {"dataset_id":identifier,"name":"Biopython官方FASTQ最小测试样例","description":"Biopython Tests/Quality/example.fastq原样文件，3条短序列。用于读取与质量面板功能测试；不是代表性测序研究，不推断样本身份或实验结果。",
        "domain":"sequence","view_kind":"series","plugin_id":"fastq-quality","entry_file":"example.fastq",
        "publisher":"Biopython contributors","source_url":"https://github.com/biopython/biopython/tree/5bbc6c12c505301f2d681f932c30fdb8fcbe9a6e/Tests/Quality",
        "license":"Biopython License Agreement","license_url":base+"LICENSE.rst",
        "license_details":"固定提交LICENSE.rst明确所有文件除单独声明外按Biopython License Agreement提供；该FASTQ无例外头。保留许可正文；不将库代码许可证推论为另一个未收录的科学数据许可。",
        "authors":["Biopython contributors"],"source_version":"Git commit 5bbc6c12c505301f2d681f932c30fdb8fcbe9a6e","sample_kind":"official_fixture",
        "sample_scope":"官方仓库完整234字节功能测试文件，共3条23/24bp左右短读段；不用于群体统计或临床结论。",
        "test_steps":["质量概览确认3条完整读段；按Phred+33解释。","序列浏览器选择record=0、start=1、count=23、quality_encoding=phred33。","FastQC应完成解析生成报告；小样本的WARN/FAIL是质量评价，不等于工具失败。"],
        "files":[declared(directory,"example.fastq",url=base+"Tests/Quality/example.fastq"),declared(directory,"LICENSE.rst",url=base+"LICENSE.rst",role="documentation")]}
    register(item, [("fastq-quality","example.fastq",[item["test_steps"][0]]),
                    ("viz-sequence-browser","example.fastq",[item["test_steps"][1]]),
                    ("viz-fastqc","example.fastq",[item["test_steps"][2]])])

    identifier = "viz-bio-flowcal-fcs"
    directory = root / identifier
    base = "https://raw.githubusercontent.com/taborlab/FlowCal/0212f417a53b2fc76658d83b751b892f2d05f175/"
    raw_fcs = (directory / "sample001.fcs").read_bytes()
    tb,te,db,de = [int(raw_fcs[start:start+8]) for start in (10,18,26,34)]
    source_fields = fcs_pairs(raw_fcs[tb:te+1],"3.0")
    assert source_fields["$DATATYPE"] == "I" and source_fields["$BYTEORD"] in {"1,2,3,4","4,3,2,1"}
    original_endian = "little" if source_fields["$BYTEORD"] == "1,2,3,4" else "big"
    count, channels = int(source_fields["$TOT"]),int(source_fields["$PAR"])
    assert all(source_fields[f"$P{i}B"] == "24" for i in range(1,channels+1))
    packed = np.frombuffer(raw_fcs[db:de+1],dtype=np.uint8).reshape(count,channels,3).astype(np.uint32)
    if original_endian == "big":
        packed = packed[:,:,::-1]
    integers = packed[:,:,0] | packed[:,:,1] << 8 | packed[:,:,2] << 16
    # No instrument bit masking, amplifier exponent, compensation or calibration.
    # 24-bit integers have exact IEEE float64 representations.
    assert int(integers[0,0]) == int.from_bytes(raw_fcs[db:db+3],original_endian)
    converted = fcs_bytes(integers.astype(np.float64).tolist(),datatype="D",
        names=[source_fields[f"$P{i}N"] for i in range(1,channels+1)],
        metadata={f"$P{i}R":source_fields[f"$P{i}R"] for i in range(1,channels+1)})
    put(directory / "sample001-digital-values.fcs",converted)
    item = {"dataset_id":identifier,"name":"FlowCal官方流式细胞示例FCS","description":"FlowCal官方examples/FCFiles/sample001.fcs原样示例，用于FCS事件和通道窗口浏览。示例不是临床病人数据；不猜测通道阈值、标定或未发布的实验设计。",
        "domain":"image_science","view_kind":"series","plugin_id":"viz-fcs-window","entry_file":"sample001-digital-values.fcs",
        "publisher":"Rice University Tabor Lab / FlowCal","source_url":"https://flowcal.readthedocs.io/en/latest/python_tutorial/read.html",
        "license":"MIT (official FlowCal bundled example)","license_url":base+"license.txt",
        "license_details":"官方MIT仓库随软件分发的FCFiles示例，保留license.txt原文及原始例子路径；此处按官方功能样例收录，不声称所有外部流式实验数据均受MIT覆盖。",
        "authors":["John Sexton","Brian Landry","Sebastian Castillo-Hair"],"source_version":"Git commit 0212f417a53b2fc76658d83b751b892f2d05f175","sample_kind":"official_fixture",
        "sample_scope":"完整FlowCal官方FCS3.0原件保留。当前阅读器不支持24位整数，因此提供全33024事件×8通道的float64原始数字值容器；原事件顺序和数字值不变。派生文件的零指数只声明float存储，不能用作原仪器线性化校准。",
        "transformation":"源FCS每值3字节uint24按原$BYTEORD逐值无损变成float64 FCS3.1，保留事件顺序/通道名/原量程；$PnE设0,0是浮点存储约束，原指数在GENERATION_SPEC及源FCS保留。未应用源指数/增益、补偿、门控、掩码、标定或物理单位换算。派生文件只代表原始编码数字值，不是MESF或线性荧光强度。",
        "test_steps":["打开sample001-digital-values.fcs而非24-bit原件，检查33024事件和8通道。","选择scatter、channels=[0,1]、event_offset=0、event_count=256；显示原始数字值，不能自动作原仪器线性化或MESF解释。"],
        "files":[declared(directory,"sample001.fcs",url=base+"examples/FCFiles/sample001.fcs"),
                 declared(directory,"sample001-digital-values.fcs",parents=["sample001.fcs"]),
                 declared(directory,"LICENSE.txt",url=base+"license.txt",role="documentation")]}
    item["description"] = "FlowCal官方完整FCS原件与兼容转换：24-bit原始数字事件无损保存为float64 FCS3.1，用于当前窗口插件。33024事件×8通道；不补偿、不门控、不应用原仪器指数/增益、不转换为物理强度。"
    item["files"].append(generation_spec(directory,{"kind":"official_fixture_container_conversion","source_bits":24,"source_byte_order":original_endian,"output_bits":64,"output_datatype":"D","events":count,"channels":channels,"original_channel_encoding":[{key:source_fields.get(f"$P{i}{key}") for key in ("B","R","E","G","N")} for i in range(1,channels+1)],"original_integers_preserved":True,"amplifier_transform_applied":False,"compensation_applied":False,"gating_applied":False}))
    register(item,[(item["plugin_id"],item["entry_file"],item["test_steps"])])

    identifier = "viz-bio-physionet-eeg"
    directory = root / identifier
    edf = (directory / "S001R01.edf").read_bytes()
    assert edf[:8] == b"0       "
    ns = int(edf[252:256]); header_bytes = int(edf[184:192]); records_count = int(edf[236:244]); duration = float(edf[244:252])
    samples = [int(edf[256+216*ns+8*i:256+216*ns+8*(i+1)]) for i in range(ns)]
    stride = sum(samples)*2
    native = b"".join(edf[header_bytes+r*stride:header_bytes+r*stride+samples[0]*2] for r in range(records_count))
    # Explicit auditory time scaling, not acquisition or invented acoustic data.
    out = io.BytesIO()
    with wave.open(out,"wb") as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(8000);audio.writeframes(native)
    put(directory / "eeg-channel0-sonification.wav",out.getvalue())
    item = {"dataset_id":identifier,"name":"PhysioNet公开EEG基线与明确标注的声化转换","description":"EEG Motor Movement/Imagery v1.0.0的S001R01睁眼静息基线EDF+；原64导EEG与注释保留。附加WAV只是第0通道原始整数样本以8000Hz重解释的声化，不是现场声音或临床诊断。",
        "domain":"general","view_kind":"series","plugin_id":"viz-edf-signals","entry_file":"S001R01.edf",
        "publisher":"PhysioNet / BCI R&D Program, Wadsworth Center","source_url":"https://physionet.org/content/eegmmidb/1.0.0/",
        "license":"Open Data Commons Attribution License v1.0","license_url":"https://physionet.org/content/eegmmidb/view-license/1.0.0/",
        "license_details":"官方数据集明确Open Access，并将files许可指定为Open Data Commons Attribution License v1.0。引用Gerwin Schalk (2009), DOI 10.13026/C28G6P及BCI2000论文；标注WAV转换。未获取受控数据或额外身份资料。",
        "authors":["Gerwin Schalk"],"doi":"10.13026/C28G6P","source_version":"PhysioNet eegmmidb 1.0.0","sample_kind":"scientific_derived",
        "sample_scope":"完整单条公开S001R01基线记录；并非109人全部数据。WAV仅通道0，数字样本不放大，以8000Hz播放，原始采样率160Hz、时间压缩50倍。不能用WAV播放时间当作实验时间。",
        "transformation":"原EDF+不变。按EDF记录布局提取通道0有符号little-endian 16位数字样本，未重采样/插值/滤波/放大；WAV采样率声明8000而原EEG160，用于50倍时间声化。WAV不是真实声学观测，低幅度可能较安静。",
        "test_steps":["EDF选择通道0与前10秒，检查160Hz与物理单位；不要把注释通道作普通信号。","WAV选择音频波形/播放；约1.22秒对应约61秒EEG，不能作原始声音或诊断解释。"],
        "files":[declared(directory,"S001R01.edf",url="https://physionet.org/files/eegmmidb/1.0.0/S001/S001R01.edf"),
                 declared(directory,"eeg-channel0-sonification.wav",parents=["S001R01.edf"])]}
    item["files"].append(generation_spec(directory,{"scientific_transformation":"sonification","channel":0,"original_sample_rate_hz":samples[0]/duration,"wav_sample_rate_hz":8000,"sample_count":len(native)//2,"resampled":False,"digital_values_changed":False,"clinical_interpretation":False}))
    register(item,[("viz-edf-signals","S001R01.edf",[item["test_steps"][0]]),("viz-audio-waveform","eeg-channel0-sonification.wav",[item["test_steps"][1]])])

    (root.parent / "bio-descriptors.json").write_bytes(encode(items))
    (root.parent / "bio-mapping.json").write_bytes(encode(mapping))
    print(json.dumps({"datasets":len(items),"plugins":len(mapping),"declared_bytes":sum(f["size"] for item in items for f in item["files"])}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.staging_root
    if not root.is_absolute() or root.resolve() != root or not root.is_dir() or root == Path("/"):
        parser.error("staging-root must be an existing specific non-symlink directory")
    build(root)
