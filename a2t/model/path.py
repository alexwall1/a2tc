"""Addressing nodes the way amendment instructions do.

Instructions mix keyed and ordinal reference, often in one breath:

    Article 10a / paragraph 1a / the second subparagraph
    Article 3   / point (h)
    Article 9   / the fourth subparagraph
    paragraph 1 / second sentence

A `Path` is a sequence of `Step`s, each either keyed ("10a", "h") or ordinal
("second" -> 2, "penultimate" -> -2). Negative indices count from the end, which is how
"last" and "penultimate" are represented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from a2t.model.subdivide import split_sentences
from a2t.model.tree import Act, Kind, Node

ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20,
    "last": -1, "final": -1, "penultimate": -2,
    "second-to-last": -2, "second to last": -2, "antepenultimate": -3,
}

ORDINAL_DIGITS = re.compile(r"^(\d+)(?:st|nd|rd|th)$", re.I)

KIND_WORDS = {
    "article": Kind.ARTICLE,
    "paragraph": Kind.PARAGRAPH,
    "subparagraph": Kind.SUBPARAGRAPH,
    "point": Kind.POINT,
    "subpoint": Kind.SUBPOINT,
    "sentence": Kind.SENTENCE,
    "chapter": Kind.CHAPTER,
}


def parse_ordinal(word: str) -> int | None:
    """"second" -> 2, "penultimate" -> -2, "4th" -> 4."""
    cleaned = word.strip().lower().replace("’", "'")
    if cleaned in ORDINAL_WORDS:
        return ORDINAL_WORDS[cleaned]
    match = ORDINAL_DIGITS.match(cleaned)
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class Step:
    kind: Kind
    key: str | None = None
    index: int | None = None  # 1-based; negative counts from the end
    # "paragraphs 12 to 18": this step names a run, from `key` to `through`. Only an
    # enumeration holds one, and the resolver expands it against the act's own siblings.
    through: str | None = None

    def __str__(self) -> str:
        if self.key is not None and self.through is not None:
            return f"{self.kind.value}s {self.key} to {self.through}"
        if self.key is not None:
            return f"{self.kind.value} {self.key}"
        word = next(
            (w for w, v in ORDINAL_WORDS.items() if v == self.index and "-" not in w),
            str(self.index),
        )
        return f"the {word} {self.kind.value}"


@dataclass(frozen=True)
class Path:
    steps: tuple[Step, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.steps)

    def __truediv__(self, step: Step) -> Path:
        return Path(self.steps + (step,))

    def __str__(self) -> str:
        return " / ".join(str(s) for s in self.steps)

    @property
    def kind(self) -> Kind | None:
        return self.steps[-1].kind if self.steps else None


def resolve(path: Path, root: Act | Node, notes: list[str] | None = None) -> Node | None:
    """Walk `path` from `root`, returning the addressed node or None.

    With `notes`, a keyed step that finds nothing under an unnumbered subparagraph is
    looked for beside it, and the jump is recorded there. The tree keeps points on the
    paragraph or article, not on the block that introduces them, while an instruction
    says "in the first subparagraph, point (a)". The jump is only taken when asked for,
    so a caller can try the exact reading first and say that it did not hold.
    """
    node = root.root if isinstance(root, Act) else root

    for step in path.steps:
        if node is None:
            return None
        if step.kind is Kind.SENTENCE:
            node = _resolve_sentence(node, step)
            continue
        if step.key is not None:
            found = node.by_key(step.kind, step.key)
            if found is None and step.kind is Kind.POINT and node.kind in (
                    Kind.POINT, Kind.SUBPOINT):
                # "in point (g), point (i)": the act's own words call every level of a
                # list a point; the tree calls the second level a sub-point.
                found = node.by_key(Kind.SUBPOINT, step.key)
            if (found is None and notes is not None and node.kind is Kind.SUBPARAGRAPH
                    and not node.of_kind(step.kind) and node.parent is not None):
                found = node.parent.by_key(step.kind, step.key)
                if found is not None:
                    notes.append(
                        f"{step} counted across the whole of {node.parent.citation}, "
                        f"not only {node.citation}")
            # An article may be addressed from the act root even when the walk
            # started deeper, e.g. a scoped target.
            if found is None and step.kind is Kind.ARTICLE and isinstance(root, Act):
                found = root.article(step.key)
            node = found
        elif step.index is not None:
            node = _by_index(node, step)
        else:
            return None
    return node


def _by_index(node: Node, step: Step) -> Node | None:
    """The step-th child of that kind, allowing for how an article divides.

    An article with numbered paragraphs divides into paragraphs, and the blocks making
    up one of them are its subparagraphs. An article with no numbered paragraphs
    divides straight into unnumbered blocks — and those are what drafters call its
    paragraphs: COM(2025) 524 amends "Article 1, second paragraph" of the European
    Climate Law, whose Article 1 is three unnumbered blocks and not one numbered
    paragraph. The tree calls those subparagraphs in both cases, so a paragraph ordinal
    has to be able to reach them; but only where there is no numbered paragraph to mean
    instead, since everywhere else the two words address different things.
    """
    found = node.by_index(step.kind, step.index)
    if (found is None and step.kind is Kind.PARAGRAPH
            and not node.of_kind(Kind.PARAGRAPH)):
        return node.by_index(Kind.SUBPARAGRAPH, step.index)
    return found


def _resolve_sentence(node: Node, step: Step) -> Node | None:
    """Sentences are derived on demand from the node's own text."""
    if step.index is None:
        return None
    sentences = split_sentences(node.text or node.full_text)
    if not sentences:
        return None
    index = step.index
    position = len(sentences) + index if index < 0 else index - 1
    if not 0 <= position < len(sentences):
        return None
    child = Node(Kind.SENTENCE, index=position + 1, text=sentences[position])
    child.parent = node
    return child


def candidates(node: Node) -> list[Path]:
    """Every addressable path below `node` — the closed set offered to the LLM tier."""
    out: list[Path] = []

    def walk(current: Node, prefix: Path) -> None:
        for child in current.children:
            step = (
                Step(child.kind, key=child.key)
                if child.key is not None
                else Step(child.kind, index=child.index)
            )
            path = prefix / step
            out.append(path)
            walk(child, path)

    walk(node, Path())
    return out
