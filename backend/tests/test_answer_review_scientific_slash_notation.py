"""Publication classifies file identities, not every slash as a filesystem root.

All evidence and review replies are synthetic; no model or external data is used.
"""
import pytest

from app.domain.services import analysis_answer_review as review
from test_analysis_answer_review import paragraph, response, run, tool


@pytest.mark.parametrize("text", [
    "Bases are A/C/G/T; regions are IR/SSC/LSC.",
    "Rates are measured in mg/kg/day and mmol/L/min.",
    "单位为 mg/kg/day；比值为 α/β/γ。",
    "本次读取/分析/绘图均已完成。",
    "Compare `A/C/G/T`, `IR/SSC/LSC`, `mg/L` and `x/y`.",
    "使用 `甲/乙/丙` 描述分组。",
    "The response is (a/b)/(c/d).",
    "文件内容表明单位为 `mg/L`。",
    "每项分析/比较的结果均使用相同单位。",
])
def test_ordinary_slash_notation_is_not_a_file_identity(text):
    assert review._public_text(text, [], set()) == text


@pytest.mark.parametrize("text", [
    "文件位于/Users/private/report.png",
    "文件位于/任意私有目录/子目录",
    "路径为/未登记目录",
    "数据在/秘密/report.customtype",
    "Private /任意私有目录/子目录",
    "`/private`", "/unobserved",
    r"C:\private\report.csv", r"C:\private", "C:/private", r"文件位于C:\秘密\无扩展名",
    r"\\host\private\report.csv", r"共享文件\\host\private\folder",
    "Generated output/fabricated.csv", "Generated 目录/虚构.customtype",
    "Generated `../private/report`", "Generated ./private/report",
    "File: `results/private`", "Path: results/private",
    "文件路径：`结果/私有数据`", "文件路径：结果/私有数据",
    "Read `text/csv source.csv`.",
    "`~/private`", "`subdir\\private`",
    "[download](https://example.test/data.csv)",
])
def test_real_file_cues_still_require_an_observed_identity(text):
    with pytest.raises(review.FileReferenceValidationError):
        review._public_text(text, [], set())


@pytest.mark.parametrize("reference", [
    "/home/ubuntu/datasets/test/测量数据/observed.csv",
    "datasets/test/测量数据/observed.csv",
    "test/测量数据/observed.csv", "测量数据/observed.csv",
])
def test_scientific_notation_and_verified_file_are_handled_independently(reference):
    path = "/home/ubuntu/datasets/test/测量数据/observed.csv"
    assert review._public_text(f"Read `{reference}`; units are `mg/kg/day`.", [], {path}) == (
        "Read `observed.csv`; units are `mg/kg/day`.")


def test_expression_cannot_authorize_an_unknown_file_with_the_same_known_leaf():
    with pytest.raises(review.FileReferenceValidationError):
        review._public_text("Units mg/L; read `other/observed.csv`.", [],
                            {"/home/ubuntu/datasets/test/observed.csv"})


def test_expression_is_not_rewritten_by_a_proper_substring_identity():
    path = "/home/ubuntu/datasets/test/A/C"
    text = "Compare A/C/G/T with `A/C/G/T`; measured unit is mg/L."
    assert review._public_text(text, [], {path}) == text
    assert review._public_text("Observed identity `A/C`.", [], {path}) == "Observed identity `C`."


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "A/C/G/T form the observed categories.",
    "Observed concentration unit is `mg/L`.",
    "本轮读取/分析/绘图均已完成。",
])
async def test_grounded_scientific_notation_passes_without_citation_repair(text):
    result, ask = await run(response(paragraph(text, quote=text)), files=[],
                            events=[tool(data={"stdout": text})])
    assert result.status == "verified"
    assert result.text == text
    assert ask.await_count == 1
