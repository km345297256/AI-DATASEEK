"""Runtime matching for administrator-managed safety rules.

The matcher contains no policy vocabulary. Rules are loaded from MongoDB by
``policy_store`` immediately before a review, so administrators can change
the safety list without editing code or restarting the service.
"""

import re
import unicodedata
from collections.abc import Iterable

from app.domain.models.safety import SafetyReview, SafetyRule


def normalize_review_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\s+", " ", value).strip()


def _matches(rule: SafetyRule, text: str) -> bool:
    if not rule.patterns:
        return False
    if rule.match_type == "regex":
        try:
            return any(re.search(pattern, text) for pattern in rule.patterns)
        except re.error:
            # Invalid patterns are rejected by the API; this guard prevents a
            # manually edited legacy document from breaking every task.
            return False
    def literal_match(value: str) -> bool:
        # Treat a plain ASCII identifier/phrase as a complete token. This
        # prevents "election" from matching "pageSelectionChanged" while
        # preserving natural substring matching for Chinese keywords.
        if re.fullmatch(r"[A-Za-z0-9_ -]+", value):
            return re.search(rf"(?<![A-Za-z0-9_]){re.escape(value)}(?![A-Za-z0-9_])", text, re.IGNORECASE) is not None
        return value.casefold() in text.casefold()

    matches = [literal_match(value) for value in rule.patterns]
    return any(matches) if rule.match_type == "keyword" else all(matches)


def deterministic_review(value: str, rules: Iterable[SafetyRule]) -> SafetyReview | None:
    """Apply enabled rules in priority order and return the first denial set."""
    text = normalize_review_text(value)
    if not text:
        return None

    matched = [rule for rule in sorted(rules, key=lambda item: (item.priority, item.name)) if rule.enabled and _matches(rule, text)]
    if not matched:
        return None

    categories = list(dict.fromkeys(rule.category for rule in matched))
    risk_order = {"medium": 1, "high": 2, "critical": 3}
    highest_risk = max((rule.risk_level for rule in matched), key=lambda value: risk_order.get(value, 2))
    reasons = [rule.reason.strip() for rule in matched if rule.reason.strip()]
    suggestions = [rule.suggestion.strip() for rule in matched if rule.suggestion.strip()]
    return SafetyReview(
        decision="reject",
        risk_level=highest_risk,
        categories=categories,
        reason=" ".join(dict.fromkeys(reasons)) or "请求命中了管理员配置的安全策略。",
        suggestion=" ".join(dict.fromkeys(suggestions)) or "请移除命中的敏感内容，并明确合法目的与授权范围后重试。",
    )
