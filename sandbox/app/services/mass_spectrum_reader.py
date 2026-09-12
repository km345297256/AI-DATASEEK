"""Bounded MGF peak lists and mzML 1.1 inline numeric spectra, without I/O."""
import base64
import io
import math
import re
import struct
import zlib

from .mass_spectrum_payload import (
    FORMATS, MAX_INPUT, MAX_OUTPUT, MAX_POINTS, MAX_SPECTRA, PAGE, WARNING,
    MassSpectrumError, require, spectrum_index, validate_mass_spectrum_options,
    validate_mass_spectrum_payload,
)

NS = "{http://psi.hupo.org/ms/mzml}"
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?\Z")


def numeric(text, minimum=-1e308, maximum=1e308):
    require(type(text) is str and len(text) <= 40 and NUMBER.fullmatch(text) is not None)
    value = float(text)
    require(math.isfinite(value) and minimum <= value <= maximum)
    # Do not turn nonzero underflow into a scientific zero.
    require(value != 0 or not any(c in "123456789" for c in re.split("[eE]", text)[0]))
    return value


def natural(text, maximum=2**31 - 1):
    require(type(text) is str and re.fullmatch(r"[0-9]{1,10}", text) is not None)
    value = int(text)
    require(value <= maximum)
    return value


def blank(index):
    return {"id": f"s-{index:06d}", "index": index, "points": 0, "representation": "unknown",
            "mz_unit": None, "intensity_unit": None, "retention_time": None, "time_unit": None,
            "precursor_mz": None, "ms_level": None, "selectable": False, "reason": "supported"}


def finish(item):
    if item["points"] == 0: item["reason"] = "empty-spectrum"
    elif item["points"] > MAX_POINTS: item["reason"] = "point-budget"
    item["selectable"] = item["reason"] == "supported"
    return item


def mgf(data, selected):
    text = data.decode("ascii")
    require("\x00" not in text)
    spectra, values, item, parameters = [], [], None, {}
    metadata_bytes, lines, seen_peak = 0, 0, False
    for raw in io.StringIO(text):
        lines += 1
        require(lines <= 500000 and len(raw) <= 4096)
        line = raw.strip()
        if not line: continue
        if line == "BEGIN IONS":
            require(item is None and len(spectra) < MAX_SPECTRA, "MGF 谱边界重复或谱目录超限。")
            item, parameters, seen_peak = blank(len(spectra)), {}, False
            item.update(representation="centroid", ms_level=2)
        elif line == "END IONS":
            require(item is not None)
            spectra.append(finish(item)); item = None
        elif item is None:
            require(line[0] in "#;!/" or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}=.*", line) is not None,
                    "首版 MGF 只支持 BEGIN IONS / END IONS 两列 MS/MS 峰表。")
            metadata_bytes += len(raw)
        elif "=" in line:
            require(not seen_peak)
            key, value = line.split("=", 1)
            require(re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key) is not None and key not in parameters,
                    "MGF 重复或混合前体参数不在本次受限方言内。")
            parameters[key] = True; metadata_bytes += len(raw)
            if key == "PEPMASS":
                parts = value.split()
                require(1 <= len(parts) <= 3)
                item["precursor_mz"] = numeric(parts[0], 0)
                if len(parts) > 1: numeric(parts[1])
                if len(parts) > 2: require(re.fullmatch(r"[0-9]{1,2}[+-]", parts[2]) is not None)
            elif key == "RTINSECONDS":
                # A time interval is not a single retention time.
                item["retention_time"] = numeric(value, 0, 1e12); item["time_unit"] = "s"
            # TITLE, RAWFILE, USERNAME, search parameters and annotations are inert
            # and deliberately never copied into any public result.
        else:
            fields = line.split()
            require(len(fields) == 2, "首版 MGF 峰表必须恰好两列 m/z 和强度；不丢弃额外列。")
            mz, intensity = numeric(fields[0], 0), numeric(fields[1])
            item["points"] += 1; seen_peak = True
            if item["index"] == selected and item["points"] <= MAX_POINTS:
                values.extend((mz, intensity))
        require(metadata_bytes <= 1024**2)
    require(item is None and spectra, "MGF 谱未闭合或未包含受支持的谱块。")
    return spectra, values, 0


def xml_document(data):
    from defusedxml import ElementTree
    depth = count = metadata_size = 0
    iterator = ElementTree.iterparse(io.BytesIO(data), events=("start", "end", "pi"), forbid_dtd=True, forbid_entities=True, forbid_external=True)
    for event, node in iterator:
        require(event != "pi", "mzML 不处理 XML 指令、DTD 或实体。")
        if event == "start":
            depth += 1; count += 1
            require(depth <= 24 and count <= 100000 and len(node.attrib) <= 20)
            require(type(node.tag) is str and node.tag.startswith(NS) and len(node.tag) <= 128)
            for key, value in node.attrib.items():
                require(len(key) <= 128 and len(value) <= 2048)
                metadata_size += len(key) + len(value)
            require(metadata_size <= 1024**2)
        else:
            depth -= 1
            if node.tag != NS + "binary":
                require(len(node.text or "") <= 4096)
            require(len(node.tail or "") <= 4096)
    root = iterator.root
    if root.tag == NS + "indexedmzML":
        mzmls = root.findall(NS + "mzML")
        require(len(mzmls) == 1 and all(child.tag in {NS + "mzML", NS + "indexList", NS + "indexListOffset", NS + "fileChecksum"} for child in root))
        root = mzmls[0]  # offsets/checksum are not trusted as access capabilities.
    require(root.tag == NS + "mzML" and root.get("version") in {"1.1.0", "1.1.1"})
    return root


