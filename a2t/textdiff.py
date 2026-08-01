"""Token-level text comparison, shared by resolution and emission."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# A token is a word plus the whitespace that follows it, so joining tokens
# reconstructs the source text exactly.
_TOKEN = re.compile(r"\S+\s*")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text)


def similarity(old: str, new: str) -> float:
    """0..1 overlap between two texts, by word tokens."""
    if not old or not new:
        return 0.0
    return SequenceMatcher(None, tokenize(old), tokenize(new), autojunk=False).ratio()
