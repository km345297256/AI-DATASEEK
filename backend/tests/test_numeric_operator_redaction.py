"""Public scientific notation must survive without weakening host-data redaction."""
import json

import pytest

from app.domain.models.event import FileToolContent, ToolEvent, ToolStatus
from app.domain.services.tools.spill_projection import (
    sanitize_spill_public_data, sanitize_spill_public_text,
)
from app.interfaces.schemas.tool_presentation import (
    MAX_TOOL_PRESENTATION_DATA_BYTES, _safe_text, normalize_tool_presentation,
)
from app.interfaces.schemas.event import ToolSSEEvent


@pytest.fixture(params=["spill", "presentation"])
def public_text(request):
    if request.param == "spill":
        return request.param, sanitize_spill_public_text
    return request.param, lambda text: _safe_text(text, limit=20_000)


@pytest.mark.parametrize("text", [
    "12.831 +/- 0.2",
    "finite: 91136 / 91136",
    "-1.2e-3 +/- +2.5E-4",
    "9.1136e4 / 9.1136E4",
    ".125 / .25",
    "0 / 0",  # Presentation is not a mathematical-validity assertion.
    "value: 12.831\t+/-\t0.2",
    "a 1 / 2; b 3 +/- 4",
    "12.831 +/- 0.2 m/s",
    "12.831 ± 0.2",
    "ratio: (1 / 2)",
])
def test_complete_spaced_numeric_operators_survive(public_text, text):
    _, project = public_text
    assert project(text) == text
    assert project(project(text)) == text


# Frozen expected behavior for sensitive or out-of-scope text. No runtime
# comparison to another implementation could accidentally bless a regression.
SECURITY_CASES = [('path=/Users/example/data.csv', 'path=[protected path]', 'path=[protected path]'),
 ('/private/example/data.csv', '[protected path]', '[protected path]'),
 ('root / directory', 'root [protected path] directory', 'root [protected path] directory'),
 ('1 /91136', '1 [protected path]', '1 [protected path]'),
 ('1 /91136/2', '1 [protected path]', '1 [protected path]'),
 ('1 +/-private/data.csv', '1 +[protected path]', '1 +[protected path]'),
 ('/1 / 2', '[protected path] [protected path] 2', '[protected path] [protected path] 2'),
 ('1 / 2.txt', '1 [protected path] 2.txt', '1 [protected path] 2.txt'),
 ('"/ 1 / 2"',
  '"[protected path] 1 [protected path] 2"',
  '"[protected path] 1 [protected path] 2"'),
 ('path=C:\\Users\\example\\data.csv', 'path=[protected path]', 'path=[protected path]'),
 ('path=\\\\server\\share\\data.csv', 'path=[protected path]', 'path=[protected path]'),
 ('Bearer dummy-sensitive-value', 'Bearer [redacted credential]', 'Bearer [redacted credential]'),
 ('password=91136 / 91136',
  'password=[redacted credential] [protected path] 91136',
  'password=[redacted credential] [protected path] 91136'),
 ('token=12.831 +/- 0.2',
  'token=[redacted credential] +[protected path] 0.2',
  'token=[redacted credential] +[protected path] 0.2'),
 ('https://example.test/view?path=/Users/example/data.csv&token=dummy-sensitive-value',
  'https://example.test/view?path=[protected path]&token=[redacted credential]',
  'https://example.test/view?path=[protected path]&token=[redacted credential]'),
 ('https://example.test/view?n=91136 / 91136',
  'https://example.test/view?n=91136 [protected path] 91136',
  'https://example.test/view?n=91136 [protected path] 91136'),
 ('https://example.test/view?n=12.831 +/- 0.2',
  'https://example.test/view?n=12.831 +[protected path] 0.2',
  'https://example.test/view?n=12.831 +[protected path] 0.2'),
 ('https://user:dummy-sensitive-value@example.test/view?path=/private/test',
  'https://[redacted credential]@example.test/view?path=[protected path]',
  'https://[redacted credential]@example.test/view?path=[protected path]'),
 ('mean +/- unknown', 'mean +[protected path] unknown', 'mean +[protected path] unknown'),
 ('12.831 +/- missing', '12.831 +[protected path] missing', '12.831 +[protected path] missing'),
 ('91136\n/\n91136', '91136\n[protected path]\n91136', '91136\n[protected path]\n91136'),
 ('/Users/example/91136 / 91136',
  '[protected path] [protected path] 91136',
  '[protected path] [protected path] 91136'),
 ('1 / 2.5.csv', '1 [protected path] 2.5.csv', '1 [protected path] 2.5.csv'),
 ('file=/91136', 'file=[protected path]', 'file=[protected path]'),
 ('/home/ubuntu/output/result.csv',
  '/home/ubuntu/output/result.csv',
  '/home/ubuntu/output/result.csv')]