def cv(element):
    result = {}
    for child in element.findall(NS + "cvParam"):
        accession = child.get("accession", "")
        require(re.fullmatch(r"[A-Z]{2,8}:[0-9]{1,12}", accession) is not None and accession not in result,
                "mzML CV 参数无效或重复。")
        result[accession] = child.attrib
    return result


def binary_descriptor(element, points):
    if set(child.tag for child in element) - {NS + "cvParam", NS + "binary"}:
        return None
    fields = cv(element)
    allowed = {"MS:1000514", "MS:1000515", "MS:1000521", "MS:1000523", "MS:1000574", "MS:1000576"}
    if set(fields) - allowed: return None  # Numpress, external data, extra arrays.
    role = set(fields) & {"MS:1000514", "MS:1000515"}
    dtype = set(fields) & {"MS:1000521", "MS:1000523"}
    compression = set(fields) & {"MS:1000574", "MS:1000576"}
    if len(role) != 1 or len(dtype) != 1 or len(compression) != 1: return None
    if element.get("arrayLength") is not None and natural(element.get("arrayLength")) != points: return None
    # Default arrays must not override their source length or reference a file.
    if set(element.attrib) - {"encodedLength", "arrayLength", "dataProcessingRef"}: return None
    binaries = element.findall(NS + "binary")
    if len(binaries) != 1 or list(binaries[0]) or binaries[0].attrib: return None
    text = binaries[0].text or ""
    compact = "".join(text.split())
    if natural(element.get("encodedLength")) != len(compact): return None
    role = next(iter(role)); width = 4 if "MS:1000521" in dtype else 8
    unit = fields[role].get("unitAccession")
    if unit is not None and re.fullmatch(r"[A-Z]{2,8}:[0-9]{1,12}", unit) is None: return None
    if role == "MS:1000514" and unit not in {None, "MS:1000040"}: return None
    return {"role": role, "width": width, "compressed": "MS:1000574" in compression, "encoded": compact, "unit": unit}


