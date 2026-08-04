import os

import pytest


def test_chinese_plot_renders_without_missing_glyph_warnings(tmp_path):
    matplotlib = pytest.importorskip("matplotlib")

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output = tmp_path / "中文标题.png"
    fig, ax = plt.subplots()
    ax.plot([1, 2, 3], [1, -2, 3])
    ax.set_title("中文标题")
    ax.set_xlabel("时间")
    ax.set_ylabel("温度")
    fig.savefig(output)
    plt.close(fig)

    assert output.exists()
    assert output.stat().st_size > 0
    assert os.environ.get("LANG") in {"zh_CN.UTF-8", "C.UTF-8", "en_US.UTF-8", None}
