import pytest

from app.services.file import FileService


@pytest.mark.asyncio
async def test_bounded_preview_does_not_decode_unread_trailing_file_data(tmp_path):
    path = tmp_path / "large-mixed-encoding.csv"
    prefix = "station,value\n北京,12\n"
    with path.open("wb") as stream:
        stream.write(prefix.encode("utf-8"))
        stream.write(b"x" * (2 * 1024 * 1024))
        stream.write(b"\xff")

    result = await FileService().read_file(str(path), max_length=len(prefix))

    assert result.content == prefix + "(truncated)"


@pytest.mark.asyncio
async def test_bounded_preview_does_not_mark_exact_limit_as_truncated(tmp_path):
    path = tmp_path / "exact.txt"
    path.write_text("北京,12\n", encoding="utf-8")

    result = await FileService().read_file(str(path), max_length=6)

    assert result.content == "北京,12\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original",
    [
        f"replace-me\n{'z' * 12_000}",
        f"{'a' * 11_000}\nreplace-me\n{'z' * 2_000}",
    ],
    ids=["match-before-limit-with-long-tail", "match-after-limit"],
)
async def test_str_replace_uses_complete_file_beyond_read_preview_limit(
    tmp_path,
    original,
):
    path = tmp_path / "large.txt"
    path.write_text(original, encoding="utf-8")

    result = await FileService().str_replace(
        str(path),
        "replace-me",
        "replacement-complete",
    )

    assert result.replaced_count == 1
    assert path.read_text(encoding="utf-8") == original.replace(
        "replace-me",
        "replacement-complete",
    )
    assert "(truncated)" not in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_find_in_content_searches_after_read_preview_limit(tmp_path):
    path = tmp_path / "large-search.txt"
    path.write_text(f"{'a' * 11_000}\nneedle-after-preview\n", encoding="utf-8")

    result = await FileService().find_in_content(
        str(path),
        r"needle-after-preview$",
    )

    assert result.matches == ["needle-after-preview"]
    assert result.line_numbers == [1]
