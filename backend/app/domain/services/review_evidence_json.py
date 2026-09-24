"""Bound private review evidence without cutting serialized JSON syntax.

Retained strings are exact prefixes of observed values, never summaries or
concatenations. Omitted values are unknown. The caller carries the truncation
flag separately so it cannot be quoted as an observation from the source.
"""
from __future__ import annotations

import json
import math
from typing import Any


def bounded_json(value: Any, limit: int) -> tuple[str, bool]:
    if limit < 2:
        raise ValueError("json_evidence_budget_too_small")
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    omitted = object()

    def encode(item: Any) -> str:
        return json.dumps(item, ensure_ascii=False, default=str, separators=(",", ":"), allow_nan=False)

    def fit(item: Any, budget: int, depth: int = 0) -> tuple[Any, bool]:
        if depth > 16 or budget < 2:
            return omitted, True
        if hasattr(item, "model_dump"):
            item = item.model_dump(mode="json")
        if isinstance(item, float) and not math.isfinite(item):
            return omitted, True
        if isinstance(item, dict):
            kept, used, cut = {}, 2, False
            for key, child in item.items():
                # JSON object keys retain their original JSON spelling.
                if not isinstance(key, str):
                    if key is None:
                        key = "null"
                    elif key is True:
                        key = "true"
                    elif key is False:
                        key = "false"
                    elif isinstance(key, (int, float)) and not (
                            isinstance(key, float) and not math.isfinite(key)):
                        key = encode(key)
                    else:
                        cut = True
                        continue
                if key in kept:
                    # A string and numeric key may serialize identically.
                    # Do not overwrite an earlier observed value silently.
                    cut = True
                    continue
                overhead = len(encode(key)) + 1 + bool(kept)
                if used + overhead + 2 > budget:
                    cut = True
                    break
                content, clipped = fit(child, budget - used - overhead, depth + 1)
                cut = cut or clipped
                if content is omitted:
                    continue
                kept[key] = content
                used += overhead + len(encode(content))
            return kept, cut
        if isinstance(item, (list, tuple)):
            kept, used, cut = [], 2, False
            for child in item:
                overhead = bool(kept)
                content, clipped = fit(child, budget - used - overhead, depth + 1)
                cut = cut or clipped
                if content is omitted:
                    # A prefix preserves positions; never shift later records
                    # into the position of an omitted observation.
                    cut = True
                    break
                kept.append(content)
                used += overhead + len(encode(content))
            return kept, cut
        if not isinstance(item, (str, int, float, bool, type(None))):
            item = str(item)
        rendered = encode(item)
        if len(rendered) <= budget:
            return item, False
        if not isinstance(item, str):
            return omitted, True
        low, high = 0, min(len(item), budget - 2)
        while low < high:
            middle = (low + high + 1) // 2
            if len(encode(item[:middle])) <= budget:
                low = middle
            else:
                high = middle - 1
        return item[:low], True

    result, truncated = fit(value, limit)
    if result is omitted:
        # No scalar can be faithfully represented within the budget. An empty
        # envelope contains no scalar assertion, unlike a fabricated null/zero.
        result = {}
    return encode(result), truncated
