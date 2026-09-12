"""Pure ODIM 2.4 SCAN contract, mirrored in the API without HDF5 imports."""
from __future__ import annotations
import json
import math

ERROR = "ODIM 2.4 扫描、存储码、窗口或读取预算不符合当前雷达预览规范。"
FORMATS = {"h5", "hdf5"}
UNITS = {"DBZH": "dBZ", "DBZV": "dBZ", "TH": "dBZ", "TV": "dBZ", "ZDR": "dB",
         "RHOHV": "1", "PHIDP": "degrees", "KDP": "degrees/km", "VRADH": "m/s", "WRADH": "m/s"}
LIMITS = {"max_sweeps": 32, "max_quantities": 16, "max_fields": 128, "max_rays": 128,
          "max_gates": 128, "max_values": 16384, "max_attribute_bytes": 32768,
          "max_chunk_bytes": 4194304, "max_decoded_bytes": 16777216}
WARNINGS = ["显示射线存储索引与斜距门中心，不推算方位、地面距离、地理坐标或地球曲率；不旋转 a1gate，不拼接扫描或请求在线底图。",
            "数值为原始存储码；nodata（无数据）与 undetect（低于探测阈值）分开显示，均不作为测量零值。仅手动切换时应用已声明 gain/offset；不反演降雨、不校正衰减或退模糊。"]
VALUE_SEMANTICS = "raw unsigned storage codes; declared nodata/undetect retained; gain/offset not applied"
RANGE_SEMANTICS = "ODIM_H5/V2_4 rstart + (gate_index + 0.5) * rscale; slant range metres"


def require(value):
    if not value: raise ValueError(ERROR)


def integer(value, lo=0, hi=2**31-1):
    return type(value) is int and lo <= value <= hi


def finite(value, bound=1e12):
    return type(value) in {int, float} and math.isfinite(value) and abs(value) <= bound


def keys(value, expected):
    return type(value) is dict and set(value) == set(expected.split())


def dtype(value):
    return type(value) is str and value in {"|u1", "<u2", ">u2"}


def validate_radar_window_options(kind, options):
    require(type(kind) is str and kind in {"tree", "image"} and type(options) is dict)
    if kind == "tree": require(not options); return {}
    require(keys(options, "sweep quantity ray_start ray_count gate_start gate_count decode"))
    require(integer(options["sweep"], 1, 32) and type(options["quantity"]) is str and options["quantity"] in UNITS
            and integer(options["ray_start"], 0, 4095) and integer(options["gate_start"], 0, 65535)
            and integer(options["ray_count"], 1, 128) and integer(options["gate_count"], 1, 128)
            and options["decode"] == "raw" and type(options["decode"]) is str)
    return dict(options)


