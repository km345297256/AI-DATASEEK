"""Small synthetic SVGs: browser/render parity, local assets and hard cleanup."""
import base64
import io
import subprocess
import sys
import threading
import time

import pytest
from PIL import Image

from app.services import artifact_validation as validation
from app.services import svg_render
from app.services.svg_styles import local_reference, xml_name
from test_svg_artifact_validation import receipt, svg


@pytest.mark.parametrize("rule", ["rect { display:none }", "svg { opacity:0 }",
    "#plot { visibility:hidden }", ".plot { opacity:0% }", "* { opacity: -1 }"])
def test_stylesheet_hidden_drawing_is_not_published_as_visible(tmp_path, rule):
    result = receipt(tmp_path, svg(f'<style>{rule}</style><rect id="plot" class="plot" width="100" height="100"/>'))
    assert not result["valid"] and result["reason"] == "invalid_content"


@pytest.mark.parametrize("body", [
    '<style>rect {display:none} .visible {display:inline}</style><rect class="visible" width="100" height="100"/>',
    '<style>#plot {display:none!important}</style><rect id="plot" style="display:inline!important" width="100" height="100"/>',
    '<g visibility="hidden"><rect visibility="visible" width="100" height="100"/></g>',
    '<defs><path id="曲线" d="M0 0 L100 100" stroke="red"/></defs><use href="#曲线"/>',
    '<defs><linearGradient id="渐变"><stop stop-color="blue"/></linearGradient></defs><rect width="100" height="100" fill="url(#渐变)"/>',
])
def test_supported_css_cascade_visibility_override_and_unicode_ids_render(tmp_path, body):
    result = receipt(tmp_path, svg(body))
    assert result["valid"], result


def test_important_rule_wins_over_nonimportant_inline_style(tmp_path):
    result = receipt(tmp_path, svg('<style>#plot {display:none!important}</style>'
        '<rect id="plot" style="display:inline" width="100" height="100"/>'))
    assert not result["valid"]


@pytest.mark.parametrize("style", [
    'background-image:image-set("https://example.invalid/image.png" 1x)',
    'fill:image-set("https://example.invalid/image.png" 1x)',
    'fill:-webkit-image-set("https://example.invalid/image.png" 1x)',
    'fill:var(--untrusted)', 'fill:expression(alert(1))', 'fill:url("#missing)',
])
def test_unsupported_resource_functions_and_bad_quoting_fail_closed(tmp_path, style):
    import html
    result = receipt(tmp_path, svg('<rect width="100" height="100" style="' + html.escape(style, quote=True) + '"/>'))
    assert not result["valid"]
    assert result["reason"] == "invalid_content"


@pytest.mark.parametrize("selector", ["rect:hover", "g > rect", "[href]", "rect + path", "@media screen"])
def test_styles_outside_explicit_static_subset_are_not_silently_ignored(tmp_path, selector):
    assert not receipt(tmp_path, svg(f'<style>{selector} {{display:none}}</style><rect width="100" height="100"/>'))["valid"]


@pytest.mark.parametrize("format", ["PNG", "JPEG", "GIF", "WEBP"])
def test_static_embedded_formats_render_without_logging_original_bytes(tmp_path, capfd, format):
    image = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(image, format=format)
    encoded = base64.b64encode(image.getvalue()).decode()
    result = receipt(tmp_path, svg(f'<image width="100" height="100" href="data:image/{format.lower()};base64,{encoded}"/>'))
    assert result["valid"], result
    captured = capfd.readouterr()
    assert encoded not in captured.out + captured.err
    assert "data:image" not in captured.out + captured.err


def test_embedded_raster_mime_mismatch_is_not_a_size_limit(tmp_path):
    image = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(image, format="JPEG")
    result = receipt(tmp_path, svg('<image width="100" height="100" href="data:image/png;base64,'
        + base64.b64encode(image.getvalue()).decode() + '"/>'))
    assert result["reason"] == "format_mismatch"


@pytest.mark.parametrize("value", ["", "bad id", "https://example.invalid/x", "../x", r"escaped\id", "%20"])
def test_local_references_do_not_accept_empty_url_or_escaped_ids(value):
    assert not xml_name(value)
    assert not local_reference("#" + value)


def test_xml_name_accepts_non_ascii_letters_and_combining_characters():
    assert xml_name("曲线") and xml_name("échelle") and xml_name("a\u0300")
    assert local_reference("#曲线")


@pytest.mark.parametrize("cancel", [False, True])
def test_renderer_timeout_or_cancel_terminates_and_reaps_native_worker(monkeypatch, cancel):
    actual_popen = subprocess.Popen
    children = []
    def stalled(command, **kwargs):
        assert command[0] == sys.executable and command[1].endswith("svg_render_worker.py")
        assert kwargs["shell"] is False and kwargs["stderr"] is subprocess.DEVNULL
        child = actual_popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(20)"], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(svg_render.subprocess, "Popen", stalled)
    stop = threading.Event()
    timer = threading.Timer(0.12, stop.set) if cancel else None
    if timer:
        timer.start()
    start = time.monotonic()
    deadline = start + (5 if cancel else 0.2)
    try:
        with pytest.raises((validation._InvalidArtifact, svg_render.SvgRenderError)) as caught:
            svg_render.render_svg(b'<svg/>', max_pixels=100,
                check=lambda: validation._check_cancelled(stop, deadline), deadline=deadline)
        assert caught.value.reason == "validation_deadline"
        assert time.monotonic() - start < 2
        assert children and all(child.poll() is not None for child in children)
        assert all(child.stdin.closed and child.stdout.closed for child in children)
    finally:
        if timer:
            timer.cancel()


def test_renderer_start_failure_is_service_unavailability_not_bad_user_file(monkeypatch):
    def failed(*_, **__):
        raise OSError("PRIVATE_HOST_PATH")
    monkeypatch.setattr(svg_render.subprocess, "Popen", failed)
    with pytest.raises(svg_render.SvgRenderError) as caught:
        svg_render.render_svg(b'<svg/>', max_pixels=100, check=lambda: None, deadline=time.monotonic()+1)
    assert caught.value.reason == "validator_unavailable"
    assert "PRIVATE_HOST_PATH" not in str(caught.value)