def decode(binary, points):
    expected = points * binary["width"]
    # Base64 allocation is bounded before calling its decoder. Compressed input
    # is bounded independently of claimed expansion; decoder can produce only
    # expected+1 bytes, including any hostile stream's first over-budget byte.
    encoded = binary["encoded"]
    require(len(encoded) <= 4 * ((MAX_POINTS * 8 + 1024 + 2) // 3))
    raw = base64.b64decode(encoded, validate=True)
    require(base64.b64encode(raw).decode("ascii") == encoded)
    if binary["compressed"]:
        decompressor = zlib.decompressobj()
        data = decompressor.decompress(raw, expected + 1)
        require(len(data) == expected and decompressor.eof and not decompressor.unused_data and not decompressor.unconsumed_tail,
                "mzML 压缩数组长度、结束标记或展开预算不符。")
    else:
        data = raw
        require(len(data) == expected)
    values = [v[0] for v in struct.iter_unpack("<f" if binary["width"] == 4 else "<d", data)]
    require(len(values) == points and all(math.isfinite(v) and abs(v) <= 1e308 for v in values))
    return values, expected


def mzml(data, selected):
    root = xml_document(data)
    runs = root.findall(NS + "run")
    require(len(runs) == 1)
    lists = runs[0].findall(NS + "spectrumList")
    require(len(lists) == 1)
    elements = lists[0].findall(NS + "spectrum")
    require(1 <= len(elements) <= MAX_SPECTRA and natural(lists[0].get("count"), MAX_SPECTRA) == len(elements))
    require(len(list(lists[0])) == len(elements), "mzML 谱目录包含不支持的元素。")
    spectra, values, decoded, ids = [], [], 0, set()
    for index, element in enumerate(elements):
        require(natural(element.get("index"), MAX_SPECTRA - 1) == index)
        native_id = element.get("id")
        require(native_id and native_id not in ids and len(native_id) <= 512); ids.add(native_id)
        item = blank(index); item["points"] = natural(element.get("defaultArrayLength"))
        fields = cv(element)
        representations = set(fields) & {"MS:1000127", "MS:1000128"}
        if len(representations) == 1:
            item["representation"] = "centroid" if "MS:1000127" in representations else "profile"
        else: item["reason"] = "unsupported-representation"
        if "MS:1000511" in fields:
            level = natural(fields["MS:1000511"].get("value"), 100)
            require(level >= 1); item["ms_level"] = level
        if element.findall(".//" + NS + "referenceableParamGroupRef"):
            item["reason"] = "ambiguous-metadata"
        scanlists = element.findall(NS + "scanList")
        if len(scanlists) > 1: item["reason"] = "ambiguous-metadata"
        elif scanlists:
            scans = scanlists[0].findall(NS + "scan")
            require(natural(scanlists[0].get("count")) == len(scans))
            if len(scans) > 1: item["reason"] = "ambiguous-metadata"
            elif scans:
                scanfields = cv(scans[0])
                if "MS:1000016" in scanfields:
                    time = scanfields["MS:1000016"]
                    unit = {"UO:0000010": "s", "UO:0000031": "min"}.get(time.get("unitAccession"))
                    if unit:
                        item["retention_time"] = numeric(time.get("value"), 0, 1e12); item["time_unit"] = unit
                    else: item["reason"] = "ambiguous-metadata"
        precursorlists = element.findall(NS + "precursorList")
        precursors = precursorlists[0].findall(NS + "precursor") if len(precursorlists) == 1 else []
        if precursorlists: require(len(precursorlists) == 1 and natural(precursorlists[0].get("count")) == len(precursors))
        if len(precursors) > 1: item["reason"] = "ambiguous-metadata"
        elif precursors:
            ionslists = precursors[0].findall(NS + "selectedIonList")
            ions = ionslists[0].findall(NS + "selectedIon") if len(ionslists) == 1 else []
            if len(ionslists) != 1 or len(ions) != 1: item["reason"] = "ambiguous-metadata"
            else:
                require(natural(ionslists[0].get("count")) == 1)
                ion = cv(ions[0])
                if "MS:1000744" in ion:
                    precursor = ion["MS:1000744"]
                    if precursor.get("unitAccession") not in {None, "MS:1000040"}:
                        item["reason"] = "ambiguous-metadata"
                    else: item["precursor_mz"] = numeric(precursor.get("value"), 0)
        arrayslists = element.findall(NS + "binaryDataArrayList")
        arrays = arrayslists[0].findall(NS + "binaryDataArray") if len(arrayslists) == 1 else []
        if len(arrayslists) != 1 or len(arrays) != 2: item["reason"] = "unsupported-encoding"; binaries = []
        else:
            require(natural(arrayslists[0].get("count")) == 2 and len(list(arrayslists[0])) == 2)
            binaries = [binary_descriptor(array, item["points"]) for array in arrays]
            if any(v is None for v in binaries) or {v["role"] for v in binaries if v} != {"MS:1000514", "MS:1000515"}:
                item["reason"] = "unsupported-encoding"
            else:
                item["mz_unit"] = next(v["unit"] for v in binaries if v["role"] == "MS:1000514")
                item["intensity_unit"] = next(v["unit"] for v in binaries if v["role"] == "MS:1000515")
        spectra.append(finish(item))
        if index == selected:
            require(item["selectable"], "所选 mzML 谱的表示、数组编码、元信息或点数不受首版支持。")
            results = {}
            for binary in binaries:
                result, count = decode(binary, item["points"]); decoded += count; results[binary["role"]] = result
            require(all(v >= 0 for v in results["MS:1000514"]))
            values = [v for pair in zip(results["MS:1000514"], results["MS:1000515"]) for v in pair]
    return spectra, values, decoded


def mass_spectrum_preview(data, fmt, kind="tree", options=None, output_limit=MAX_OUTPUT):
    try:
        require(type(data) is bytes and 1 <= len(data) <= MAX_INPUT and type(fmt) is str and fmt in FORMATS)
        options = validate_mass_spectrum_options(kind, {} if options is None else options)
        index = spectrum_index(options["spectrum"]) if kind == "series" else None
        spectra, values, decoded = (mgf if fmt == "mgf" else mzml)(data, index)
        offset = index if index is not None else options["offset"]
        require(offset < len(spectra))
        chosen = spectra[offset:offset + PAGE] if kind == "tree" else [spectra[offset]]
        if kind == "series": require(chosen[0]["selectable"])
        result = {"contract_version": 2, "type": "mass-spectrum", "reader": "mass-spectrum", "kind": kind,
            "media_type": "application/json", "choices": {"spectra": chosen}, "selected": options,
            "warnings": [WARNING], "sampled": False,
            "metadata": {"format": fmt, "dialect": "mgf-ions-2column" if fmt == "mgf" else "mzml-1.1-inline-float",
                "input_mode": "whole", "source_bytes": len(data), "total_spectra": len(spectra), "offset": offset,
                "next_offset": offset + PAGE if kind == "tree" and offset + PAGE < len(spectra) else None,
                "decoded_bytes": decoded, "output_points": len(values) // 2}}
        if kind == "tree":
            result["tree"] = [{"path": "/" + v["id"], "node_type": "array", "attributes": {"label": "Spectrum " + str(v["index"] + 1)}} for v in chosen]
        else: result["array"] = {"shape": [chosen[0]["points"], 2], "dimensions": ["m/z", "intensity"], "values": values}
        return validate_mass_spectrum_payload(result, kind=kind, options=options, fmt=fmt, size=len(data), limit=output_limit)
    except MassSpectrumError:
        raise
    except Exception:
        raise MassSpectrumError("质谱内容不完整、不受支持或超出安全预算。") from None
