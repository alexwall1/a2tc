"""Split the operative part into a tree of instructions with their payloads.

Two properties of the source make naive segmentation fail:

**Indentation is unreliable.** In COM(2026) 616 final, `(a)` of point (2) sits at
column 13 while its sibling `(b)` sits at column 5. Nesting has to come from marker
type and sequence, never from layout.

**Payloads contain marker-shaped text.** Of 250 lines beginning with a `(x)` marker,
only 125 are instructions. The replacement text quoted inside `(2)(a)` opens with
`(i) a stationary technical unit ...`, which is indistinguishable from an instruction
marker by shape alone.

The discriminator is that an instruction *states an operation* — "is replaced by",
"is deleted", "is amended as follows". Quoted legislation being inserted does not.
Combined with a check that the marker is a legal successor in its sequence, this
separates the two cleanly.
"""

from __future__ import annotations

import re

from a2t.detect.targets import ANCHOR
from a2t.detect.celex import parse_citation
from a2t.extract.pdf import Line
from a2t.model.instruction import Instruction, Op, TargetBlock

MARKER = re.compile(r"^\((?P<marker>[0-9]{1,2}|[a-z]{1,3}|[ivxl]{1,5})\)\s*(?P<rest>.*)$")

# Older acts number instructions "1." rather than "(1)". That form is ambiguous —
# quoted legislation numbers its own paragraphs the same way — so it is only accepted
# in documents that demonstrably use it, decided once per block.
DOT_MARKER = re.compile(r"^(?P<marker>\d{1,2})\.\s+(?P<rest>\S.*)$")

# An instruction states an operation; quoted legislation does not.
OPERATION = re.compile(
    r"\b(?:is|are)\s+(?:hereby\s+)?(?:replaced|deleted|inserted|added|amended)\b"
    r"|\bamended\s+as\s+(?:follows|set\s+out)\b"
    r"|\bshall\s+be\s+(?:replaced|deleted|inserted)\b"
    # The auxiliary is sometimes dropped: "paragraph 7 replaced by the following".
    r"|\b(?:replaced|inserted|added)\s+by\s+the\s+following\b",
    re.I,
)

CONTAINER = re.compile(r"\b(?:is|shall\s+be)\s+(?:hereby\s+)?amended\s+as\s+follows\s*:", re.I)

OPEN_QUOTE = "‘"  # ‘
CLOSE_QUOTE = "’"  # ’
PAYLOAD_RULE = re.compile(r"^\s*_{3,}\s*$")

ROMAN_ONLY = set("ivxl")
ROMAN_SEQUENCE = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
                  "xi", "xii", "xiii", "xiv", "xv"]

LOOKAHEAD = 3  # lines joined when testing for an operation verb


def _alpha_successor(marker: str) -> str:
    """a -> b, z -> aa, aa -> ab."""
    chars = list(marker)
    i = len(chars) - 1
    while i >= 0:
        if chars[i] != "z":
            chars[i] = chr(ord(chars[i]) + 1)
            return "".join(chars)
        chars[i] = "a"
        i -= 1
    return "a" + "".join(chars)


def _roman_successor(marker: str) -> str | None:
    try:
        return ROMAN_SEQUENCE[ROMAN_SEQUENCE.index(marker) + 1]
    except (ValueError, IndexError):
        return None


def marker_kind(marker: str) -> str:
    if marker.isdigit():
        return "num"
    if set(marker) <= ROMAN_ONLY and marker in ROMAN_SEQUENCE:
        return "ambiguous" if len(marker) == 1 else "roman"
    return "alpha"


def _first_of(kind: str) -> str:
    return {"num": "1", "alpha": "a", "roman": "i"}[kind]


