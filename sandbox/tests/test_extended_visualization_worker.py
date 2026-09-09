"""Real library fixtures and adversarial protocol checks for v2 readers."""
from __future__ import annotations

import base64
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest

from app.services import extended_visualization_worker as worker


def optional(name):
    if os.environ.get("AI_DATASEEK_REQUIRE_VISUALIZATION_V2") == "1":
        return importlib.import_module(name)
    return pytest.importorskip(name)


def executable(name):
    if shutil.which(name):
        return
    if os.environ.get("AI_DATASEEK_REQUIRE_VISUALIZATION_V2") == "1":
        pytest.fail(f"Required v2 runtime executable is missing: {name}")
    pytest.skip(f"Optional v2 runtime executable is missing: {name}")


def request(data=b"x,y\n1,2\n", **changes):
    header = {"contract_version": 2, "size": len(data), "reader": "tabular", "kind": "table", "format": "csv", "options": {}}
    header.update(changes)
    return json.loads(worker._encoded_result(io.BytesIO(json.dumps(header).encode() + b"\n" + data)))


def zip_bytes(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return stream.getvalue()


def npy_bytes(array):
    stream = io.BytesIO()
    np.save(stream, array, allow_pickle=True)
    return stream.getvalue()


def hdf_bytes(tmp_path, setup):
    h5py = optional("h5py")
    path = tmp_path / "fixture.h5"
    with h5py.File(path, "w") as root:
        setup(root)
    return path.read_bytes()


def xlsx_bytes():
    openpyxl = optional("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Measures"
    sheet.append(["time", "temperature", "formula"])
    sheet.append([1, 20.5, "=A2+B2"])
    book.create_sheet("Other").append([7, 8])
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


@pytest.mark.parametrize("changes", [
    {"contract_version": 1}, {"contract_version": True}, {"size": True}, {"size": 0},
    {"size": worker.MAX_INPUT_BYTES + 1}, {"reader": "python"}, {"kind": "exec"},
    {"format": "../csv"}, {"format": "CSV"}, {"format": "https://example.test/a.csv"},
    {"path": "/etc/passwd"}, {"options": {"path": "/etc/passwd"}},
    {"options": {"code": "print(1)"}}, {"options": []}, {"truncated": True},
    {"truncated": 1}, {"reader": None}, {"format": None}, {"size": 1},
])
def test_protocol_rejects_unsupported_or_unsafe_header(changes):
    result = request(**changes)
    assert result["ok"] is False
    assert set(result) == {"ok", "error"}
    assert "/etc/" not in result["error"] and "Traceback" not in result["error"]


@pytest.mark.parametrize("data", [b"", b"{}", b"[1]\n", b"x" * 8193 + b"\n", b"not JSON\n"])
def test_bad_framing_is_safe(data):
    assert json.loads(worker._encoded_result(io.BytesIO(data)))["ok"] is False


@pytest.mark.parametrize("options", [
    {"row_offset": -1}, {"row_offset": True}, {"row_offset": 100001},
    {"column_offset": 16384}, {"y_columns": []}, {"y_columns": [0, 0]},
    {"y_columns": [100]}, {"x_column": True}, {"indices": [False]},
    {"indices": [0] * 7}, {"variable": "../array"}, {"variable": "bad\x00name"},
])
def test_tabular_option_budget(options):
    assert request(options=options)["ok"] is False


def test_csv_real_table_and_numeric_series():
    data = b"time,temp\n0,1.25\n1,2.5\n2,\n"
    table = request(data)["data"]
    assert table["contract_version"] == 2 and table["type"] == "tabular"
    assert table["table"]["rows"] == [["0", "1.25"], ["1", "2.5"], ["2", ""]]
    series = request(data, kind="series", options={"x_column": 0, "y_columns": [1]})["data"]
    assert series["array"] == {"shape": [3, 1], "dimensions": ["row", "column"], "values": [1.25, 2.5, None]}
    assert series["selected"] == {"x_column": 0, "y_columns": [1]}


def test_csv_window_is_bounded_and_does_not_claim_unknown_total():
    data = ("a,b\n" + "\n".join(f"{n},{n + 1}" for n in range(1000))).encode()
    result = request(data, options={"row_offset": 10})["data"]
    assert len(result["table"]["rows"]) == 200
    assert result["table"]["rows"][0] == ["10", "11"]
    assert result["table"]["total_rows"] is None
    assert result["sampled"] is True


def test_tsv_and_host_path_text_are_safe():
    result = request(b"x\ty\n1\t/Users/private/source.csv\n", format="tsv")["data"]
    assert result["table"]["rows"] == [["1", "[redacted]"]]


@pytest.mark.parametrize("data,options", [(b"x,y\na,1\n", {}), (b"x\n1\n", {"y_columns": [1]}), (b"x\n1\n", {"x_column": 3})])
def test_series_does_not_silently_coerce_text_or_invalid_columns(data, options):
    assert request(data, kind="series", options=options)["ok"] is False


def test_npy_real_array_bounds_and_nonfinite():
    array = np.array([[1.5, np.inf, np.nan], [2.5, 4, 5]])
    result = request(npy_bytes(array), format="npy", kind="heatmap")["data"]
    assert result["array"]["shape"] == [2, 3]
    assert result["array"]["values"] == [1.5, None, None, 2.5, 4, 5]
    assert result["selected"]["variable"] == "array"


def test_npy_slices_are_explicit_and_bounded():
    array = np.arange(3 * 300 * 500).reshape(3, 300, 500)
    result = request(npy_bytes(array), format="npy", kind="heatmap", options={"indices": [2]})["data"]
    assert result["selected"]["indices"] == [2]
    assert result["array"]["shape"] == [100, 125]
    assert result["array"]["values"][0] == 300000
    assert len(result["array"]["values"]) <= worker.MAX_ARRAY_VALUES
    assert result["sampled"] is True


@pytest.mark.parametrize("array", [np.array(["x"]), np.array([object()], dtype=object), np.array([1 + 2j]), np.array([]), np.array(1)])
def test_npy_rejects_object_complex_empty_and_scalar(array):
    assert request(npy_bytes(array), format="npy", kind="heatmap")["ok"] is False


def test_npz_variable_selection_and_no_pickle():
    stream = io.BytesIO()
    np.savez(stream, a=np.arange(6).reshape(2, 3), b=np.arange(4))
    result = request(stream.getvalue(), format="npz", kind="series", options={"variable": "b"})["data"]
    assert result["choices"]["variables"] == ["a", "b"]
    assert result["array"]["values"] == [0, 1, 2, 3]


@pytest.mark.parametrize("name", ["../a.npy", "/a.npy", "nested/a.npy", "a.pkl"])
def test_npz_untrusted_archive_paths(name):
    assert request(zip_bytes({name: npy_bytes(np.arange(4))}), format="npz")["ok"] is False


def test_npy_declared_huge_shape_rejected_before_numpy_allocation(monkeypatch):
    stream = io.BytesIO()
    np.lib.format.write_array_header_1_0(stream, {"descr": "<f8", "fortran_order": False, "shape": (2**40,)})
    def forbidden_load(*args, **kwargs):
        pytest.fail("numpy.load must not receive an oversized declared shape")
    monkeypatch.setattr(np, "load", forbidden_load)
    assert request(stream.getvalue(), format="npy")["ok"] is False
    assert request(zip_bytes({"bad.npy": stream.getvalue()}), format="npz")["ok"] is False


def test_mat_real_named_array():
    scipy_io = optional("scipy.io")
    stream = io.BytesIO()
    scipy_io.savemat(stream, {"temperature": np.arange(6).reshape(2, 3)})
    result = request(stream.getvalue(), format="mat", kind="heatmap")["data"]
    assert result["array"]["values"] == [0, 1, 2, 3, 4, 5]
    assert result["selected"]["variable"] == "temperature"


def test_hdf5_real_tree_attributes_and_slice(tmp_path):
    def setup(root):
        dataset = root.create_dataset("entry/signal", data=np.arange(24).reshape(2, 3, 4))
        dataset.attrs["units"] = np.bytes_("counts")
        dataset.attrs["sensitive"] = np.bytes_("/Users/owner/data")
    data = hdf_bytes(tmp_path, setup)
    tree = request(data, reader="hdf5", format="h5", kind="tree")["data"]
    assert tree["tree"][1]["path"] == "/entry/signal"
    assert tree["tree"][1]["attributes"]["units"] == "counts"
    assert tree["tree"][1]["attributes"]["sensitive"] == "[redacted]"
    result = request(data, reader="hdf5", format="h5", kind="heatmap", options={"path": "/entry/signal", "indices": [1]})["data"]
    assert result["array"]["values"] == list(range(12, 24))
    assert result["selected"]["path"] == "/entry/signal"


def test_hdf5_never_follows_external_soft_or_virtual_data(tmp_path):
    h5py = optional("h5py")
    def setup(root):
        root["outside"] = h5py.ExternalLink("/etc/secret.h5", "/data")
        root["soft"] = h5py.SoftLink("/outside")
        root.create_dataset("ordinary", data=np.arange(4))
        layout = h5py.VirtualLayout(shape=(4,), dtype="i4")
        layout[:] = h5py.VirtualSource("/etc/secret.h5", "data", shape=(4,))
        root.create_virtual_dataset("virtual", layout)
    data = hdf_bytes(tmp_path, setup)
    result = request(data, reader="hdf5", format="h5", kind="tree")["data"]
    types = {node["path"]: node["node_type"] for node in result["tree"]}
    assert types["/outside"] == types["/soft"] == "blocked-link"
    assert types["/virtual"] == "blocked-dataset"
    assert "/etc/" not in json.dumps(result)
    for path in ("/outside", "/soft", "/virtual"):
        assert request(data, reader="hdf5", format="h5", kind="series", options={"path": path})["ok"] is False


@pytest.mark.parametrize("path", ["relative", "/../outside", "/a//b", "/Users/private/data", "/a\\b", "/a/", "/" + "/".join(["a"] * 9)])
def test_hdf5_path_protocol_does_not_accept_host_paths(path):
    assert request(reader="hdf5", format="h5", kind="series", options={"path": path})["ok"] is False


def test_hdf5_cycles_and_tree_budget(tmp_path):
    def setup(root):
        group = root.create_group("group")
        group["cycle"] = root
        for i in range(300):
            root.create_dataset(f"number_{i:03d}", data=[i])
    result = request(hdf_bytes(tmp_path, setup), reader="hdf5", format="h5", kind="tree")["data"]
    assert len(result["tree"]) == 256
    assert result["sampled"] is True


@pytest.mark.parametrize("fmt", ["h5", "hdf5", "nxs", "nx", "nc4", "nc"])
def test_hdf5_declared_extensions_have_real_hdf5_reader(tmp_path, fmt):
    data = hdf_bytes(tmp_path, lambda root: root.create_dataset("entry/signal", data=np.arange(8)))
    result = request(data, reader="hdf5", format=fmt, kind="series")
    assert result["ok"] is True
    assert result["data"]["array"]["values"] == list(range(8))


def test_hdf5_reads_real_netcdf4_but_rejects_classic_netcdf3(tmp_path):
    netcdf = optional("netCDF4")
    for format_name, expected in (("NETCDF4", True), ("NETCDF3_CLASSIC", False)):
        path = tmp_path / (format_name + ".nc")
        with netcdf.Dataset(path, "w", format=format_name) as dataset:
            dataset.createDimension("sample", 3)
            variable = dataset.createVariable("signal", "f4", ("sample",))
            variable[:] = [1.5, 2.5, 3.5]
        result = request(path.read_bytes(), reader="hdf5", format="nc", kind="series", options={"path": "/signal"})
        assert result["ok"] is expected
        if expected:
            assert result["data"]["array"]["values"] == [1.5, 2.5, 3.5]


def test_excel_workbook_values_and_formulas_are_separate():
    result = request(xlsx_bytes(), reader="excel", format="xlsx", kind="table")["data"]
    assert result["choices"]["sheets"] == ["Measures", "Other"]
    assert result["table"]["columns"] == ["A", "B", "C"]
    assert result["table"]["rows"][1] == [1, 20.5, None]
    assert result["table"]["formulas"][1] == [None, None, "=A2+B2"]
    other = request(xlsx_bytes(), reader="excel", format="xlsx", kind="table", options={"sheet": "Other"})["data"]
    assert other["table"]["rows"] == [[7, 8]]


def test_excel_real_window_is_bounded():
    openpyxl = optional("openpyxl")
    book = openpyxl.Workbook()
    for i in range(250):
        book.active.append(list(range(i, i + 120)))
    stream = io.BytesIO()
    book.save(stream)
    result = request(stream.getvalue(), reader="excel", format="xlsx", kind="table", options={"row_offset": 1, "column_offset": 2})["data"]
    assert len(result["table"]["rows"]) == 200
    assert len(result["table"]["rows"][0]) == 100
    assert result["table"]["rows"][0][0] == 3
    assert result["sampled"] is True


@pytest.mark.parametrize("extra,content", [
    ("xl/vbaProject.bin", b"macro"), ("xl/embeddings/object.bin", b"object"),
    ("xl/activeX/control.xml", b"control"), ("xl/externalLinks/link.xml", b"link"),
    ("xl/_rels/external.rels", b'<Relationships><Relationship TargetMode="External" Target="file:///etc/secret"/></Relationships>'),
    ("xl/_rels/entity.rels", b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><x>&x;</x>'),
])
def test_excel_rejects_macros_embedded_objects_and_external_relationships(extra, content):
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes())) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    entries[extra] = content
    result = request(zip_bytes(entries), reader="excel", format="xlsx", kind="table")
    assert result["ok"] is False
    assert "/etc/" not in result["error"]


def test_rdkit_real_smiles_png():
    optional("rdkit")
    result = request(b"CCO ethanol\nCC acetylene\n", reader="rdkit", kind="image", format="smi")["data"]
    assert result["media_type"] == "image/png"
    assert base64.b64decode(result["data_base64"]).startswith(b"\x89PNG\r\n\x1a\n")
    assert result["metadata"]["formula"] == "C2H6O"
    assert result["metadata"]["atoms"] == 3
    assert result["metadata"]["molecular_weight"] == pytest.approx(46.069, abs=.01)


def test_rdkit_real_mol_and_sdf():
    chem = optional("rdkit.Chem")
    data = chem.MolToMolBlock(chem.MolFromSmiles("CCO")).encode()
    for fmt, payload in (("mol", data), ("sdf", data + b"\n$$$$\n")):
        result = request(payload, reader="rdkit", kind="image", format=fmt)
        assert result["ok"] is True and result["data"]["metadata"]["atoms"] == 3


def test_rdkit_invalid_input_is_safe():
    optional("rdkit")
    assert request(b"not a molecule", reader="rdkit", kind="image", format="smi")["ok"] is False


METPY_OPTIONS = {"pressure_column": "p", "temperature_column": "t", "dewpoint_column": "td",
                 "pressure_unit": "hPa", "temperature_unit": "degC", "dewpoint_unit": "degC"}


def test_metpy_real_sounding_png():
    optional("metpy")
    result = request(b"p,t,td\n1000,20,16\n850,10,6\n700,0,-4\n500,-20,-25\n", reader="metpy", kind="image", format="csv", options=METPY_OPTIONS)["data"]
    assert result["metadata"]["levels"] == 4
    assert base64.b64decode(result["data_base64"]).startswith(b"\x89PNG")
    assert result["selected"] == METPY_OPTIONS


@pytest.mark.parametrize("options", [{}, {**METPY_OPTIONS, "pressure_unit": "guess"}, {**METPY_OPTIONS, "dewpoint_column": "t"}, {**METPY_OPTIONS, "code": "print(1)"}])
def test_metpy_never_guesses_columns_or_units(options):
    assert request(b"p,t,td\n1000,20,10\n", reader="metpy", kind="image", format="csv", options=options)["ok"] is False


def test_metpy_rejects_nonmonotonic_levels():
    optional("metpy")
    assert request(b"p,t,td\n1000,20,16\n850,10,6\n900,0,-4\n", reader="metpy", kind="image", format="csv", options=METPY_OPTIONS)["ok"] is False


def docx_fixture():
    return zip_bytes({
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>DataSeek isolated office preview fixture</w:t></w:r></w:p><w:sectPr/></w:body></w:document>',
    })


def office_quality_image():
    """A sharp, compressible RGB pattern, placed at 1,200 DPI in both fixtures."""
    image = optional("PIL.Image")
    y, x = np.indices((800, 1200))
    pixels = np.stack((x % 256, y % 256, ((x // 3 + y // 3) % 2) * 255), axis=-1).astype("uint8")
    bitmap = image.fromarray(pixels)
    output = io.BytesIO()
    bitmap.save(output, format="PNG")
    return output.getvalue(), bitmap.tobytes()


def office_quality_fixture(fmt):
    bitmap, pixels = office_quality_image()
    output = io.BytesIO()
    if fmt == "docx":
        docx = optional("docx")
        document = docx.Document()
        paragraph = document.add_paragraph()
        run = paragraph.add_run("科学数据高清预览 DataSeek 123")
        run.font.name = "Noto Sans CJK SC"
        run.font.size = docx.shared.Pt(8)
        run._element.get_or_add_rPr().rFonts.set(docx.oxml.ns.qn("w:eastAsia"), "Noto Sans CJK SC")
        borders = docx.oxml.OxmlElement("w:pBdr")
        border = docx.oxml.OxmlElement("w:bottom")
        for name, value in {"val": "single", "sz": "2", "color": "000000"}.items():
            border.set(docx.oxml.ns.qn("w:" + name), value)
        borders.append(border)
        paragraph._element.get_or_add_pPr().append(borders)
        document.add_picture(io.BytesIO(bitmap), width=docx.shared.Inches(1))
        document.save(output)
    else:
        pptx = optional("pptx")
        deck = pptx.Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        box = slide.shapes.add_textbox(pptx.util.Inches(1), pptx.util.Inches(1), pptx.util.Inches(7), pptx.util.Inches(1))
        run = box.text_frame.paragraphs[0].add_run()
        run.text = "科学数据高清预览 DataSeek 123"
        run.font.name = "Noto Sans CJK SC"
        run.font.size = pptx.util.Pt(8)
        east_asian = pptx.oxml.xmlchemy.OxmlElement("a:ea")
        east_asian.set("typeface", "Noto Sans CJK SC")
        run._r.get_or_add_rPr().append(east_asian)
        line = slide.shapes.add_connector(pptx.enum.shapes.MSO_CONNECTOR.STRAIGHT,
                                         pptx.util.Inches(1), pptx.util.Inches(2),
                                         pptx.util.Inches(6), pptx.util.Inches(2))
        line.line.width = pptx.util.Pt(0.25)
        slide.shapes.add_picture(io.BytesIO(bitmap), pptx.util.Inches(1), pptx.util.Inches(3), width=pptx.util.Inches(1))
        deck.save(output)
    return output.getvalue(), pixels


@pytest.mark.parametrize("fmt", ["docx", "pptx"])
def test_office_quality_preserves_chinese_fonts_vectors_and_image_pixels(fmt):
    executable("soffice")
    pypdf = optional("pypdf")
    data, pixels = office_quality_fixture(fmt)
    result = request(data, reader="office", kind="pdf", format=fmt)
    assert result["ok"] is True, result
    reader = pypdf.PdfReader(io.BytesIO(base64.b64decode(result["data"]["data_base64"])))
    assert len(reader.pages) == 1
    page = reader.pages[0]
    assert "科学数据高清预览" in page.extract_text()
    assert "DataSeek 123" in page.extract_text()
    fonts = [font.get_object() for font in page["/Resources"]["/Font"].values()]
    assert any("NotoSansCJK" in font["/BaseFont"] for font in fonts)
    for font in fonts:
        for descendant in font.get("/DescendantFonts", [font]):
            descriptor = descendant.get_object()["/FontDescriptor"]
            assert any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3"))
    operations = page.get_contents().operations
    assert any(operator == b"w" and 0 < float(args[0]) <= 0.3 for args, operator in operations)
    assert any(operator in (b"S", b"s") for _, operator in operations)
    # Impress may add a small line-shadow image and enumerate the same XObject
    # through multiple form resources. The source bitmap must still be intact.
    embedded = [image.image.convert("RGB") for image in page.images if image.image.size == (1200, 800)]
    assert embedded, "Do not downsample the 1,200 DPI source image"
    assert all(image.tobytes() == pixels for image in embedded), "Do not introduce lossy JPEG recompression"


def test_office_real_output_is_limited_to_first_100_pages():
    executable("soffice")
    docx = optional("docx")
    pypdf = optional("pypdf")
    document = docx.Document()
    for index in range(101):
        if index:
            document.add_page_break()
        document.add_paragraph(f"DataSeek page {index + 1:03d}")
    output = io.BytesIO()
    document.save(output)
    result = request(output.getvalue(), reader="office", kind="pdf", format="docx")
    assert result["ok"] is True, result
    pdf = pypdf.PdfReader(io.BytesIO(base64.b64decode(result["data"]["data_base64"])))
    assert len(pdf.pages) == 100
    assert "DataSeek page 100" in pdf.pages[-1].extract_text()
    assert result["data"]["sampled"] is True


@pytest.mark.parametrize("fmt,filter_name", [("docx", "writer_pdf_Export"), ("pptx", "impress_pdf_Export")])
def test_office_quality_parameters_are_fixed_in_cli_and_private_profile(monkeypatch, tmp_path, fmt, filter_name):
    pypdf = optional("pypdf")
    calls = []

    def convert(command, directory):
        from xml.etree import ElementTree
        calls.append(command)
        options = command[command.index("--convert-to") + 1]
        assert options.startswith("pdf:" + filter_name + ":")
        options = json.loads(options.split(":", 2)[2])
        assert options["PageRange"] == {"type": "string", "value": "1-100"}
        root = ElementTree.fromstring((directory / "profile/user/registrymodifications.xcu").read_text())
        namespace = "{http://openoffice.org/2001/registry}"
        by_path = {item.attrib[namespace + "path"]: {
            prop.attrib[namespace + "name"]: prop.find("value").text for prop in item
        } for item in root}
        profile_options = by_path["/org.openoffice.Office.Common/Filter/PDF/Export"]
        for name, value in worker.OFFICE_PDF_SETTINGS.items():
            assert options[name] == {"type": "boolean", "value": str(value).lower()}
            assert profile_options[name] == str(value).lower()
        security = by_path["/org.openoffice.Office.Common/Security/Scripting"]
        assert security["MacroSecurityLevel"] == "3"
        assert security["DisableMacrosExecution"] == security["DisableActiveContent"] == "true"
        assert security["BlockUntrustedRefererLinks"] == "true"
        assert by_path["/org.openoffice.Office.Writer/Content/Update"]["Link"] == "2"
        assert by_path["/org.openoffice.Office.Calc/Content/Update"]["Link"] == "0"
        assert (directory / ("input." + fmt)).stat().st_mode & 0o777 == 0o400
        pdf = pypdf.PdfWriter()
        pdf.add_blank_page(width=72, height=72)
        pdf.write(directory / "output/input.pdf")

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/soffice")
    monkeypatch.setattr(worker, "_process", convert)
    data = docx_fixture() if fmt == "docx" else zip_bytes({"ppt/presentation.xml": "<presentation/>"})
    result = worker.office_preview(data, fmt, tmp_path)
    assert len(calls) == 1
    assert result["metadata"]["image_compression"] == "lossless"
    assert result["metadata"]["image_downsampling"] is False
    assert result["metadata"]["standard_font_embedding"] == "requested"


def test_office_pdf_size_guard_precedes_parser(monkeypatch):
    pypdf = optional("pypdf")
    monkeypatch.setattr(worker, "MAX_MEDIA_BYTES", 16)
    monkeypatch.setattr(pypdf, "PdfReader", lambda *_a, **_k: pytest.fail("Oversized PDF reached parser"))
    with pytest.raises(worker.PreviewError, match="5 MiB.*未降低清晰度"):
        worker._office_pdf_pages(b"%PDF-" + b"x" * 16)


def test_office_pdf_page_tree_has_an_additional_parse_budget():
    pypdf = optional("pypdf")
    pdf = pypdf.PdfWriter()
    pdf.add_blank_page(width=72, height=72)
    pdf._root_object["/Pages"][pypdf.generic.NameObject("/Count")] = pypdf.generic.NumberObject(worker.MAX_OFFICE_PDF_PAGES + 1)
    output = io.BytesIO()
    pdf.write(output)
    with pytest.raises(worker.PreviewError, match="页数超过安全解析预算"):
        worker._office_pdf_pages(output.getvalue())


def test_office_pdf_limit_preserves_image_pixels_and_vector_operations():
    pypdf = optional("pypdf")
    executable("soffice")
    data, pixels = office_quality_fixture("docx")
    result = request(data, reader="office", kind="pdf", format="docx")
    assert result["ok"] is True, result
    source = pypdf.PdfReader(io.BytesIO(base64.b64decode(result["data"]["data_base64"])))
    original_operations = source.pages[0].get_contents().operations
    writer = pypdf.PdfWriter()
    for _ in range(101):
        writer.add_page(source.pages[0])
    output = io.BytesIO()
    writer.write(output)
    limited, pages, truncated = worker._office_pdf_pages(output.getvalue())
    assert pages == 100 and truncated is True
    final = pypdf.PdfReader(io.BytesIO(limited))
    assert len(final.pages) == 100
    assert final.pages[0].get_contents().operations == original_operations
    assert final.pages[-1].images[0].image.convert("RGB").tobytes() == pixels


@pytest.mark.parametrize("data", [b"%PDF-invalid", b"not a pdf"])
def test_office_pdf_parse_failure_is_safe(data):
    optional("pypdf")
    with pytest.raises(worker.PreviewError, match="安全预算内解析"):
        worker._office_pdf_pages(data)


def test_office_real_docx_to_pdf():
    executable("soffice")
    result = request(docx_fixture(), reader="office", kind="pdf", format="docx")
    assert result["ok"] is True, result
    payload = result["data"]
    assert base64.b64decode(payload["data_base64"]).startswith(b"%PDF-")
    assert payload["metadata"]["macros"] == "disabled"
    assert payload["metadata"]["page_limit"] == 100


def test_office_real_pptx_to_pdf():
    executable("soffice")
    presentation = optional("pptx")
    deck = presentation.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[0])
    slide.shapes.title.text = "DataSeek preview fixture"
    stream = io.BytesIO()
    deck.save(stream)
    result = request(stream.getvalue(), reader="office", kind="pdf", format="pptx")
    assert result["ok"] is True, result
    assert base64.b64decode(result["data"]["data_base64"]).startswith(b"%PDF-")


@pytest.mark.parametrize("fmt", ["odt", "odp"])
def test_office_real_odf_to_pdf(fmt):
    executable("soffice")
    prefix = '<office:document-content office:version="1.2" xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"><office:body>'
    body = '<office:text><text:p>DataSeek synthetic ODT</text:p></office:text>' if fmt == "odt" else '<office:presentation><draw:page draw:name="page1"><draw:frame svg:x="1cm" svg:y="1cm" svg:width="10cm" svg:height="2cm"><draw:text-box><text:p>DataSeek synthetic ODP</text:p></draw:text-box></draw:frame></draw:page></office:presentation>'
    mime = "application/vnd.oasis.opendocument." + ("text" if fmt == "odt" else "presentation")
    manifest = '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2"><manifest:file-entry manifest:full-path="/" manifest:media-type="' + mime + '"/><manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/></manifest:manifest>'
    data = zip_bytes({"mimetype": mime, "META-INF/manifest.xml": manifest, "content.xml": prefix + body + "</office:body></office:document-content>"})
    result = request(data, reader="office", kind="pdf", format=fmt)
    assert result["ok"] is True, result
    assert base64.b64decode(result["data"]["data_base64"]).startswith(b"%PDF-")


@pytest.mark.parametrize("fmt", ["docx", "pptx", "odt", "odp"])
def test_office_rejects_external_links_before_conversion(fmt):
    executable("soffice")
    data = zip_bytes({"_rels/.rels": '<Relationships><Relationship TargetMode="External" Target="file:///etc/passwd"/></Relationships>'})
    result = request(data, reader="office", kind="pdf", format=fmt)
    assert result["ok"] is False and "外部链接" in result["error"]


def test_office_missing_dependency_is_not_fake_success(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    result = request(docx_fixture(), reader="office", kind="pdf", format="docx")
    assert result["ok"] is False and "LibreOffice" in result["error"]


def test_fixed_process_has_timeout_and_kills_child_group(tmp_path):
    with pytest.raises(worker.PreviewError, match="时间预算"):
        worker._process([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, timeout=.1)


@pytest.mark.parametrize("options", [{}, {"confirm": False}, {"confirm": 1}, {"confirm": True, "command": "fastqc"}])
def test_fastqc_requires_explicit_action(options):
    # True must not be conflated with JSON's integer 1.
    result = request(b"@x\nACGT\n+\nIIII\n", reader="fastqc", kind="report", format="fastq", options=options)
    assert result["ok"] is False


def test_fastqc_real_complete_file_and_multiqc_summary():
    executable("fastqc")
    executable("multiqc")
    data = b"".join(f"@read{i}\n".encode() + b"ACGT" * 25 + b"\n+\n" + b"I" * 100 + b"\n" for i in range(500))
    result = request(data, reader="fastqc", kind="report", format="fastq", options={"confirm": True})
    assert result["ok"] is True, result
    payload = result["data"]
    assert payload["metadata"]["engines"] == ["FastQC", "MultiQC"]
    assert payload["metadata"]["complete_input"] is True
    basic = next(s for s in payload["sections"] if s["name"] == "Basic Statistics")
    assert ["Total Sequences", "500"] in basic["rows"]
    assert payload["table"]["rows"]
    assert "html" not in payload and "data_base64" not in payload


def test_fastqc_report_parser_never_exports_html():
    sections = worker._fastqc_sections(">>Basic Statistics\tpass\n#Measure\tValue\nTotal Sequences\t12\n>>END_MODULE\n")
    assert sections == [{"name": "Basic Statistics", "status": "pass", "columns": ["Measure", "Value"], "rows": [["Total Sequences", "12"]]}]


def test_media_budget_is_enforced_before_encoding():
    with pytest.raises(worker.PreviewError):
        worker._media({}, b"x" * (worker.MAX_MEDIA_BYTES + 1), "image/png")


def test_native_subprocess_stdout_is_one_json_envelope():
    header = {"contract_version": 2, "reader": "tabular", "kind": "table", "format": "csv", "size": 8}
    process = subprocess.run([sys.executable, "-m", "app.services.extended_visualization_worker"],
                             input=json.dumps(header).encode() + b"\n" + b"x,y\n1,2\n",
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
                             env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])})
    assert process.returncode == 0
    result = json.loads(process.stdout)
    assert result["ok"] is True
    assert result["data"]["table"]["rows"] == [["1", "2"]]
    assert b"Traceback" not in process.stdout


def root_bytes(tmp_path, setup):
    uproot = optional("uproot")
    path = tmp_path / "fixture.root"
    with uproot.recreate(path) as root:
        setup(root, uproot)
    return path.read_bytes()


def test_root_real_histogram_preserves_nonuniform_bin_edges(tmp_path):
    def setup(root, _):
        root["detector/counts"] = (np.array([1., 4., 9.]), np.array([0., 1., 3., 7.]))
        root["description"] = "This TObjString is not executed or rendered."
    data = root_bytes(tmp_path, setup)
    result = request(data, reader="root", kind="series", format="root")["data"]
    assert result["metadata"]["root_class"] == "TH1D"
    assert result["metadata"]["x_edges"] == [0, 1, 3, 7]
    assert result["array"] == {"shape": [3], "dimensions": ["x_bin"], "values": [1, 4, 9]}
    assert result["table"]["rows"] == [[.5, 1], [2, 4], [5, 9]]
    assert result["selected"]["path"] == "/detector/counts"
    assert any(n["node_type"] == "blocked-object" for n in result["tree"])
    assert request(data, reader="root", kind="series", format="root", options={"path": "/description"})["ok"] is False


def test_root_real_th2_transpose_and_axes(tmp_path):
    def setup(root, _):
        root["density"] = (np.arange(6, dtype=float).reshape(2, 3), np.array([0., 1., 4.]), np.array([-2., 0., 3., 8.]))
    data = root_bytes(tmp_path, setup)
    result = request(data, reader="root", kind="heatmap", format="root")["data"]
    assert result["metadata"]["root_class"] == "TH2D"
    assert result["array"]["shape"] == [3, 2]
    assert result["array"]["values"] == [0, 3, 1, 4, 2, 5]
    assert result["metadata"]["x_edges"] == [0, 1, 4]
    assert result["metadata"]["y_edges"] == [-2, 0, 3, 8]
    assert request(data, reader="root", kind="series", format="root")["ok"] is False


def test_root_real_tgraph_preserves_coordinates(tmp_path):
    def setup(root, uproot):
        root["curve"] = uproot.as_TGraph({"x": np.array([1., 3., 4.]), "y": np.array([2., 7., 5.])})
    result = request(root_bytes(tmp_path, setup), reader="root", kind="series", format="root")["data"]
    assert result["metadata"]["root_class"] == "TGraph"
    assert result["array"] == {"shape": [3, 2], "dimensions": ["point", "coordinate"], "values": [1, 2, 3, 7, 4, 5]}


def test_root_larger_histogram_uses_array_not_unbounded_table(tmp_path):
    def setup(root, _):
        root["counts"] = (np.arange(500, dtype=float), np.arange(501, dtype=float))
    result = request(root_bytes(tmp_path, setup), reader="root", kind="series", format="root")["data"]
    assert result["array"]["shape"] == [500]
    assert "table" not in result
    assert len(result["metadata"]["x_edges"]) == 501


def test_root_does_not_sample_or_invent_wider_bin_counts(tmp_path):
    def setup(root, _):
        root["counts"] = (np.arange(9000, dtype=float), np.arange(9001, dtype=float))
    assert request(root_bytes(tmp_path, setup), reader="root", kind="series", format="root")["ok"] is False


JCAMP = b"""##TITLE=DataSeek synthetic processed spectrum
##JCAMP-DX=5.00
##DATA TYPE=NMR SPECTRUM
##.OBSERVE NUCLEUS=1H
##.OBSERVE FREQUENCY=400
##XUNITS=PPM
##YUNITS=ARBITRARY UNITS
##NPOINTS=4
##FIRSTX=4
##LASTX=1
##XFACTOR=1
##YFACTOR=0.5
##XYDATA=(X++(Y..Y))
4 0 4
2 8 0
##END=
"""


def test_jcamp_real_affn_data_scaled_and_axis_preserved():
    result = request(JCAMP, reader="jcamp", kind="series", format="jdx")["data"]
    assert result["table"]["rows"] == [[4, 0], [3, 2], [2, 4], [1, 0]]
    assert result["array"]["values"] == [4, 0, 3, 2, 2, 4, 1, 0]
    assert result["metadata"]["nucleus"] == "1H"
    assert result["metadata"]["frequency"] == 400
    assert result["metadata"]["is_fid"] is False


def test_jcamp_real_pac_and_xy_pairs():
    data = JCAMP.replace(b"##XYDATA=(X++(Y..Y))\n4 0 4\n2 8 0", b"##XYPOINTS=(XY..XY)\n4+0,3+4;2+8,1+0")
    result = request(data, reader="jcamp", kind="series", format="dx")["data"]
    assert result["table"]["rows"] == [[4, 0], [3, 2], [2, 4], [1, 0]]


@pytest.mark.parametrize("old,new", [
    (b"NMR SPECTRUM", b"NMR FID"), (b"PPM", b"HZ"),
    (b"##.OBSERVE NUCLEUS=1H", b"##.OBSERVE NUCLEUS=unknown"),
    (b"##NPOINTS=4", b"##NPOINTS=8193"), (b"##NPOINTS=4", b"##NPOINTS=3"),
    (b"##NPOINTS=4", b"##NPOINTS=4.5"), (b"4 0 4", b"4 @AC"),
    (b"2 8 0", b"1 8 0"), (b"##END=", b""), (b"##END=", b"##END=\n##TITLE=second"),
    (b"##YFACTOR=0.5", b"##YFACTOR=0"),
    (b"##XUNITS=PPM", b"##XUNITS=PPM\n##NTUPLES=NMR SPECTRUM"),
    (b"##XUNITS=PPM", b"##XUNITS=PPM\n##XUNITS=PPM"),
    (b"##.OBSERVE FREQUENCY=400", b"##.OBSERVE FREQUENCY=-1"),
])
def test_jcamp_rejects_unsupported_ambiguous_or_corrupt_spectra(old, new):
    assert request(JCAMP.replace(old, new), reader="jcamp", kind="series", format="jdx")["ok"] is False


def test_jcamp_more_than_200_points_remain_lossless_array():
    count = 300
    data = ("##TITLE=fixture\n##DATA TYPE=NMR SPECTRUM\n##.OBSERVE NUCLEUS=13C\n##XUNITS=PPM\n"
            f"##NPOINTS={count}\n##XYPOINTS=(XY..XY)\n" + "\n".join(f"{i} {i % 3}" for i in range(count)) + "\n##END=\n").encode()
    result = request(data, reader="jcamp", kind="series", format="jdx")["data"]
    assert result["array"]["shape"] == [300, 2]
    assert result["array"]["values"][-2:] == [299, 2]
    assert "table" not in result
    assert result["sampled"] is False
