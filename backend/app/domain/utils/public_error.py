from __future__ import annotations

import re


PUBLIC_ERROR_FALLBACK = "The analysis task failed unexpectedly"

_QUOTED_ABSOLUTE_PATH = re.compile(
    r"(?P<quote>['\"])(?:/|[A-Za-z]:[\\/])[^'\"]+(?P=quote)"
)
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s'\"`<>]+"
)
_UNC_ABSOLUTE_PATH = re.compile(r"(?<![\\])\\\\[^\s'\"`<>]+")
_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?<![:A-Za-z0-9_])/(?!/)[^\s'\"`<>]+"
)
_AUTHORIZATION_CREDENTIAL = re.compile(
    r"""
    (?P<prefix>
        (?<![A-Za-z0-9_-])
        authorization\s*[:=]\s*(?:bearer|basic)\s+
    )
    (?P<value>
        (?P<value_quote>['\"])[^'\"\r\n]*(?P=value_quote)
        |
        [^\s,;&]+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_KEY_VALUE_CREDENTIAL = re.compile(
    r"""
    (?P<prefix>
        (?<![A-Za-z0-9_-])
        (?P<key_quote>['\"]?)
        (?:api[-_]key|access[-_]token|token|password|passwd|secret)
        (?P=key_quote)
        \s*[:=]\s*
    )
    (?P<value>
        (?P<value_quote>['\"])[^'\"\r\n]*(?P=value_quote)
        |
        [^\s,;&]+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _replace_credential(match: re.Match[str]) -> str:
    quote = match.group("value_quote") or ""
    return f"{match.group('prefix')}{quote}[redacted credential]{quote}"


def public_error_message(value: object) -> str:
    """Return an error message safe to persist and expose through HTTP/SSE.

    Operational exceptions often embed dataset bind sources, volume
    mountpoints, worker-local paths, or credentials.  Preserve the useful
    surrounding explanation while redacting those sensitive values.
    """

    message = str(value).strip()
    if not message:
        return PUBLIC_ERROR_FALLBACK

    # Internal terminal-state identifiers are useful in logs and metrics, but
    # they are not actionable user messages. Keep the public SSE contract
    # stable and human-readable across execution, repair, and provider failures.
    normalized = message.casefold()
    if "finalization_timeout" in normalized:
        return "分析过程未能在等待时限内生成最终回答，请重试或缩小问题范围。"
    if "finalization_failed" in normalized:
        return "分析过程暂时无法生成最终回答，请稍后重试。"
    if "invalid_final_result" in normalized:
        return "分析已经结束，但未能生成可用的最终回答，请重试。"
    if "maximum iteration count" in normalized:
        return "分析步骤已达到本次执行上限，请缩小问题范围后重试。"
    if "validation error for executionresult" in normalized:
        return "分析结果格式异常，系统未能生成可用回答，请重试。"

    def replace_quoted(match: re.Match[str]) -> str:
        quote = match.group("quote")
        return f"{quote}[redacted path]{quote}"

    message = _AUTHORIZATION_CREDENTIAL.sub(_replace_credential, message)
    message = _KEY_VALUE_CREDENTIAL.sub(_replace_credential, message)
    message = _QUOTED_ABSOLUTE_PATH.sub(replace_quoted, message)
    message = _WINDOWS_ABSOLUTE_PATH.sub("[redacted path]", message)
    message = _UNC_ABSOLUTE_PATH.sub("[redacted path]", message)
    message = _POSIX_ABSOLUTE_PATH.sub("[redacted path]", message)
    return message or PUBLIC_ERROR_FALLBACK
