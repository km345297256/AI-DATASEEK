"""Portable relative paths for saved data-product files and ZIP members."""
from pathlib import PurePosixPath, PureWindowsPath


def product_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid product file path")
    try:
        if len(value.encode("utf-8")) > 4096:
            raise ValueError("Invalid product file path")
    except UnicodeEncodeError:
        raise ValueError("Invalid product file path") from None
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or PureWindowsPath(value).drive
        or str(path) != value
        or not path.parts
        or ".." in path.parts
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("Invalid product file path")
    return str(path)
