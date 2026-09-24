"""Preserve proved numeric notation at the existing host-path redaction boundary.

This is not a mathematical evaluator or a general slash allowlist. Callers keep
all credential/URL handling and supply their unchanged host-path expression.
"""
from __future__ import annotations

import re


_NUMBER = r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
_NUMERIC_EXPRESSION = re.compile(
    r"(?<![\w./\\:=+?,-])"
    + _NUMBER
    + r"[ \t]+(?P<operator>\+/-|/)[ \t]+"
    + _NUMBER
    + r"(?![\w./\\+,-])"
)

# Conservatively keep query/assignment text on its original path-redaction
# policy, even when values are quoted, parenthesized or spaced.
_QUERY_OR_ASSIGNMENT = re.compile(r"[=?][^\r\n]*")


def redact_host_paths(text: str, *, pattern: re.Pattern[str]) -> str:
    """Keep only whole `/` or `/-` matches inside complete spaced operands.

    No substitution markers are inserted or later restored. Every other path
    match follows the supplied original expression, including numeric paths,
    path suffixes, incomplete expressions, and cross-line/compact notation.
    """
    numeric_operator_spans: set[tuple[int, int]] = set()
    excluded = iter(_QUERY_OR_ASSIGNMENT.finditer(text))
    exclusion = next(excluded, None)
    for expression in _NUMERIC_EXPRESSION.finditer(text):
        while exclusion is not None and exclusion.end() <= expression.start():
            exclusion = next(excluded, None)
        if exclusion is not None and exclusion.start() <= expression.start() < exclusion.end():
            continue
        previous = expression.start() - 1
        while previous >= 0 and text[previous] in " \t":
            previous -= 1
        if previous >= 0 and text[previous] in "/\\=?":
            # Do not reinterpret spaced paths or query/assignment values as
            # arithmetic. Colon-delimited labels such as `finite: 1 / 2`
            # remain eligible when they are followed by horizontal whitespace.
            continue
        following = expression.end()
        while following < len(text) and text[following] in " \t":
            following += 1
        if following < len(text) and text[following] in "/\\":
            continue
        start, end = expression.span("operator")
        if expression.group("operator") == "+/-":
            start += 1  # The unchanged path expression sees only the `/-`.
        numeric_operator_spans.add((start, end))

    def replacement(match: re.Match[str]) -> str:
        if match.group(0) in {"/", "/-"} and match.span() in numeric_operator_spans:
            return match.group(0)
        return "[protected path]"

    return pattern.sub(replacement, text)
