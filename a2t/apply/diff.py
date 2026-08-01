"""Turn an old/new text pair into a renderable edit script.

Word-level where the texts are related, whole-block where they are not. A provision
whose only change is a date should read as one struck date and one inserted date — not
as several hundred words deleted and re-inserted.

The output is presentation-independent: Phase 8 renders spans as Word revisions, but
the same spans are equally checkable in a test.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from a2t.textdiff import similarity, tokenize

BLOCK_THRESHOLD = 0.4


@dataclass(frozen=True)
class Span:
    op: str  # "equal" | "delete" | "insert"
    text: str

    @property
    def changed(self) -> bool:
        return self.op != "equal"


@dataclass
class Edit:
    spans: list[Span]
    mode: str  # "word" | "block" | "unchanged"
    ratio: float

    @property
    def changed_spans(self) -> list[Span]:
        return [s for s in self.spans if s.changed]

    @property
    def deleted_text(self) -> str:
        return "".join(s.text for s in self.spans if s.op == "delete")

    @property
    def inserted_text(self) -> str:
        return "".join(s.text for s in self.spans if s.op == "insert")

    @property
    def old_text(self) -> str:
        return "".join(s.text for s in self.spans if s.op in ("equal", "delete"))

    @property
    def new_text(self) -> str:
        return "".join(s.text for s in self.spans if s.op in ("equal", "insert"))


def diff(old: str, new: str, block_threshold: float = BLOCK_THRESHOLD) -> Edit:
    """Compare two texts, choosing word-level or whole-block presentation."""
    if old == new:
        return Edit([Span("equal", old)] if old else [], "unchanged", 1.0)
    if not old:
        return Edit([Span("insert", new)], "block", 0.0)
    if not new:
        return Edit([Span("delete", old)], "block", 0.0)

    a, b = tokenize(old), tokenize(new)
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    ratio = matcher.ratio()

    if ratio < block_threshold:
        # A wholesale rewrite: interleaving it word by word reads as noise.
        return Edit([Span("delete", old), Span("insert", new)], "block", ratio)

    spans: list[Span] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            spans.append(Span("equal", "".join(a[i1:i2])))
        elif op == "delete":
            spans.append(Span("delete", "".join(a[i1:i2])))
        elif op == "insert":
            spans.append(Span("insert", "".join(b[j1:j2])))
        else:
            spans.append(Span("delete", "".join(a[i1:i2])))
            spans.append(Span("insert", "".join(b[j1:j2])))
    return Edit(spans, "word", ratio)


def summarise(edit: Edit) -> str:
    if edit.mode == "unchanged":
        return "no change"
    deleted = len(tokenize(edit.deleted_text))
    inserted = len(tokenize(edit.inserted_text))
    return f"{edit.mode}: -{deleted} +{inserted} words ({edit.ratio:.0%} similar)"


__all__ = ["Edit", "Span", "diff", "similarity", "summarise", "BLOCK_THRESHOLD"]
