"""Original bounded XML profile readers, not an XSD validator or analysis engine.

Semantics: Malvern Panalytical XRDML schemas and canSAS1d element_SASdata.
No XSLT/schema loading, paths, networking, plugins or native dependencies.
"""
from __future__ import annotations
from decimal import Decimal
import re
import xml.etree.ElementTree as ET
from .diffraction_payload import (DiffractionError, ERROR, FORMATS, LIMITS, MAX_INPUT, SEMANTICS, UNCERTAINTY,
    WARNINGS, number, require, validate_diffraction_options, validate_diffraction_payload, validate_scan)

XRD_NAMESPACES = {"http://www.xrdml.com/XRDMeasurement/" + version for version in
                  ("1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "2.0", "2.1", "2.2", "2.3", "2.4")}
CANSAS_NAMESPACES = {"cansas1d/1.0": "1.0", "urn:cansas1d:1.1": "1.1"}
LEXEME = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?\Z")

class BoundedTree(ET.TreeBuilder):
    def __init__(self):
        super().__init__(); self.depth = self.nodes = self.text_size = 0
    def start(self, tag, attrs):
        self.depth += 1; self.nodes += 1
        require(self.depth <= 32 and self.nodes <= 350000 and len(tag) <= 256 and len(attrs) <= 16)
        for name, value in attrs.items():
            require(len(name) <= 256 and len(value) <= 2048 and name.rsplit("}", 1)[-1].lower() not in ("href", "src", "base"))
        require(not tag.startswith("{http://www.w3.org/2001/XInclude}"))
        return super().start(tag, attrs)
    def end(self, tag):
        self.depth -= 1; return super().end(tag)
    def data(self, value):
        self.text_size += len(value); require(self.text_size <= MAX_INPUT)
        return super().data(value)
    def doctype(self, *args): require(False)
    def pi(self, *args): require(False)

def scalar(text):
    require(isinstance(text, str) and len(text) <= 64 and LEXEME.fullmatch(text))
    exact = Decimal(text); value = float(exact)
    require(number(value) and (exact == 0 or value != 0))
    # Integer source values must not silently lose binary64 precision.
    require(exact != exact.to_integral_value() or abs(exact) <= 9007199254740991)
    return value

def vector(element, minimum=1, maximum=16384):
    require(element is not None and len(element) == 0)
    values = (element.text or "").split(None, maximum)
    require(minimum <= len(values) <= maximum)
    return [scalar(value) for value in values]

def children(node, namespace):
    result = {}
    for child in node:
        require(isinstance(child.tag, str) and child.tag.startswith(namespace))
        name = child.tag[len(namespace):]
        result.setdefault(name, []).append(child)
    return result

def one(mapping, name):
    values = mapping.get(name, [])
    require(len(values) == 1)
    return values[0]

def _position(element, ns, points):
    require(set(element.attrib) == {"axis", "unit"})
    row = children(element, ns)
    if set(row) == {"commonPosition"}: return vector(one(row, "commonPosition"), maximum=1), "constant"
    if set(row) == {"listPositions"}: return vector(one(row, "listPositions"), points, points), "explicit"
    require(set(row) == {"startPosition", "endPosition"})
    begin, end = vector(one(row, "startPosition"), maximum=1)[0], vector(one(row, "endPosition"), maximum=1)[0]
    values = [begin + (end - begin) * i / (points - 1) for i in range(points)]
    values[-1] = end
    return values, "linear-declared"

def xrdml(root, ns):
    require(root.tag == ns + "xrdMeasurements" and root.get("status") == "Completed")
    measurements = root.findall(ns + "xrdMeasurement")
    require(1 <= len(measurements) <= 32)
    scans = []
    for measurement in measurements:
        entries = measurement.findall(ns + "scan")
        require(entries and len(entries) + len(scans) <= 32)
        for node in entries:
            require(node.get("status") == "Completed" and node.get("mode") in ("Continuous", "Pre-set time", "Pre-set counts"))
            axis = node.get("scanAxis")
            axes = {"2Theta": ("2Theta",), "Omega": ("Omega",), "2Theta-Omega": ("2Theta", "Omega"),
                    "Omega-2Theta": ("Omega", "2Theta"), "Gonio": ("2Theta",)}.get(axis)
            require(axes is not None)
            blocks = node.findall(ns + "dataPoints"); require(len(blocks) == 1)
            values = children(blocks[0], ns)
            allowed = {"positions", "counts", "intensities", "commonCountingTime", "countingTimes",
                       "commonBeamAttenuationFactor", "beamAttenuationFactors", "commonDivergenceCorrection", "divergenceCorrections"}
            require(set(values) <= allowed and bool("counts" in values) != bool("intensities" in values))
            y_name = "counts" if "counts" in values else "intensities"
            require(y_name == ("intensities" if "/1." in ns else "counts"))
            intensity = one(values, y_name)
            require(intensity.attrib == {"unit": "counts"})
            y = vector(intensity, 2); n = len(y)
            require(sum(len(trace["x"]) for _, trace in scans) + n <= 65536)
            # Counting times/factors are validated but never applied to stored values.
            for short, long in (("commonCountingTime", "countingTimes"), ("commonBeamAttenuationFactor", "beamAttenuationFactors"),
                                ("commonDivergenceCorrection", "divergenceCorrections")):
                present = [name for name in (short, long) if name in values]
                require(len(present) <= 1 and (short != "commonCountingTime" or len(present) == 1))
                if present:
                    name = present[0]; element = one(values, name)
                    require(element.attrib == ({"unit": "seconds"} if short == "commonCountingTime" else {}))
                    factor = vector(element, 1 if name == short else n, 1 if name == short else n)
                    require(all(v > 0 for v in factor))
            positions = {}
            for element in values.get("positions", []):
                name = element.get("axis")
                require(name in ("2Theta", "Omega", "Phi", "Chi", "Psi", "X", "Y", "Z", "Gamma") and name not in positions)
                positions[name] = (*_position(element, ns, n), element.get("unit"))
            # Gonio curves use the explicitly stored 2Theta coordinate. Some
            # exporters omit Omega; that does not make the 1-D curve ambiguous.
            # Never synthesize a missing axis. If Omega is declared, retain the
            # coupled-axis checks below instead of silently ignoring conflicts.
            if axis == "Gonio" and "Omega" in positions:
                axes = ("2Theta", "Omega")
            require(set(axes) <= set(positions))
            for name, (coordinates, mode, unit) in positions.items():
                if name not in axes: require(mode == "constant")
                else: require(len(coordinates) == n and mode != "constant" and unit == "deg")
            x, mode, unit = positions[axes[0]]
            if len(axes) == 2:
                theta, omega = positions["2Theta"], positions["Omega"]
                require(theta[2] == omega[2])
                offset = omega[0][0] - theta[0][0] / 2
                require(all(abs((o - t / 2) - offset) <= 1e-8 * max(1, abs(o), abs(t)) for t, o in zip(theta[0], omega[0])))
                if axis == "Gonio": require(abs(offset) <= 1e-8)
            scans.append(({"x_quantity": axes[0], "x_unit": unit, "y_quantity": y_name, "y_unit": "counts",
                           "x_error": None, "y_error": None, "axis_mode": mode}, {"x": x, "y": y, "x_error": None, "y_error": None}))
    require(len(root.findall(".//" + ns + "scan")) == len(scans))
    return scans, "xrdml-1d"

def cansas(root, ns, version):
    require(root.tag == ns + "SASroot" and root.get("version") == version)
    entries = root.findall(ns + "SASentry"); require(1 <= len(entries) <= 32)
    scans = []
    for entry in entries:
        datasets = entry.findall(ns + "SASdata"); require(datasets and len(datasets) + len(scans) <= 32)
        for dataset in datasets:
            mapping = children(dataset, ns); require(set(mapping) == {"Idata"})
            records = mapping["Idata"]; require(2 <= len(records) <= 16384)
            require(sum(len(trace["x"]) for _, trace in scans) + len(records) <= 65536)
            arrays = {"Q": [], "I": [], "Qdev": [], "Idev": []}; units = {}; fields = None
            for point in records:
                row = children(point, ns)
                require({"Q", "I"} <= set(row) <= {"Q", "I", "Qdev", "Idev"})
                if fields is None: fields = set(row)
                require(set(row) == fields)
                for name in fields:
                    element = one(row, name); require(set(element.attrib) == {"unit"})
                    unit = element.get("unit"); require(name not in units or units[name] == unit)
                    units[name] = unit; arrays[name].append(vector(element, maximum=1)[0])
            for base, error in (("Q", "Qdev"), ("I", "Idev")):
                if error in fields: require(units[error] == units[base] and all(v >= 0 for v in arrays[error]))
            scans.append(({"x_quantity": "Q", "x_unit": units["Q"], "y_quantity": "I", "y_unit": units["I"],
                           "x_error": UNCERTAINTY if "Qdev" in fields else None, "y_error": UNCERTAINTY if "Idev" in fields else None,
                           "axis_mode": "explicit"}, {"x": arrays["Q"], "y": arrays["I"],
                           "x_error": arrays["Qdev"] if "Qdev" in fields else None, "y_error": arrays["Idev"] if "Idev" in fields else None}))
    require(len(root.findall(".//" + ns + "SASdata")) == len(scans))
    return scans, "cansas1d-" + version

def diffraction_preview(data, fmt, kind="tree", options=None):
    selected = validate_diffraction_options(kind, {} if options is None else options)
    require(type(data) is bytes and 0 < len(data) <= MAX_INPUT and isinstance(fmt, str) and fmt in FORMATS)
    try:
        text = data.decode("utf-8-sig", errors="strict")
        declaration = re.match(r"\s*<\?xml\s+[^?]*\?>", text)
        if declaration:
            encoding = re.search(r"encoding\s*=\s*['\"]([^'\"]+)['\"]", declaration[0])
            require(encoding is None or encoding[1].lower() in ("utf-8", "utf8"))
        root = ET.fromstring(text, parser=ET.XMLParser(target=BoundedTree()))
        require(root.tag.startswith("{") and "}" in root.tag)
        namespace = root.tag[1:root.tag.index("}")]; ns = "{" + namespace + "}"
        if fmt == "xrdml":
            require(namespace in XRD_NAMESPACES); parsed, dialect = xrdml(root, ns)
        else:
            require(namespace in CANSAS_NAMESPACES); parsed, dialect = cansas(root, ns, CANSAS_NAMESPACES[namespace])
        require(1 <= len(parsed) <= 32 and sum(len(trace["x"]) for _, trace in parsed) <= 65536)
        scans = []
        for i, (choice, trace) in enumerate(parsed):
            scan = {"id": i, "label": "Scan " + str(i + 1), "points": len(trace["x"]), **choice}
            validate_scan(scan, i, dialect)
            x = trace["x"]
            require(all(x[j] > x[j - 1] for j in range(1, len(x))) or all(x[j] < x[j - 1] for j in range(1, len(x))))
            if dialect.startswith("cansas"): require(all(v >= 0 for v in x))
            scans.append(scan)
        require(kind == "tree" or selected["scan"] < len(scans))
        result = {"contract_version": 2, "type": "diffraction", "reader": "diffraction", "kind": kind, "media_type": "application/json",
                  "choices": {"scans": scans}, "selected": selected, "metadata": {"format": fmt, "dialect": dialect, "input_mode": "whole", "source_bytes": len(data),
                  "scan_count": len(scans), "total_points": sum(s["points"] for s in scans), "returned_points": 0 if kind == "tree" else scans[selected["scan"]]["points"],
                  "limits": dict(LIMITS), "value_semantics": SEMANTICS}, "warnings": list(WARNINGS), "sampled": False}
        if kind == "tree": result["tree"] = [{"path": "/scan-" + str(s["id"]), "node_type": "scan", "attributes": {"label": s["label"]}} for s in scans]
        else: result["series"] = [{"scan": selected["scan"], **parsed[selected["scan"]][1]}]
        return validate_diffraction_payload(result, kind=kind, options=selected, size=len(data), fmt=fmt)
    except DiffractionError: raise
    except Exception: raise DiffractionError(ERROR) from None