class _Segmenter:
    def __init__(self, lines: list[Line], prefix: str):
        self.lines = lines
        self.prefix = prefix
        self.roots: list[Instruction] = []
        self.stack: list[Instruction] = []  # open instructions, outermost first
        self.allow_dot = self._prefers_dot_markers()

    def _prefers_dot_markers(self) -> bool:
        """Does this block number its instructions "1." or "(1)"?

        Decided by which form actually carries operation verbs, so a document whose
        payloads contain numbered paragraphs is not misread as using dot markers.
        """
        paren = dot = 0
        for line in self.lines:
            text = line.stripped
            if not OPERATION.search(text):
                continue
            if MARKER.match(text):
                paren += 1
            elif DOT_MARKER.match(text):
                dot += 1
        return dot > paren

    def _match_marker(self, text: str):
        match = MARKER.match(text)
        if match is not None:
            return match
        return DOT_MARKER.match(text) if self.allow_dot else None

    # ------------------------------------------------------------- recognition

    def _window(self, index: int) -> str:
        """The candidate line plus its wrapped continuation.

        The window stops at the next marker line: without that, a payload line that
        merely happens to begin with "(b)" borrows the operation verb from the real
        instruction below it and is mistaken for one.
        """
        window = [self.lines[index].stripped]
        for line in self.lines[index + 1:index + LOOKAHEAD]:
            if self._match_marker(line.stripped):
                break
            window.append(line.stripped)
        return " ".join(window)

    def _classify(self, marker: str) -> tuple[int, str] | None:
        """Decide which level `marker` belongs to, or None if it fits nowhere.

        A marker is valid if it continues an open level, or opens the next level
        down with that sequence's first marker.
        """
        kind = marker_kind(marker)

        # Continue an existing level.
        for depth in range(len(self.stack) - 1, -1, -1):
            current = self.stack[depth]
            current_kind = marker_kind(current.marker)
            expected = None
            if current_kind == "num" and kind == "num":
                expected = str(int(current.marker) + 1)
            elif current_kind in ("alpha", "ambiguous") and kind in ("alpha", "ambiguous"):
                expected = _alpha_successor(current.marker)
            elif current_kind in ("roman", "ambiguous") and kind in ("roman", "ambiguous"):
                expected = _roman_successor(current.marker)
            if expected is not None and marker == expected:
                return depth + 1, current_kind if current_kind != "ambiguous" else kind

        # Open a level below the innermost open instruction.
        if kind in ("alpha", "ambiguous") and marker == "a":
            return len(self.stack) + 1, "alpha"
        if kind in ("roman", "ambiguous") and marker == "i":
            return len(self.stack) + 1, "roman"
        if kind == "num" and marker == "1" and not self.stack:
            return 1, "num"
        return None

    def _is_instruction(self, index: int, marker: str) -> tuple[int, str] | None:
        if not OPERATION.search(self._window(index)):
            return None
        return self._classify(marker)

    # ------------------------------------------------------------------- build

    def run(self) -> list[Instruction]:
        pending: Instruction | None = None
        body: list[Line] = []

        for index, line in enumerate(self.lines):
            match = self._match_marker(line.stripped)
            placement = (
                self._is_instruction(index, match.group("marker")) if match else None
            )

            if placement is None:
                if pending is not None:
                    body.append(line)
                continue

            if pending is not None:
                self._finish(pending, body)
            level, _ = placement
            del self.stack[level - 1:]

            instruction = Instruction(
                id="", marker=match.group("marker"), level=level,
                raw=match.group("rest").strip(), source_line=line.lineno,
            )
            parent = self.stack[-1] if self.stack else None
            if parent is None:
                self.roots.append(instruction)
            else:
                parent.children.append(instruction)
            instruction.id = self._make_id(instruction, parent)
            self.stack.append(instruction)
            pending, body = instruction, []

        if pending is not None:
            self._finish(pending, body)
        if not self.roots:
            self._run_unnumbered()
        return self.roots

    def _run_unnumbered(self) -> None:
        """Handle a block whose single instruction carries no marker.

        Where an act is amended in only one respect, drafters often omit the "(1)":

            Regulation (EU) 2020/1056 is amended as follows:
            In Article 9(1), the following point (l) is inserted:

        Only applied when marker-based segmentation found nothing, so a numbered
        block can never be reinterpreted this way.
        """
        starts = [
            index for index, line in enumerate(self.lines)
            if not line.blank
            and line.stripped[:1] not in (OPEN_QUOTE, CLOSE_QUOTE, "\u201c", '"')
            and OPERATION.search(line.stripped)
        ]
        for position, index in enumerate(starts):
            end = starts[position + 1] if position + 1 < len(starts) else len(self.lines)
            instruction = Instruction(
                id=f"{self.prefix}.{position + 1}", marker=str(position + 1), level=1,
                raw=self.lines[index].stripped, source_line=self.lines[index].lineno,
            )
            instruction.note("instruction carries no number in the source")
            self.roots.append(instruction)
            self._finish(instruction, self.lines[index + 1:end])

    def _make_id(self, instruction: Instruction, parent: Instruction | None) -> str:
        base = parent.id if parent else self.prefix
        return f"{base}.{instruction.marker}"

    def _finish(self, instruction: Instruction, body: list[Line]) -> None:
        """Attach the trailing lines to an instruction as continuation or payload."""
        text_lines = [line for line in body if not line.blank]
        if not text_lines:
            instruction.op = (
                Op.CONTAINER if CONTAINER.search(instruction.raw) else instruction.op
            )
            return

        if CONTAINER.search(instruction.raw):
            instruction.op = Op.CONTAINER
            return

        # An instruction sentence may wrap across lines. It ends at its own
        # terminator: ":" introduces a payload, ";" or "." closes the instruction
        # outright. Quote position cannot be used — an inline replacement such as
        # "'Article 27' is replaced by 'Articles 27 and 27a'." is all sentence.
        consumed = 0
        sentence = instruction.raw
        while not sentence.rstrip().endswith((":", ";", ".")) and consumed < len(text_lines):
            sentence = f"{sentence} {text_lines[consumed].stripped}".strip()
            consumed += 1
        instruction.raw = re.sub(r"\s+", " ", sentence).strip()

        if not instruction.raw.rstrip().endswith(":"):
            # Self-contained instruction; anything after it is not its payload.
            return

        payload_lines = text_lines[consumed:]
        if not payload_lines:
            return
        (instruction.payload, instruction.footnotes, instruction.payload_layout,
         instruction.payload_breaks) = _extract_payload(payload_lines)
        instruction.payload_lines = (
            payload_lines[0].lineno, payload_lines[-1].lineno
        )


