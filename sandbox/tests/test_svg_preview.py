"""Real offline SVG previews and the same static-content policy as delivery."""
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import random
import subprocess
import sys
import threading

import pytest
from PIL import Image

from app.services import extended_visualization_worker as worker
from app.services import svg_render
from app.services.svg_preview import MAX_SVG_INPUT_BYTES


@pytest.fixture(scope="module")
def backend_validator():
    path = Path(__file__).resolve().parents[2] / "backend/app/application/services/svg_visualization.py"
    spec = importlib.util.spec_from_file_location("svg_preview_host_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_svg_preview_payload


def svg(body, *, width=320, height=160):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
            + body + '</svg>').encode()


def preview(content):
    result = worker.preview_bytes(content, "svg", "image", {}, format="svg")
    assert set(result) == {"contract_version", "type", "reader", "kind", "media_type",
                           "data_base64", "metadata", "warnings", "sampled"}
    assert result["type"] == result["reader"] == "svg"
    assert result["kind"] == "image" and result["media_type"] == "image/png"
    assert result["warnings"] == [] and result["sampled"] is False
    assert set(result["metadata"]) == {"width", "height", "source_format", "rasterized"}
    assert result["metadata"]["source_format"] == "SVG" and result["metadata"]["rasterized"] is True
    png = base64.b64decode(result["data_base64"], validate=True)
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) <= svg_render.MAX_PREVIEW_PNG_BYTES
    with Image.open(io.BytesIO(png)) as image:
        image.load()
        assert image.size == (result["metadata"]["width"], result["metadata"]["height"])
        assert max(image.size) <= 2048
        assert image.convert("RGBA").getchannel("A").getbbox() is not None
    return png, result


def test_css_glyphs_and_local_references_are_rendered_without_modifying_source():
    content = svg('<style>.plot{fill:red} text{font-family:sans-serif;font-size:20px}</style>'
                  '<defs><path id="mark" d="M 0 0 L 40 0 L 40 40 Z"/></defs>'
                  '<use href="#mark" class="plot"/>'
                  '<text x="60" y="40">Scientific SVG 2026</text>')
    digest = hashlib.sha256(content).hexdigest()
    png, _ = preview(content)
    assert b"Scientific SVG" not in png
    assert hashlib.sha256(content).hexdigest() == digest
    with Image.open(io.BytesIO(png)).convert("RGBA") as image:
        # Local CSS and text are real visible pixels, not metadata-only success.
        assert image.getpixel((20, 8))[0] > 200
        assert image.crop((180, 30, 900, 135)).getchannel("A").getbbox() is not None


def test_real_renderer_png_matches_host_contract_and_supersamples_small_plots(backend_validator):
    _, result = preview(svg('<text x="10" y="40" font-size="24">Scientific SVG</text>'
                            '<rect x="10" y="60" width="60" height="20" fill="navy"/>'))
    assert result["metadata"]["width"] == 960
    assert result["metadata"]["height"] == 480
    assert backend_validator(result, kind="image", limit=8 * 1024 * 1024) == result


def test_matplotlib_doctype_local_font_paths_and_embedded_bitmap_render(backend_validator):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    figure, axes = plt.subplots(figsize=(4, 2))
    try:
        axes.imshow([[1, 2], [3, 4]], extent=(0, 1, 0, 1))
        axes.plot([0, .5, 1], [0, .7, .3], label="Measured signal")
        axes.set_title("Scientific plot α")
        axes.legend()
        saved = io.BytesIO()
        with matplotlib.rc_context({"svg.fonttype": "path"}):
            figure.savefig(saved, format="svg")
        source = saved.getvalue()
        assert b"<!DOCTYPE svg" in source and b"<use" in source and b"data:image/png" in source
        _, result = preview(source)
        assert backend_validator(result, kind="image", limit=8 * 1024 * 1024) == result
    finally:
        plt.close(figure)


@pytest.mark.parametrize("attribute", ["href", "xlink:href", "alias:href"])
def test_local_glyph_reference_is_visible_regardless_of_source_namespace_prefix(attribute, backend_validator):
    source = ('<svg xmlns="http://www.w3.org/2000/svg" '
              'xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:alias="http://www.w3.org/1999/xlink" '
              'width="100" height="100"><defs><path id="glyph" d="M 10 10 H 40 V 40 H 10 Z"/></defs>'
              f'<use {attribute}="#glyph" fill="red"/></svg>').encode()
    png, result = preview(source)
    assert backend_validator(result, kind="image", limit=8 * 1024 * 1024) == result
    with Image.open(io.BytesIO(png)).convert("RGBA") as image:
        assert image.getpixel((60, 60)) == (255, 0, 0, 255)


