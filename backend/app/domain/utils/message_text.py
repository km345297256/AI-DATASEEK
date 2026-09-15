"""Shared, deliberately narrow recognition of internal placeholder answers."""
import re
from typing import Any

NON_SUBSTANTIVE_MESSAGE_PATTERN = re.compile(
    r"^(?:placeholder|tbd|todo|n/?a|待补充|占位(?:符|文本)?|暂无(?:内容|结果)?)"
    r"(?:\s*[-_:—–]*\s*(?:"
    r"do[-_ ]+not[-_ ]+(?:send|use|display)|"
    r"not[-_ ]+(?:used|for[-_ ]+(?:sending|display))|"
    r"ignore(?:[-_ ]+this)?|不要发送|请勿发送|无需发送"
    r"))?[.!。]?$",
    re.IGNORECASE,
)


def is_non_substantive_message_text(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    text = value.strip()
    return not text or bool(NON_SUBSTANTIVE_MESSAGE_PATTERN.fullmatch(text))