PAYLOAD_END = re.compile(rf"{CLOSE_QUOTE}\s*[;.]\s*$")


def _truncate_at_close(lines: list[Line]) -> list[Line]:
    """Cut a payload at its closing quote.

    Fencing only on the next instruction marker lets anything between the two —
    a footnote, an ELI line, a page marker — fall inside the quoted legislation.
    A payload ends with the closing quote and the instruction's own punctuation, and
    in well-formed text that appears exactly once, so this is a no-op there.
    """
    for index, line in enumerate(lines):
        if PAYLOAD_END.search(line.stripped):
            return lines[:index + 1]
    return lines


def _extract_payload(
    lines: list[Line],
) -> tuple[str, list[str], list[str], list[bool]]:
    """Join payload lines, split off any footnote block, and strip the outer quotes.

    The body's lines are returned alongside the joined text, indentation intact, and
    with which of them open a paragraph in the source: the joining is lossy, and
    printing the payload as legislation needs the structure that only the layout and
    the line spacing still record. See `a2t.model.payload`.
    """
    lines = _truncate_at_close(lines)
    rule = next(
        (i for i, line in enumerate(lines) if PAYLOAD_RULE.match(line.text)), None
    )
    body, footnote_lines = (
        (lines[:rule], lines[rule + 1:]) if rule is not None else (lines, [])
    )

    text = " ".join(line.stripped for line in body).strip()
    text = re.sub(r"\s+", " ", text)

    # Drafting slips use the closing quote, or a double quote, as the opener.
    if text[:1] in (OPEN_QUOTE, CLOSE_QUOTE, "\u201c", '"'):
        text = text[1:]
    # Trailing "’;" / "’." / "’" — the closing quote plus the instruction's own
    # punctuation. Quote counting cannot be used: apostrophes share the character.
    text = re.sub(rf"[{CLOSE_QUOTE}\u201d]\s*[;.]?\s*$", "", text).strip()

    footnotes: list[str] = []
    if footnote_lines:
        joined = " ".join(line.stripped for line in footnote_lines)
        joined = re.sub(rf"{CLOSE_QUOTE}\s*[;.]?\s*$", "", joined).strip()
        footnotes = [f for f in re.split(r"\s(?=\*\s)", joined) if f.strip()]
    return (text, footnotes, [line.text for line in body],
            [line.starts_paragraph for line in body])


_HEADING = re.compile(r"^(?:Article\s+\d+|Amendments?\s+to\s+.+|)$", re.I)


def _is_heading(text: str) -> bool:
    return bool(_HEADING.match(text.strip()))


def segment(operative: list[Line], targets) -> list[TargetBlock]:
    """Split the operative part into one `TargetBlock` per amended act."""
    anchors: list[tuple[int, object]] = []
    for index, line in enumerate(operative):
        match = ANCHOR.match(line.stripped)
        if match and parse_citation(match.group("act")) is not None:
            anchors.append((index, line))

    blocks: list[TargetBlock] = []
    for position, (index, _) in enumerate(anchors):
        target = targets[position] if position < len(targets) else None
        if target is None:
            continue
        end = anchors[position + 1][0] if position + 1 < len(anchors) else len(operative)

        # After the last amending article come the act's own provisions —
        # "Article 3 / Transposition" and so on. Without this bound the final
        # instruction's payload swallows them.
        if target.amending_article and target.amending_article.isdigit():
            following = re.compile(rf"^Article\s+{int(target.amending_article) + 1}$")
            for offset in range(index + 1, end):
                if following.match(operative[offset].stripped):
                    end = offset
                    break

        # The next target's "Article 2 / Amendments to ..." heading sits *above* its
        # anchor, so it would otherwise land in this block's last instruction.
        while end - 1 > index and _is_heading(operative[end - 1].stripped):
            end -= 1
        prefix = str(target.amending_article or position + 1)
        block = TargetBlock(
            celex=target.celex,
            citation=target.citation.raw,
            root_scope=target.root_scope,
        )
        block.instructions = _Segmenter(operative[index + 1:end], prefix).run()
        blocks.append(block)
    return blocks
