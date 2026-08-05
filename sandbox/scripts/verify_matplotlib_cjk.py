#!/usr/bin/env python3
"""Fail fast when the sandbox cannot render Chinese text with Noto CJK SC."""

from __future__ import annotations

import io
import warnings
from pathlib import Path

import matplotlib


EXPECTED_CONFIG = Path("/etc/matplotlibrc")
EXPECTED_FAMILY = "Noto Sans CJK SC"


def main() -> None:
    matplotlib.use("Agg")

    from matplotlib import font_manager, pyplot as plt

    active_config = Path(matplotlib.matplotlib_fname()).resolve()
    if active_config != EXPECTED_CONFIG:
        raise RuntimeError(
            f"Unexpected matplotlibrc: {active_config}; expected {EXPECTED_CONFIG}"
        )
    if matplotlib.rcParams["font.family"] != ["sans-serif"]:
        raise RuntimeError(
            f"Unexpected default font family: {matplotlib.rcParams['font.family']}"
        )
    if matplotlib.rcParams["font.sans-serif"][0] != EXPECTED_FAMILY:
        raise RuntimeError(
            "Noto Sans CJK SC is not the first sans-serif font: "
            f"{matplotlib.rcParams['font.sans-serif']}"
        )
    if matplotlib.rcParams["axes.unicode_minus"]:
        raise RuntimeError("axes.unicode_minus must be disabled")

    requested_font = Path(
        font_manager.findfont(
            font_manager.FontProperties(family=[EXPECTED_FAMILY]),
            fallback_to_default=False,
        )
    ).resolve()
    default_font = Path(
        font_manager.findfont(
            font_manager.FontProperties(family=["sans-serif"]),
            fallback_to_default=False,
        )
    ).resolve()
    if requested_font != default_font:
        raise RuntimeError(
            f"Default sans-serif resolved to {default_font}, not {requested_font}"
        )
    resolved_family = font_manager.get_font(str(requested_font)).family_name
    if resolved_family != EXPECTED_FAMILY:
        raise RuntimeError(
            f"findfont resolved {resolved_family!r}, expected {EXPECTED_FAMILY!r}"
        )

    png = io.BytesIO()
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        figure, axes = plt.subplots()
        axes.plot([1, 2, 3], [2, -1, 4])
        axes.set_title("科学数据探查")
        axes.set_xlabel("时间（年）")
        axes.set_ylabel(r"碳通量 (gC/m$^2$/yr)")
        figure.canvas.draw()
        figure.savefig(png, format="png")
        plt.close(figure)

    missing_glyph_warnings = [
        str(item.message)
        for item in captured
        if "Glyph" in str(item.message) and "missing from font" in str(item.message)
    ]
    if missing_glyph_warnings:
        raise RuntimeError(
            "Chinese rendering emitted missing-glyph warnings: "
            + "; ".join(missing_glyph_warnings)
        )
    if len(png.getvalue()) < 1_000:
        raise RuntimeError("Chinese rendering produced an unexpectedly small PNG")

    print(
        "Matplotlib CJK verification passed: "
        f"config={active_config}, family={resolved_family}, font={requested_font}"
    )


if __name__ == "__main__":
    main()
