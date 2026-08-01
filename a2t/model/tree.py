"""The structural model of a legal act.

Addressing mirrors how amendment instructions actually refer to text:

    Article 10a / paragraph 1a / subparagraph 2
    Article 3   / point (h)
    Article 9   / subparagraph 4

Numbered levels (article, paragraph, point) carry a ``key`` — "10a", "1a", "h".
Unnumbered levels (subparagraph, sentence) are addressed by ordinal position, which is
why every node also records its 1-based ``index`` among siblings of the same kind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Kind(str, Enum):
    ACT = "act"
    CHAPTER = "chapter"
    ARTICLE = "article"
    PARAGRAPH = "paragraph"
    SUBPARAGRAPH = "subparagraph"
    POINT = "point"
    SUBPOINT = "subpoint"
    SENTENCE = "sentence"


# eq=False keeps identity semantics: two provisions with the same text are still
# different provisions, and changes are indexed by node.
@dataclass(eq=False)
class Node:
    kind: Kind
    key: str | None = None  # "10a", "1a", "h" — None when addressed by ordinal only
    index: int = 1  # 1-based position among same-kind siblings
    text: str = ""
    title: str | None = None
    children: list[Node] = field(default_factory=list)
    parent: Node | None = field(default=None, repr=False, compare=False)
    provenance: str | None = None  # the ▼M marker in force for this node
    source_id: str | None = None  # the id attribute in the source HTML

    # ------------------------------------------------------------------ structure

    def add(self, child: Node) -> Node:
        child.parent = self
        child.index = sum(1 for c in self.children if c.kind is child.kind) + 1
        self.children.append(child)
        return child

    def of_kind(self, kind: Kind) -> list[Node]:
        return [c for c in self.children if c.kind is kind]

    def by_key(self, kind: Kind, key: str) -> Node | None:
        want = _normalise_key(key)
        return next(
            (c for c in self.children if c.kind is kind and _normalise_key(c.key) == want),
            None,
        )

    def by_index(self, kind: Kind, index: int) -> Node | None:
        """1-based; negative counts from the end (-1 = last, -2 = penultimate)."""
        siblings = self.of_kind(kind)
        if not siblings:
            return None
        if index < 0:
            return siblings[index] if -index <= len(siblings) else None
        return siblings[index - 1] if 1 <= index <= len(siblings) else None

    # ----------------------------------------------------------------------- text

    @property
    def full_text(self) -> str:
        """This node's own text plus its descendants', in document order."""
        parts = [self.text] if self.text else []
        parts += [c.full_text for c in self.children]
        return "\n".join(p for p in parts if p)

    @property
    def label(self) -> str:
        if self.kind is Kind.ARTICLE:
            return f"Article {self.key}"
        if self.kind in (Kind.POINT, Kind.SUBPOINT):
            return f"point ({self.key})"
        if self.key:
            return f"{self.kind.value} {self.key}"
        return f"{self.kind.value} {self.index}"

    @property
    def citation(self) -> str:
        """Human-readable address, e.g. 'Article 10a / paragraph 1a / subparagraph 2'."""
        chain: list[str] = []
        node: Node | None = self
        while node is not None and node.kind is not Kind.ACT:
            chain.append(node.label)
            node = node.parent
        return " / ".join(reversed(chain))

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def __repr__(self) -> str:
        return f"<{self.kind.value} {self.key or self.index} {self.text[:40]!r}>"


@dataclass
class Act:
    """A retrieved legal act, rooted at its articles."""

    celex: str
    title: str
    root: Node = field(default_factory=lambda: Node(Kind.ACT))
    version_date: str | None = None

    @property
    def articles(self) -> list[Node]:
        return [n for n in self.root.walk() if n.kind is Kind.ARTICLE]

    def article(self, key: str) -> Node | None:
        want = _normalise_key(key)
        return next((a for a in self.articles if _normalise_key(a.key) == want), None)

    def count(self, kind: Kind) -> int:
        return sum(1 for n in self.root.walk() if n.kind is kind)

    def __repr__(self) -> str:
        return f"<Act {self.celex} articles={len(self.articles)}>"


def _normalise_key(key: str | None) -> str | None:
    """Compare keys case- and decoration-insensitively: '(h)' == 'h', '10A' == '10a'."""
    if key is None:
        return None
    return key.strip().strip("().").lower()