SECURITY_CASES += [('https://example.test/?n= 1 / 2',
  'https://example.test/?n= 1 [protected path] 2',
  'https://example.test/?n= 1 [protected path] 2'),
 ('https://example.test/?n=\t1 +/- 2',
  'https://example.test/?n=\t1 +[protected path] 2',
  'https://example.test/?n=\t1 +[protected path] 2'),
 ('path = 1 / 2', 'path = 1 [protected path] 2', 'path = 1 [protected path] 2'),
 ('https://example.test/?n=(1 / 2)',
  'https://example.test/?n=(1 [protected path] 2)',
  'https://example.test/?n=(1 [protected path] 2)'),
 ('https://example.test/?n="1 / 2"',
  'https://example.test/?n="1 [protected path] 2"',
  'https://example.test/?n="1 [protected path] 2"'),
 ('path = (1 / 2)', 'path = (1 [protected path] 2)', 'path = (1 [protected path] 2)'),
 ('https://example.test/?n=1; later: 1 / 2',
  'https://example.test/?n=1; later: 1 [protected path] 2',
  'https://example.test/?n=1; later: 1 [protected path] 2'),
 ('1\xa0/\xa02', '1\xa0[protected path]\xa02', '1\xa0[protected path]\xa02'),
 ('1\u2028/\u20282', '1\u2028[protected path]\u20282', '1\u2028[protected path]\u20282'),
 ('1e / 2', '1e [protected path] 2', '1e [protected path] 2'),
 ('1 / 2e', '1 [protected path] 2e', '1 [protected path] 2e'),
 ('1 / 2 /private/example',
  '1 [protected path] 2 [protected path]',
  '1 [protected path] 2 [protected path]'),
 ('91136 / 91136.txt', '91136 [protected path] 91136.txt', '91136 [protected path] 91136.txt')]

@pytest.mark.parametrize("text,spill_expected,presentation_expected", SECURITY_CASES)
def test_sensitive_text_and_ambiguous_notation_keep_prior_protection(
    public_text, text, spill_expected, presentation_expected,
):
    name, project = public_text
    assert project(text) == (spill_expected if name == "spill" else presentation_expected)


def test_mixed_nested_public_data_preserves_math_and_keeps_secret_key_policy():
    source = {"values": ["12.831 +/- 0.2", "91136 / 91136"],
              "path": "/Users/example/input.dat", "accessToken": "must-stay-private",
              "note": "finite: 91136 / 91136; password=must-stay-private"}
    spill = sanitize_spill_public_data(source)
    shown = normalize_tool_presentation({"kind": "table", "data": [source]})
    assert spill["values"] == shown.data[0]["values"] == source["values"]
    assert spill["accessToken"] == "[redacted credential]"
    assert "accessToken" not in shown.data[0]
    for value in (spill, shown.model_dump()):
        encoded = json.dumps(value)
        assert "/Users/example" not in encoded and "must-stay-private" not in encoded
        assert "91136 / 91136" in encoded and "12.831 +/- 0.2" in encoded
    assert source["accessToken"] == "must-stay-private"  # Input is never mutated.


def test_compact_or_multiline_plus_minus_is_not_broadly_exempted():
    for project in (sanitize_spill_public_text, lambda t: _safe_text(t, limit=20_000)):
        assert project("12.831+/-0.2") == "12.831+[protected path]"
        assert project("12.831 +/-\n0.2") == "12.831 +[protected path]\n0.2"
        assert project("/ 91136 / 91136").count("[protected path]") == 2


def test_signed_resource_url_and_size_limits_are_unchanged():
    url = "/api/v1/files/example?signature=" + "a" * 64 + "&expires=123"
    result = normalize_tool_presentation({"kind": "artifact", "url": url,
        "data": [{"value": "91136 / 91136 " * 3000} for _ in range(100)]})
    assert result.url == url
    assert _safe_text(url, limit=2048) == url
    assert len(json.dumps(result.data, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")) <= MAX_TOOL_PRESENTATION_DATA_BYTES
    assert _safe_text("12.831 +/- 0.2", limit=8) == "12.831 +"


@pytest.mark.asyncio
async def test_real_sse_projection_preserves_operators_but_not_host_path():
    event = ToolEvent(status=ToolStatus.CALLED, tool_call_id="projection-only",
        tool_name="file", function_name="file_read", function_args={},
        tool_content=FileToolContent(content="12.831 +/- 0.2; finite: 91136 / 91136; path=/private/example/data"),
        function_result={"success": True, "data": {"content":
            "12.831 +/- 0.2; finite: 91136 / 91136; path=/private/example/data"}})
    mapped = await ToolSSEEvent.from_event_async(event)
    text = mapped.model_dump_json()
    assert "12.831 +/- 0.2" in text and "91136 / 91136" in text
    assert "/private/example" not in text and "[protected path]" in text
