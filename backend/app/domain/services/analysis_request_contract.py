"""Current-turn output intent shared by planning and delivery validation.

The controller's structured contract is authoritative. Local keyword routing is
only a fallback and cannot introduce file obligations into a read-only request.
"""
from __future__ import annotations

import re


def current_artifact_requirement(message) -> bool | None:
    required = getattr(message, "controller_requires_artifacts", None)
    if required is None:
        return None
    # Explicit structured deliverables take precedence over an inconsistent
    # boolean from the same decision; they still belong to this user turn.
    return bool(required or message.deliverables)


_CLAUSE_BOUNDARY = re.compile(r"[，,。;；!?！？\n、]+|\bbut\b|\binstead\b|而是|但是|但", re.I)
_NEGATED_ACTION = re.compile(
    r"(?:不要|不用|无需|无须|不需要|不必|不能|不得|不允许|禁止|别|不)(?:再|继续|实际|自动|额外|重新)?\s*"
    r"(?:生成|创建|制作|输出|导出|下载|保存|写入|绘制|绘图|画|可视化|做|运行|执行|计算|分析|统计|预测|比较|进行)"
    r"|\b(?:do\s+not|don't|don’t|no\s+need\s+to|without|avoid|never|skip)\s+"
    r"(?:\w+\s+){0,2}(?:generat\w*|creat\w*|mak\w*|sav\w*|writ\w*|export\w*|download\w*|"
    r"plot\w*|draw\w*|chart\w*|graph\w*|visualiz\w*|visualis\w*|run\w*|execut\w*|comput\w*|"
    r"analy[sz]\w*|predict\w*|file\w*)\b"
    r"|\bno\s+(?:new\s+)?(?:plots?|charts?|graphs?|files?|exports?|downloads?|visuali[sz]ations?)\b",
    re.I,
)
_NEGATED_OUTPUT_SUFFIX = re.compile(
    r"(?:绘图|画图|图表|可视化|文件|报告|导出|下载|保存)\s*(?:不需要|无需|无须|不必|不要|不用)"
    r"|\b(?:plots?|charts?|graphs?|files?|exports?|downloads?|visuali[sz]ations?)\s+"
    r"(?:are\s+|is\s+)?(?:not\s+(?:needed|required)|unnecessary)\b",
    re.I,
)


def affirmative_request_text(text: str) -> str:
    """Conservative routing view; never rewrite the actual user question.

    Remove negated action spans through their clause boundary so coordinated
    prohibitions ("do not plot or export") don't become affirmative requests.
    Separate positive clauses remain intact ("do not plot, export a CSV"). The
    host contract, not this lexical helper, decides mandatory deliverables.
    """
    parts = []
    for clause in _CLAUSE_BOUNDARY.split(text or ""):
        suffix = _NEGATED_OUTPUT_SUFFIX.search(clause)
        if suffix:
            clause = clause[:suffix.start()]
        negative = _NEGATED_ACTION.search(clause)
        if negative:
            clause = clause[:negative.start()]
        parts.append(clause)
    return " ".join(" ".join(parts).casefold().split())
