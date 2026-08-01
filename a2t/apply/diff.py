"""Turn an old/new text pair into a renderable edit script.

Word-level where the texts are related, whole-block where they are not. A provision
whose only change is a date should read as one struck date and one inserted date — not
as several hundred words deleted and re-inserted.

A provision replaced in full needs the same judgement one level up: `align` matches
its old blocks to the blocks that replace them, so each pair can be diffed on its own
instead of the whole provision collapsing into a single edit.

The output is presentation-independent: Phase 8 renders spans as Word revisions, but
the same spans are equally checkable in a test.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from a2t.model.payload import Provision, printed
from a2t.model.tree import Kind
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


# What sharing a number is worth against sharing words. Enough that two provisions
# numbered alike pair even with nothing else in common, and not so much that it
# overrides a plainly better match elsewhere.
KEY_BONUS = 0.5


@dataclass
class Pair:
    """One step of an alignment: an old block, a new one, or a match of the two."""

    old: Provision | None
    new: Provision | None
    edit: Edit
    title: Edit | None = None  # articles only — their title is a line of its own

    @property
    def block(self) -> Provision:
        """Whichever side exists, for deciding how the step is printed."""
        return self.new or self.old  # type: ignore[return-value]


def _affinity(old: Provision, new: Provision) -> float:
    """What pairing these two blocks is worth, negative when it is worth nothing.

    Scored as similarity above the threshold at which a diff is worth showing word by
    word, so two blocks pair exactly when the reader would rather see one marked-up
    provision than a deletion followed by an insertion.
    """
    score = similarity(printed(old), printed(new)) - BLOCK_THRESHOLD
    # A provision keeps its number across a rewrite far more reliably than its words:
    # "paragraph 3 is replaced by the following" yields a new paragraph 3 however
    # little of the old one survives.
    if old.key is not None and old.key == new.key and old.kind is new.kind:
        score += KEY_BONUS
    return score


def _title_edit(old: Provision | None, new: Provision | None) -> Edit | None:
    kind = (new or old).kind
    if kind is not Kind.ARTICLE:
        return None
    before, after = (old.title if old else None), (new.title if new else None)
    if not before and not after:
        return None
    return diff(before or "", after or "")


def align(old: list[Provision], new: list[Provision]) -> list[Pair]:
    """Match the blocks of a replaced provision to the blocks that replace it.

    A whole-provision replacement is not one edit but many: some paragraphs survive
    with a date changed, some are rewritten, some go, some are new. Rendering it as a
    single diff of the whole text buries all of that in one paragraph — and rendering
    it as the old text struck through followed by the new text inserted throws away
    the word-level marks that make a mostly-unchanged provision readable.

    So the two sides are aligned first, and each pair diffed on its own. The alignment
    maximises total affinity, which keeps the blocks in document order and lets a
    block that matches nothing stand alone as a deletion or an insertion.
    """
    rows, columns = len(old), len(new)
    best = [[0.0] * (columns + 1) for _ in range(rows + 1)]
    for i in range(1, rows + 1):
        for j in range(1, columns + 1):
            best[i][j] = max(
                best[i - 1][j - 1] + _affinity(old[i - 1], new[j - 1]),
                best[i - 1][j],
                best[i][j - 1],
            )

    pairs: list[Pair] = []
    i, j = rows, columns
    while i > 0 or j > 0:
        paired = (
            i > 0 and j > 0
            and best[i][j] == best[i - 1][j - 1] + _affinity(old[i - 1], new[j - 1])
        )
        if paired:
            a, b = old[i - 1], new[j - 1]
            pairs.append(Pair(a, b, diff(printed(a), printed(b)), _title_edit(a, b)))
            i, j = i - 1, j - 1
        elif j > 0 and (i == 0 or best[i][j] == best[i][j - 1]):
            b = new[j - 1]
            pairs.append(Pair(None, b, diff("", printed(b)), _title_edit(None, b)))
            j -= 1
        else:
            a = old[i - 1]
            pairs.append(Pair(a, None, diff(printed(a), ""), _title_edit(a, None)))
            i -= 1
    pairs.reverse()
    return pairs


def summarise(edit: Edit) -> str:
    if edit.mode == "unchanged":
        return "no change"
    deleted = len(tokenize(edit.deleted_text))
    inserted = len(tokenize(edit.inserted_text))
    return f"{edit.mode}: -{deleted} +{inserted} words ({edit.ratio:.0%} similar)"


__all__ = ["Edit", "Pair", "Span", "align", "diff", "similarity", "summarise",
           "BLOCK_THRESHOLD"]