def _validate(value, *, kind=None, options=None, fmt=None, source_bytes=None, read_bytes=None, read_requests=None):
    require(type(value) is dict)
    actual = value.get("kind"); selected = validate_radar_window_options(actual, value.get("selected"))
    require(keys(value, "contract_version type reader kind media_type choices selected metadata warnings sampled "
                 + ("tree" if actual == "tree" else "array radar")))
    require(type(value["contract_version"]) is int and value["contract_version"] == 2
            and value["type"] == value["reader"] == "radar-window" and value["media_type"] == "application/json"
            and value["warnings"] == WARNINGS and type(value["sampled"]) is bool
            and (kind is None or kind == actual) and (options is None or selected == validate_radar_window_options(actual, options)))
    m = value["metadata"]
    require(keys(m, "format odim_version object input_mode source_bytes read_bytes read_requests attribute_bytes chunks_touched decoded_chunk_bytes numeric_bytes_read value_semantics range_semantics geometry limits"))
    require(type(m["format"]) is str and m["format"] in FORMATS and (fmt is None or m["format"] == fmt)
            and m["odim_version"] == "ODIM_H5/V2_4" and m["object"] in {"PVOL", "SCAN"} and m["input_mode"] == "window"
            and integer(m["source_bytes"], 256, 8*1024**3) and integer(m["read_bytes"], 1, 8*1024**2)
            and integer(m["read_requests"], 1, 128) and integer(m["attribute_bytes"], 1, 32768)
            and integer(m["chunks_touched"], 0, 128) and integer(m["decoded_chunk_bytes"], 0, 16777216)
            and integer(m["numeric_bytes_read"], 0, 32768) and m["value_semantics"] == VALUE_SEMANTICS
            and m["range_semantics"] == RANGE_SEMANTICS and m["geometry"] == "ray index versus slant range; not georeferenced"
            and m["limits"] == LIMITS and all(type(v) is int for v in m["limits"].values()))
    for key, expected in (("source_bytes", source_bytes), ("read_bytes", read_bytes), ("read_requests", read_requests)):
        require(expected is None or type(expected) is int and m[key] == expected)
    require(keys(value["choices"], "sweeps") and type(value["choices"]["sweeps"]) is list)
    sweeps = value["choices"]["sweeps"]
    require(1 <= len(sweeps) <= 32 and (m["object"] != "SCAN" or len(sweeps) == 1))
    fields = 0
    for i, sweep in enumerate(sweeps, 1):
        require(keys(sweep, "id elevation nrays nbins rstart_m rscale_m a1gate quantities")
                and integer(sweep["id"], i, i) and finite(sweep["elevation"], 90) and sweep["elevation"] >= -10
                and integer(sweep["nrays"], 1, 4096) and integer(sweep["nbins"], 1, 65536)
                and integer(sweep["a1gate"], 0, sweep["nrays"]-1)
                and finite(sweep["rstart_m"], 1e6) and sweep["rstart_m"] >= 0
                and finite(sweep["rscale_m"], 1e5) and sweep["rscale_m"] > 0
                and sweep["rstart_m"] + sweep["nbins"]*sweep["rscale_m"] <= 1e7
                and type(sweep["quantities"]) is list and 1 <= len(sweep["quantities"]) <= 16)
        ids = set()
        for quantity in sweep["quantities"]:
            require(keys(quantity, "id dtype chunks gain offset nodata undetect unit"))
            ident = quantity["id"]
            require(type(ident) is str and ident in UNITS and ident not in ids and dtype(quantity["dtype"])
                    and quantity["unit"] == UNITS[ident] and finite(quantity["gain"], 1e6) and quantity["gain"] != 0
                    and finite(quantity["offset"], 1e9))
            ids.add(ident); fields += 1
            maximum = 255 if quantity["dtype"] == "|u1" else 65535
            require(integer(quantity["nodata"], 0, maximum) and integer(quantity["undetect"], 0, maximum)
                    and quantity["nodata"] != quantity["undetect"])
            require(all(finite(quantity["offset"]+quantity["gain"]*v) for v in (0, maximum)))
            chunks = quantity["chunks"]
            require(chunks is None or type(chunks) is list and len(chunks) == 2
                    and all(integer(n, 1, size) for n, size in zip(chunks, (sweep["nrays"], sweep["nbins"]))))
            if chunks is not None: require(math.prod(chunks)*int(quantity["dtype"][-1]) <= 4194304)
    require(fields <= 128)
    if actual == "tree":
        require(value["tree"] == [{"path": f"/sweeps/{s['id']}", "node_type": "array", "shape": [s["nrays"], s["nbins"]]} for s in sweeps]
                and all(type(n) is int for item in value["tree"] for n in item["shape"])
                and value["sampled"] is False and m["chunks_touched"] == m["decoded_chunk_bytes"] == m["numeric_bytes_read"] == 0)
    else:
        require(selected["sweep"] <= len(sweeps))
        sweep = sweeps[selected["sweep"]-1]
        quantity = next((q for q in sweep["quantities"] if q["id"] == selected["quantity"]), None)
        require(quantity is not None and selected["ray_start"]+selected["ray_count"] <= sweep["nrays"]
                and selected["gate_start"]+selected["gate_count"] <= sweep["nbins"])
        rows, columns = selected["ray_count"], selected["gate_count"]
        a = value["array"]
        require(keys(a, "shape dimensions dtype values") and type(a["shape"]) is list and a["shape"] == [rows, columns]
                and all(type(n) is int for n in a["shape"]) and a["dimensions"] == ["ray", "gate"]
                and a["dtype"] == quantity["dtype"] and type(a["values"]) is list and len(a["values"]) == rows*columns
                and all(integer(v, 0, 255 if a["dtype"] == "|u1" else 65535) for v in a["values"]))
        r = value["radar"]
        require(keys(r, "range_m ray_indices nodata_count undetect_count")
                and type(r["range_m"]) is list and len(r["range_m"]) == columns
                and all(finite(v) and v == sweep["rstart_m"]+(selected["gate_start"]+i+.5)*sweep["rscale_m"] for i, v in enumerate(r["range_m"]))
                and type(r["ray_indices"]) is list and r["ray_indices"] == list(range(selected["ray_start"], selected["ray_start"]+rows))
                and all(type(v) is int for v in r["ray_indices"])
                and integer(r["nodata_count"], 0, rows*columns) and r["nodata_count"] == a["values"].count(quantity["nodata"])
                and integer(r["undetect_count"], 0, rows*columns) and r["undetect_count"] == a["values"].count(quantity["undetect"]))
        require(m["numeric_bytes_read"] == rows*columns*int(a["dtype"][-1])
                and value["sampled"] == (rows < sweep["nrays"] or columns < sweep["nbins"]))
        chunks = quantity["chunks"]
        touched = math.prod((start+count-1)//chunk-start//chunk+1 for start, count, chunk in
                           zip((selected["ray_start"], selected["gate_start"]), (rows, columns), chunks)) if chunks else 0
        require(m["chunks_touched"] == touched and m["decoded_chunk_bytes"] == (touched*math.prod(chunks)*int(a["dtype"][-1]) if chunks else 0))
    require(len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) <= 2*1024**2 - 512)
    return value


def validate_radar_window_payload(value, **bindings):
    try: return _validate(value, **bindings)
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError): raise ValueError(ERROR) from None
