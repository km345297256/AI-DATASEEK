"""SVG charts are deliverable only after safe static parsing and rendering."""
import base64
import hashlib
import io
import json

import pytest
from PIL import Image

from app.services import artifact_validation as validation
from app.services import svg_validation


def receipt(tmp_path, source):
    root = tmp_path.resolve()
    path = root / "chart.svg"
    path.write_bytes(source.encode() if isinstance(source, str) else source)
    return validation.validate_artifacts([{"path": str(path), "kind": "image"}], root=root)["files"][0]


def svg(body, attributes=""):
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" {attributes}>{body}</svg>'


def test_generated_matplotlib_svg_with_standard_doctype_and_local_glyphs(tmp_path, monkeypatch):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    # Validation must not resolve even the standard W3C DTD URL.
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("network access"))
    figure, axes = plt.subplots(figsize=(4, 2))
    axes.plot([1, 2, 3], [2, 1, 4], label="observed")
    axes.set_title("Measured data")
    axes.legend()
    output = io.BytesIO()
    figure.savefig(output, format="svg")
    plt.close(figure)
    data = output.getvalue()
    assert b"<!DOCTYPE svg PUBLIC" in data and b"<use" in data
    result = receipt(tmp_path, data)
    assert result["valid"], result
    assert result["metadata"]["format"] == "SVG"
    assert result["metadata"]["validation_level"] == "safe_static_svg_render"
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert "Measured data" not in json.dumps(result)


def test_embedded_png_and_local_gradient_are_supported(tmp_path):
    image = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(image, format="PNG")
    encoded = base64.b64encode(image.getvalue()).decode()
    result = receipt(tmp_path, svg(
        '<defs><linearGradient id="gradient"><stop stop-color="blue"/></linearGradient></defs>'
        '<rect width="100" height="100" fill="url(#gradient)"/>'
        f'<image x="100" width="100" height="100" href="data:image/png;base64,{encoded}"/>'
    ))
    assert result["valid"], result


@pytest.mark.parametrize("body", [
    '<script>alert(1)</script><rect width="10" height="10"/>',
    '<rect width="10" height="10" onclick="alert(1)"/>',
    '<foreignObject><div xmlns="http://www.w3.org/1999/xhtml">x</div></foreignObject>',
    '<image href="https://example.com/private.png" width="10" height="10"/>',
    '<image href="file:///private/data.png" width="10" height="10"/>',
    '<image href="../dataset/private.png" width="10" height="10"/>',
    '<use href="javascript:alert(1)"/>',
    '<style>@import "https://example.com/style.css";</style><rect width="10" height="10"/>',
    '<rect width="10" height="10" style="fill:url(https://example.com/color)"/>',
    '<rect width="10" height="10" fill="u\\72l(https://example.com/color)"/>',
    '<rect width="10" height="10" fill="url/**/(https://example.com/color)"/>',
    '<rect width="10" height="10"><set attributeName="fill" to="url(https://example.com/x)"/></rect>',
    '<defs><path id="loop" d="M0 0 L5 5"/><use id="loop" href="#loop"/></defs><use href="#loop"/>',
    '<defs><g id="cycle"><use href="#cycle"/></g></defs><use href="#cycle"/>',
    '<use href="#missing"/>',
])
def test_active_external_or_invalid_reference_content_is_rejected(tmp_path, body):
    result = receipt(tmp_path, svg(body))
    assert not result["valid"]
    assert result["reason"] in {"invalid_content", "image_size_limit"}
    assert "example.com" not in json.dumps(result)


@pytest.mark.parametrize("source", [
    '<!DOCTYPE svg [<!ENTITY x "expanded">]>' + svg('<text>&x;</text>'),
    '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]>' + svg('<text>&x;</text>'),
    '<?xml-stylesheet href="https://example.com/style.css"?>' + svg('<rect width="10" height="10"/>'),
    svg('<rect width="10" height="10"/>', 'xml:base="https://example.com/"'),
    '<html><svg><rect/></svg></html>',
    '<svg><path',
    svg(''),
    svg('<defs><path d="M0 0 L100 100"/></defs>'),
    svg('<rect width="10" height="10" visibility="hidden"/>'),
])
def test_unsafe_xml_wrong_format_and_empty_drawing_fail(tmp_path, source):
    result = receipt(tmp_path, source)
    assert not result["valid"]


def test_svg_structure_and_raster_limits_are_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(svg_validation, "MAX_ELEMENTS", 3)
    assert receipt(tmp_path, svg('<g><g><rect width="10" height="10"/></g></g>'))["reason"] == "image_size_limit"
    monkeypatch.setattr(svg_validation, "MAX_ELEMENTS", 100_000)
    monkeypatch.setattr(validation, "MAX_IMAGE_PIXELS", 100)
    assert receipt(tmp_path, svg('<rect width="10" height="10"/>'))["reason"] == "image_size_limit"


def test_missing_svg_remains_missing_artifact(tmp_path):
    root = tmp_path.resolve()
    result = validation.validate_artifacts([{"path": str(root / "absent.svg"), "kind": "image"}], root=root)["files"][0]
    assert result["kind"] == "image"
    assert result["reason"] == "missing_artifact"
    assert not result["valid"]


def test_svg_cannot_be_passed_off_as_a_report(tmp_path):
    root = tmp_path.resolve()
    path = root / "chart.svg"
    path.write_text(svg('<rect width="10" height="10"/>'))
    result = validation.validate_artifacts([{"path": str(path), "kind": "report"}], root=root)["files"][0]
    assert result["reason"] == "kind_mismatch"
