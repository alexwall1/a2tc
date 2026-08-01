"""The parsed form of a single amendment instruction."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Op(str, Enum):
    CONTAINER = "container"      # "Article 3 is amended as follows:"
    REPLACE_NODE = "replace"     # "paragraph 6 is replaced by the following:"
    DELETE_NODE = "delete"       # "point (p) is deleted"
    INSERT_NODE = "insert"       # "the following Article 3gaa is inserted"
    APPEND_NODE = "append"       # "the following points are added"
    REPLACE_TEXT = "replace-text"  # "'Article 27' is replaced by 'Articles 27 and 27a'"
    DELETE_TEXT = "delete-text"  # "the words '...' are deleted"
    EXTERNAL = "external"        # "Annexes I to V are amended as set out in ..."
    UNKNOWN = "unknown"


@dataclass
class Instruction:
    """One numbered instruction, with its position in the proposal's own hierarchy."""

    id: str  # hierarchical, e.g. "1.15.b.i"
    marker: str  # "15", "b", "i"
    level: int  # 1 = (1), 2 = (a), 3 = (i)
    raw: str  # the instruction sentence, joined across wrapped lines
    source_line: int
    op: Op = Op.UNKNOWN

    scope_text: str | None = None  # "in Article 2," / "in paragraph 1, second sentence,"
    target_text: str | None = None  # "the first subparagraph", "point (e)"
    payload: str | None = None  # verbatim replacement text, quotes stripped
    payload_lines: tuple[int, int] | None = None
    # The payload's source lines, indentation intact. `payload` is normalised for
    # comparison and so cannot be printed with its structure; these can (see
    # `a2t.model.payload`).
    payload_layout: list[str] = field(default_factory=list)
    find_text: str | None = None  # for REPLACE_TEXT / DELETE_TEXT
    replace_text: str | None = None
    footnotes: list[str] = field(default_factory=list)

    children: list[Instruction] = field(default_factory=list)
    confidence: float = 1.0
    resolver: str = "rule"
    notes: list[str] = field(default_factory=list)

    @property
    def is_container(self) -> bool:
        return self.op is Op.CONTAINER

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    def __repr__(self) -> str:
        return f"<Instruction {self.id} {self.op.value} {self.raw[:52]!r}>"


@dataclass
class TargetBlock:
    """All instructions amending one target act."""

    celex: str
    citation: str
    root_scope: str | None
    instructions: list[Instruction] = field(default_factory=list)

    def walk(self):
        for instruction in self.instructions:
            yield from instruction.walk()

    @property
    def leaves(self) -> list[Instruction]:
        return [i for i in self.walk() if not i.children]
