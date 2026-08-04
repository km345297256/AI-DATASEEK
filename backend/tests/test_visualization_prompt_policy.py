from app.domain.services.prompts.execution import EXECUTION_PROMPT
from app.domain.services.prompts.system import SYSTEM_PROMPT


def test_visualization_prompts_define_the_chinese_font_policy():
    for prompt in (SYSTEM_PROMPT, EXECUTION_PROMPT):
        assert "global sans-serif default" in prompt
        assert "Noto Sans CJK JP" in prompt
        assert "Noto Sans CJK SC" not in prompt
        assert "SimHei" in prompt
        assert "Microsoft YaHei" in prompt
        assert "unavailable fonts" in prompt


def test_visualization_prompts_define_unicode_and_png_output_policy():
    for prompt in (SYSTEM_PROMPT, EXECUTION_PROMPT):
        assert 'matplotlib.rcParams["axes.unicode_minus"] = False' in prompt
        assert "UTF-8" in prompt
        assert "PNG" in prompt
        assert "/home/ubuntu/output" in prompt


def test_visualization_prompts_avoid_unicode_superscript_units():
    for prompt in (SYSTEM_PROMPT, EXECUTION_PROMPT):
        assert "U+207B" in prompt
        assert "$m^{-2}$" in prompt
        assert "m^-2" in prompt
