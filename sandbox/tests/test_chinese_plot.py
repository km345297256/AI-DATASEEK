import io
import warnings
from pathlib import Path

import pytest


def test_chinese_plot_uses_noto_sc_without_missing_glyph_warnings():
    matplotlib = pytest.importorskip("matplotlib")

    matplotlib.use("Agg")
    from matplotlib import font_manager
    import matplotlib.pyplot as plt

    expected_config = Path("/etc/matplotlibrc")
    if Path(matplotlib.matplotlib_fname()).resolve() != expected_config:
        pytest.skip("strict CJK font verification runs inside the sandbox image")

    assert matplotlib.rcParams["font.family"] == ["sans-serif"]
    assert matplotlib.rcParams["font.sans-serif"][0] == "Noto Sans CJK SC"
    assert matplotlib.rcParams["axes.unicode_minus"] is False

    requested_font = Path(font_manager.findfont(
        font_manager.FontProperties(family=["Noto Sans CJK SC"]),
        fallback_to_default=False,
    )).resolve()
    default_font = Path(font_manager.findfont(
        font_manager.FontProperties(family=["sans-serif"]),
        fallback_to_default=False,
    )).resolve()

    assert requested_font == default_font
    assert font_manager.get_font(str(requested_font)).family_name == "Noto Sans CJK SC"

    output = io.BytesIO()
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        fig, ax = plt.subplots()
        ax.plot([1, 2, 3], [1, -2, 3])
        ax.set_title("科学数据探查")
        ax.set_xlabel("时间（年）")
        ax.set_ylabel(r"碳通量 (gC/m$^2$/yr)")
        fig.canvas.draw()
        fig.savefig(output, format="png")
        plt.close(fig)

    missing_glyph_warnings = [
        str(item.message)
        for item in captured
        if "Glyph" in str(item.message) and "missing from font" in str(item.message)
    ]
    assert missing_glyph_warnings == []
    assert len(output.getvalue()) > 1_000
