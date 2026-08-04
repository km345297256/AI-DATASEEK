#!/usr/bin/env python3
"""Extract Matplotlib-readable Simplified Chinese faces from Debian Noto TTCs."""

from __future__ import annotations

import os
from pathlib import Path

from fontTools.ttLib import TTCollection, TTFont


FONT_ROOT = Path("/usr/share/fonts")
TARGET_ROOT = Path("/usr/local/share/fonts/opentype/noto")
FAMILY = "Noto Sans CJK SC"
FACES = {
    "Regular": "NotoSansCJK-Regular.ttc",
    "Bold": "NotoSansCJK-Bold.ttc",
}


def _family_names(font: TTFont) -> set[str]:
    names: set[str] = set()
    for record in font["name"].names:
        if record.nameID not in {1, 16}:
            continue
        try:
            names.add(record.toUnicode())
        except UnicodeDecodeError:
            continue
    return names


def _find_collection(filename: str) -> Path:
    matches = sorted(FONT_ROOT.rglob(filename))
    if not matches:
        raise RuntimeError(f"Noto CJK collection is missing: {filename}")
    return matches[0]


def _extract_face(style: str, filename: str) -> Path:
    source = _find_collection(filename)
    collection = TTCollection(source)
    try:
        matches = [font for font in collection.fonts if FAMILY in _family_names(font)]
        if len(matches) != 1:
            available = sorted(
                {
                    name
                    for font in collection.fonts
                    for name in _family_names(font)
                    if name.startswith("Noto Sans CJK")
                }
            )
            raise RuntimeError(
                f"Expected one {FAMILY} face in {source}, found {len(matches)}; "
                f"available families: {available}"
            )

        TARGET_ROOT.mkdir(parents=True, exist_ok=True)
        target = TARGET_ROOT / f"NotoSansCJKSC-{style}.otf"
        temporary = target.with_suffix(".otf.tmp")
        matches[0].save(temporary, reorderTables=True)
        os.chmod(temporary, 0o644)
        temporary.replace(target)
    finally:
        collection.close()

    extracted = TTFont(target, lazy=True)
    try:
        if FAMILY not in _family_names(extracted):
            raise RuntimeError(f"Extracted font has the wrong family: {target}")
    finally:
        extracted.close()
    return target


def main() -> None:
    targets = [_extract_face(style, filename) for style, filename in FACES.items()]
    print("Extracted Matplotlib CJK fonts:", ", ".join(map(str, targets)))


if __name__ == "__main__":
    main()
