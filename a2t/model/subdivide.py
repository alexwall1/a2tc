"""Sentence derivation.

`eurlex-builder` has no sentence granularity, but amendment instructions address
sentences directly — "the penultimate sentence is replaced by the following",
"in paragraph 1, second sentence, '2030' is replaced by '2040'".

Legal text is unusually hostile to naive sentence splitting: it is dense with
abbreviations ("No 219/2009", "OJ L 275", "p. 32"), decimal figures, and citations
carrying internal punctuation. Sentences are therefore derived lazily — only when an
instruction actually addresses one — and the split is conservative.
"""

from __future__ import annotations

import re

# Tokens that end in "." but do not end a sentence.
ABBREVIATIONS = {
    "no", "nos", "art", "arts", "para", "paras", "p", "pp", "cf", "e.g", "i.e",
    "etc", "vol", "ch", "sec", "fig", "approx", "incl", "min", "max", "eq",
}

# A candidate boundary: terminator, whitespace, then something that can open a
# sentence. Quotes and brackets are common openers in legislation.
_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z‘“\"(‘“])")

_TRAILING_TOKEN = re.compile(r"([A-Za-z.]+)\.$")


def _ends_with_abbreviation(chunk: str) -> bool:
    match = _TRAILING_TOKEN.search(chunk.rstrip())
    if match is None:
        return False
    token = match.group(1).rstrip(".").lower()
    return token in ABBREVIATIONS


def _ends_mid_citation(chunk: str) -> bool:
    """A dangling opener means the terminator sits inside a citation or aside."""
    return chunk.count("(") > chunk.count(")")


def split_sentences(text: str) -> list[str]:
    """Split `text` into sentences, conservatively.

    When in doubt the text is left joined: a missed split makes an instruction
    unresolvable and therefore visibly flagged, whereas a wrong split would silently
    amend the wrong words.
    """
    text = text.strip()
    if not text:
        return []

    parts = _BOUNDARY.split(text)
    sentences: list[str] = []
    for part in parts:
        if sentences and (
            _ends_with_abbreviation(sentences[-1]) or _ends_mid_citation(sentences[-1])
        ):
            sentences[-1] = f"{sentences[-1]} {part}"
        else:
            sentences.append(part)
    return [s.strip() for s in sentences if s.strip()]


def count_sentences(text: str) -> int:
    return len(split_sentences(text))