@pytest.mark.parametrize("attribute", ["href", "xlink:href", "alias:href"])
def test_embedded_png_paints_pixels_through_all_supported_reference_spellings(attribute, backend_validator):
    raster = io.BytesIO()
    Image.new("RGB", (4, 4), (0, 80, 220)).save(raster, format="PNG")
    source = ('<svg xmlns="http://www.w3.org/2000/svg" '
              'xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:alias="http://www.w3.org/1999/xlink" '
              'width="100" height="100"><rect width="100" height="100" fill="white"/>'
              f'<image x="10" y="10" width="40" height="40" {attribute}="data:image/png;base64,'
              + base64.b64encode(raster.getvalue()).decode() + '"/></svg>').encode()
    png, result = preview(source)
    assert backend_validator(result, kind="image", limit=8 * 1024 * 1024) == result
    with Image.open(io.BytesIO(png)).convert("RGBA") as image:
        assert image.getpixel((60, 60)) == (0, 80, 220, 255)


@pytest.mark.parametrize("attributes", [
    'href="#glyph" xlink:href="#other"',
    'href="#glyph" xlink:href="https://example.invalid/image.svg"',
    'xlink:href="https://example.invalid/image.svg"',
    'xlink:href="file:///private/image.svg"',
    'unknown:href="#glyph" xmlns:unknown="urn:not-xlink"',
])
def test_reference_normalization_rejects_conflicts_unknown_namespaces_and_external_resources(attributes, monkeypatch):
    monkeypatch.setattr(svg_render.subprocess, "Popen", lambda *_a, **_k: pytest.fail("unsafe native input"))
    source = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
              'width="100" height="100"><defs><path id="glyph" d="M 10 10 H 40 V 40 Z"/>'
              '<path id="other" d="M 50 50 H 80 V 80 Z"/></defs>'
              f'<use {attributes}/></svg>').encode()
    with pytest.raises(worker.PreviewError):
        worker.preview_bytes(source, "svg", "image", {}, format="svg")


def test_same_duplicate_svg1_svg2_reference_preserves_the_glyph():
    source = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
              'width="100" height="100"><defs><path id="glyph" d="M 10 10 H 40 V 40 H 10 Z"/></defs>'
              '<use href="#glyph" xlink:href="#glyph" fill="red"/></svg>').encode()
    png, _ = preview(source)
    with Image.open(io.BytesIO(png)).convert("RGBA") as image:
        assert image.getpixel((60, 60)) == (255, 0, 0, 255)


def test_matplotlib_path_title_ticks_units_and_legend_have_real_ink(backend_validator):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    figure, axes = plt.subplots(figsize=(6, 4), dpi=72)
    try:
        figure.subplots_adjust(left=.2, right=.95, bottom=.2, top=.8)
        axes.plot([0, 1, 2], [0, 2, 1], label="Signal legend", color="red")
        texts = [axes.set_title("Visible scientific title 123"), axes.set_xlabel("Time [seconds]"),
                 axes.set_ylabel("Response [mV]")]
        axes.set_xticks([0, 1, 2], labels=["0", "1", "2"])
        axes.set_yticks([0, 1, 2], labels=["0", "1", "2"])
        texts.extend(axes.get_xticklabels() + axes.get_yticklabels())
        texts.extend(axes.legend(loc="upper left").get_texts())
        figure.canvas.draw()
        boxes = [text.get_window_extent(figure.canvas.get_renderer()).bounds for text in texts]
        source = io.BytesIO()
        with matplotlib.rc_context({"svg.fonttype": "path"}):
            figure.savefig(source, format="svg")
        assert b"xlink:href" in source.getvalue()
        png, result = preview(source.getvalue())
        assert backend_validator(result, kind="image", limit=8 * 1024 * 1024) == result
        with Image.open(io.BytesIO(png)).convert("RGBA") as image:
            scale = image.width / figure.bbox.width
            for text, (x, y, width, height) in zip(texts, boxes):
                region = image.crop((int(x * scale), int((figure.bbox.height - y - height) * scale),
                                     int((x + width) * scale), int((figure.bbox.height - y) * scale)))
                # White chart background is not evidence that a label painted.
                dark = sum(1 for r, g, b, a in region.getdata() if a > 128 and max(r, g, b) < 128)
                assert dark >= 10, f"Missing rendered label: {text.get_text()}"
    finally:
        plt.close(figure)


def test_large_svg_is_rasterized_to_bounded_preview_not_browser_content():
    _, result = preview(svg('<rect width="4000" height="3000" fill="navy"/>', width=4000, height=3000))
    assert result["metadata"]["width"] == 2048
    assert result["metadata"]["height"] == 1536


def test_registered_synthetic_fixture_renders_and_matches_host_contract(backend_validator):
    path = Path(__file__).parent / "fixtures/svg/viz-test-svg/static-chart.svg"
    _, result = preview(path.read_bytes())
    assert backend_validator(result, kind="image", limit=8 * 1024 * 1024) == result


