from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain.external.plugin_runtime import _validate_portable_regex


CORPUS_PATH = Path(__file__).parent / "fixtures" / "portable_regex_corpus.json"


@pytest.mark.parametrize(
    ("pattern", "accepted"),
    [
        (entry["pattern"], entry["accepted"])
        for entry in json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    ],
)
def test_portable_regex_corpus(pattern: str, accepted: bool) -> None:
    if accepted:
        _validate_portable_regex(pattern)
        return
    with pytest.raises((ValueError, TypeError)):
        _validate_portable_regex(pattern)