def test_embedded_png_larger_than_pipe_buffer_finishes_without_deadlock():
    raster = Image.frombytes("RGB", (256, 256), random.Random(42).randbytes(256 * 256 * 3))
    encoded = io.BytesIO()
    raster.save(encoded, format="PNG")
    content = svg('<image width="256" height="256" href="data:image/png;base64,'
                  + base64.b64encode(encoded.getvalue()).decode() + '"/>', width=256, height=256)
    png, _ = preview(content)
    assert len(png) > 65536


@pytest.mark.parametrize("body", [
    '<script>alert(1)</script><rect width="20" height="20"/>',
    '<rect width="20" height="20" onclick="alert(1)"/>',
    '<foreignObject><div xmlns="http://www.w3.org/1999/xhtml">unsafe</div></foreignObject>',
    '<image width="20" height="20" href="https://example.invalid/private.png"/>',
    '<image width="20" height="20" href="file:///private/secret.png"/>',
    '<use href="javascript:alert(1)"/>',
    '<style>@import "https://example.invalid/style.css";</style><rect width="20" height="20"/>',
    '<rect width="20" height="20" fill="url(https://example.invalid/x)"/>',
    '<rect width="20" height="20"><animate attributeName="x"/></rect>',
    '<defs><g id="a"><use href="#a"/></g></defs><use href="#a"/>',
    '<rect width="20" height="20" style="display:none"/>',
])
def test_unsafe_or_invisible_svg_is_rejected_before_native_rendering(body, monkeypatch):
    monkeypatch.setattr(svg_render.subprocess, "Popen", lambda *_a, **_k: pytest.fail("unsafe native input"))
    with pytest.raises(worker.PreviewError) as failure:
        worker.preview_bytes(svg(body), "svg", "image", {}, format="svg")
    assert "example.invalid" not in str(failure.value) and "/private/" not in str(failure.value)


@pytest.mark.parametrize("content", [
    b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg><text>&x;</text></svg>',
    b'<?xml-stylesheet href="https://example.invalid/style.css"?><svg/>',
    b'<html><body>not SVG</body></html>', b'<svg><path', b'',
])
def test_xml_or_format_attacks_never_become_preview_images(content):
    with pytest.raises(worker.PreviewError):
        worker.preview_bytes(content, "svg", "image", {}, format="svg")


@pytest.mark.parametrize("options", [{"url": "https://example.invalid/a.svg"}, {"path": "/private/x"},
                                     {"script": "print(1)"}, {"width": 100000}])
def test_reader_has_no_paths_urls_scripts_or_unbounded_render_options(options):
    with pytest.raises(worker.PreviewError):
        worker.preview_bytes(svg('<rect width="20" height="20"/>'), "svg", "image", options, format="svg")


def test_input_budget_is_checked_in_header_before_source_read():
    class HeaderOnly(io.BytesIO):
        def read(self, *_args):
            pytest.fail("oversized source read")
    header = {"contract_version": 2, "size": MAX_SVG_INPUT_BYTES + 1,
              "reader": "svg", "kind": "image", "format": "svg", "options": {}}
    result = json.loads(worker._encoded_result(HeaderOnly(json.dumps(header).encode() + b"\n")))
    assert result["ok"] is False


def test_preview_protocol_returns_png_not_svg_or_raw_xml():
    source = svg('<rect width="20" height="20" fill="red"/>')
    header = {"contract_version": 2, "size": len(source), "reader": "svg", "kind": "image",
              "format": "svg", "options": {}}
    result = worker._encoded_result(io.BytesIO(json.dumps(header).encode() + b"\n" + source))
    assert b"<svg" not in result and b"image/svg+xml" not in result
    assert json.loads(result)["data"]["media_type"] == "image/png"


def test_preview_cancel_reaps_renderer_and_closes_anonymous_files(monkeypatch):
    actual = subprocess.Popen
    children, files = [], []
    def stalled(command, **kwargs):
        assert len(command) == 4 and kwargs["pass_fds"] == (int(command[3]),)
        files.append(kwargs["stdin"])
        child = actual([sys.executable, "-c", "import time; time.sleep(20)"], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(svg_render.subprocess, "Popen", stalled)
    stop = threading.Event()
    timer = threading.Timer(.1, stop.set)
    timer.start()
    def check():
        if stop.is_set():
            raise ValueError("cancelled")
    try:
        with pytest.raises(ValueError, match="cancelled"):
            svg_render.render_svg_png(b"<svg/>", max_pixels=100, check=check)
        assert children and all(child.poll() is not None and child.stdout.closed for child in children)
        assert all(source.closed for source in files)
    finally:
        timer.cancel()
