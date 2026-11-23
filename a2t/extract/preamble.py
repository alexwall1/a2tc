"""Recover the amending act's own preamble from the proposal PDF.

The preamble is not an amendment to anything. Nothing in it resolves against the base
act, nothing diffs against it: an amending act carries its own recitals and leaves the
recitals of the act it amends untouched (verified across every act amending Directive
2003/87/EC — none contains a single recital-amending instruction). So this module
parses, and the emitter prints; no other phase has an opinion about it.

What has to be recovered is the shape the Official Journal prints, because that is what
makes the text readable:

    2026/0212 (COD)                                       <- procedure number, right
              Proposal for a                              <- centred
    DIRECTIVE OF THE EUROPEAN PARLIAMENT AND OF THE ...   <- centred, bold
    amending Directive 2003/87/EC and Decision ...        <- centred, bold
              (Text with EEA relevance)                   <- centred
    THE EUROPEAN PARLIAMENT AND THE COUNCIL ...,          <- left
    Having regard to the Treaty ..., and in particular    <- one citation, wrapped
    Article 192(1) thereof,
    Whereas:
    (1)     The Paris Agreement3, adopted on 12 December  <- recital, hanging indent
            2015 under the United Nations ...
    HAVE ADOPTED THIS DIRECTIVE:

Three boundaries have to be found rather than assumed:

* **Where the preamble starts.** A COM proposal prints its title block twice — once on
  the cover page and once above the preamble proper. The institutions line
  ("THE EUROPEAN PARLIAMENT AND THE COUNCIL OF THE EUROPEAN UNION,") appears exactly
  once in both sample proposals, so the walk starts there and works backwards.

* **Where one recital ends and the next begins.** A recital may quote a number in
  brackets, so "(2)" opening a line is not enough. As everywhere else in this project,
  the discriminator is sequence: a marker starts a new recital only when it succeeds
  the one before it.

* **Which digits are footnote references.** `pdftotext -layout` renders a superscript
  glued to the word it follows — "the Paris Agreement3, adopted on" — and sometimes
  separated from it by a space, which makes "of 30 June" the same shape as a reference
  to footnote 30. Neither the shape of the digits nor their order settles it, so the
  markers are read from the geometry instead (`a2t.extract.pdf.superscripts`) and
  matched here against the word each one follows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from a2t.extract.deboilerplate import ENACTING_FORMULA, strip_furniture
from a2t.extract.pdf import Line, superscripts

# "2026/0212 (COD)" — the interinstitutional procedure number.
PROCEDURE = re.compile(r"^\s*(\d{4}/\d{4}\s*\([A-Z]{3}\))\s*$")

# "Proposal for a" — the line above a COM proposal's title block.
PROPOSAL_LEAD = re.compile(r"^\s*Proposal for an?\s*$", re.I)

# "THE EUROPEAN PARLIAMENT AND THE COUNCIL OF THE EUROPEAN UNION," and its Commission
# and Council equivalents. All caps and a trailing comma separate it from the title.
INSTITUTIONS = re.compile(r"^\s*THE\s+[A-Z][A-Z\s'’()-]*,\s*$")

# Openers of a citation. Everything between the institutions line and "Whereas:" is one
# of these; a line matching none of them continues the citation above it.
CITATION_OPENER = re.compile(
    r"^\s*(?:Having regard to|Acting in accordance|After (?:transmission|consulting|"
    r"having consulted)|In accordance with|Whereas)\b", re.I
)

WHEREAS = re.compile(r"^\s*Whereas\s*:?\s*$", re.I)

# "(1)    The Paris Agreement ..." — a recital marker and the text that follows it.
RECITAL = re.compile(r"^\s*\((?P<number>\d{1,3})\)\s+(?P<rest>\S.*)$")

# A footnote body at the foot of a page: the number alone on a line, its text below.
FOOTNOTE_NUMBER = re.compile(r"^\s*(\d{1,3})\s*$")



class PreambleError(RuntimeError):
    pass


@dataclass(frozen=True)
class Piece:
    """A run of preamble text. `superscript` marks a footnote reference."""

    text: str
    superscript: bool = False


@dataclass(frozen=True)
class Para:
    """One paragraph of the preamble, with its footnote references marked."""

    pieces: tuple[Piece, ...]
    number: str | None = None  # the recital's number, where it has one

    @property
    def text(self) -> str:
        return "".join(piece.text for piece in self.pieces)

    def __repr__(self) -> str:
        head = f"({self.number}) " if self.number else ""
        return f"<Para {head}{self.text[:48]!r}>"


@dataclass
class Preamble:
    """The amending act's own front matter, as the Official Journal prints it."""

    procedure: str | None = None
    title: tuple[Para, ...] = ()
    institutions: str | None = None
    citations: tuple[Para, ...] = ()
    whereas: str = "Whereas:"
    recitals: tuple[Para, ...] = ()
    enacting: str = ""
    # (number, text) per document footnote referenced from the preamble, written as real
    # Word footnotes by `emit/tracked_docx.py`.
    footnotes: tuple[tuple[str, str], ...] = ()
    source_lines: tuple[int, int] = (0, 0)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.recitals)} recital(s), {len(self.citations)} citation(s), "
            f"{len(self.footnotes)} footnote(s)"
        )


def _join(lines: list[Line]) -> str:
    """Wrapped lines as one paragraph."""
    return re.sub(r"\s+", " ", " ".join(ln.text for ln in lines)).strip()


def _groups(lines: list[Line]) -> list[list[Line]]:
    """Consecutive non-blank lines, split on blank ones."""
    out: list[list[Line]] = []
    current: list[Line] = []
    for line in lines:
        if line.blank:
            if current:
                out.append(current)
                current = []
        else:
            current.append(line)
    if current:
        out.append(current)
    return out


def _succeeds(previous: str | None, number: str) -> bool:
    """True when `number` is the recital that follows `previous`."""
    if previous is None:
        return number == "1"
    return int(number) == int(previous) + 1


def _split_recitals(lines: list[Line]) -> list[tuple[str, list[Line]]]:
    """Group the lines below "Whereas:" into (number, lines) per recital.

    A bracketed number opens a recital only when it succeeds the one before it, so a
    figure quoted inside a recital cannot start a spurious one.
    """
    out: list[tuple[str, list[Line]]] = []
    previous: str | None = None
    for line in lines:
        match = RECITAL.match(line.text)
        if match and _succeeds(previous, match.group("number")):
            previous = match.group("number")
            out.append((previous, [line]))
        elif out and not line.blank:
            out[-1][1].append(line)
    return out


def _strip_marker(text: str) -> str:
    """Drop the leading "(n)" from a recital's first line."""
    match = RECITAL.match(text)
    return match.group("rest") if match else text.strip()


class _Markers:
    """Places the footnote references poppler found, in the order it found them.

    Each reference is looked for as the word it follows plus its own digits, so the
    match has to agree with the geometry on *both* before a digit in the running text
    is taken for a marker. A reference that cannot be found simply stays unplaced: the
    text is then printed as the PDF renders it, which is worse-looking than a
    superscript and better than a figure silently raised out of a sentence.
    """

    def __init__(self, marks: list[tuple[str, str]]) -> None:
        self._marks = list(marks)
        self._at = 0
        self.used: list[str] = []

    @property
    def unplaced(self) -> list[str]:
        return [number for _, number in self._marks[self._at:]]

    def _pattern(self, previous: str, number: str) -> re.Pattern:
        digits = re.escape(number) + r"(?!\d)"
        if not previous:
            return re.compile(r"(?<!\d)" + digits)
        return re.compile(re.escape(previous) + r"\s*" + digits)

    def split(self, text: str) -> tuple[Piece, ...]:
        """`text` as pieces, with its footnote references marked superscript."""
        pieces: list[Piece] = []
        cursor = 0
        while self._at < len(self._marks):
            previous, number = self._marks[self._at]
            match = self._pattern(previous, number).search(text, cursor)
            if match is None:
                break
            cut = match.end() - len(number)
            if cut > cursor:
                # Poppler separates a superscript from its word on some documents and
                # glues it on others. The printed page has no space either way.
                pieces.append(Piece(text[cursor:cut].rstrip(" ")))
            pieces.append(Piece(number, superscript=True))
            cursor = match.end()
            self._at += 1
            self.used.append(number)
        if cursor < len(text):
            pieces.append(Piece(text[cursor:]))
        return tuple(pieces) or (Piece(text),)


def _footnote_bodies(removed: list[Line]) -> list[tuple[str, str]]:
    """The document footnotes stripped as page furniture, as (number, text) pairs.

    They are furniture for parsing — no instruction is in them — but the preamble cites
    them, and a recital that references a source the reader cannot see is a recital
    printed short.
    """
    out: list[tuple[str, str]] = []
    pending: str | None = None
    body: list[str] = []
    for line in removed:
        if line.blank:
            continue
        match = FOOTNOTE_NUMBER.match(line.text)
        if match:
            if pending is not None:
                out.append((pending, " ".join(body).strip()))
            pending, body = match.group(1), []
        elif pending is not None:
            body.append(line.stripped)
    if pending is not None:
        out.append((pending, " ".join(body).strip()))
    return [(number, text) for number, text in out if text]


def find_preamble(lines: list[Line]) -> tuple[int, int]:
    """Index range ``[start, end)`` of the preamble, ending at the enacting formula.

    The enacting formula itself is inside the range: it closes the preamble and the
    emitted document needs it to introduce the articles.
    """
    end = next(
        (i for i, ln in enumerate(lines) if ENACTING_FORMULA.search(ln.text)), None
    )
    if end is None:
        raise PreambleError("no enacting formula found — is this an amending act?")

    institutions = next(
        (i for i in range(end - 1, -1, -1) if INSTITUTIONS.match(lines[i].text)), None
    )
    if institutions is None:
        raise PreambleError(
            "no institutions line ('THE EUROPEAN PARLIAMENT AND ...,') before the "
            "enacting formula — cannot tell where the preamble begins"
        )

    # A COM proposal prints its title block twice; the one that belongs to the preamble
    # is the last "Proposal for a" above the institutions line.
    lead = next(
        (i for i in range(institutions - 1, -1, -1)
         if PROPOSAL_LEAD.match(lines[i].text)), None
    )
    if lead is None:
        # An adopted act has no "Proposal for a" — take the title block as whatever
        # non-blank run immediately precedes the institutions line.
        start = institutions
        while start > 0 and not lines[start - 1].blank:
            start -= 1
        return start, end + 1

    start = lead
    # The procedure number sits above the lead, separated by a blank line at most.
    for i in range(lead - 1, max(lead - 4, -1), -1):
        if PROCEDURE.match(lines[i].text):
            start = i
            break
    return start, end + 1


def _end_of_page(lines: list[Line], index: int) -> int:
    """Index just past the last line of the page `index` falls on.

    The enacting formula sits mid-page, and the footnotes the last recitals cite are
    printed *below* it, at the foot of that same page. Cutting the preamble at the
    formula would therefore lose them.
    """
    page = lines[index].page
    stop = index
    while stop < len(lines) and lines[stop].page == page:
        stop += 1
    return stop


def preamble(lines: list[Line], pdf_path: str | Path | None = None) -> Preamble:
    """Parse the amending act's preamble out of the extracted PDF lines.

    `pdf_path` is the PDF the lines came from. Without it the text still comes out
    whole, but footnote references stay as the digits `-layout` glued to their words.
    """
    start, end = find_preamble(lines)
    cleaned = strip_furniture(lines[start:end])
    body = cleaned.lines
    # Footnote bodies are collected over the whole of the closing page, the text only
    # down to the enacting formula.
    footnotes = _footnote_bodies(
        strip_furniture(lines[start:_end_of_page(lines, end - 1)])
        .removed_for("document footnote")
    )

    index = {name: None for name in ("institutions", "whereas", "enacting")}
    for i, line in enumerate(body):
        if index["institutions"] is None and INSTITUTIONS.match(line.text):
            index["institutions"] = i
        elif index["whereas"] is None and WHEREAS.match(line.text):
            index["whereas"] = i
        elif ENACTING_FORMULA.search(line.text):
            index["enacting"] = i

    if index["institutions"] is None or index["enacting"] is None:
        raise PreambleError("preamble boundaries lost after stripping page furniture")

    result = Preamble(
        enacting=body[index["enacting"]].stripped,
        institutions=body[index["institutions"]].stripped,
        source_lines=(lines[start].lineno, lines[end - 1].lineno),
    )

    # --- title block: everything above the institutions line, blank-line separated
    head = body[:index["institutions"]]
    groups = _groups(head)
    if groups and PROCEDURE.match(groups[0][0].text):
        result.procedure = _join(groups[0])
        groups = groups[1:]
    result.title = tuple(Para((Piece(_join(g)),)) for g in groups if _join(g))

    marks = _Markers(
        superscripts(pdf_path, lines[start].page, lines[end - 1].page)
        if pdf_path is not None else []
    )

    # --- citations: institutions line to "Whereas:", one paragraph per opener
    whereas_at = index["whereas"] if index["whereas"] is not None else index["enacting"]
    citations: list[list[Line]] = []
    for line in body[index["institutions"] + 1:whereas_at]:
        if line.blank:
            continue
        if CITATION_OPENER.match(line.text) or not citations:
            citations.append([line])
        else:
            citations[-1].append(line)
    result.citations = tuple(Para(marks.split(_join(g))) for g in citations)

    if index["whereas"] is not None:
        result.whereas = body[index["whereas"]].stripped

    # --- recitals
    recitals = _split_recitals(body[whereas_at + 1:index["enacting"]])
    parsed: list[Para] = []
    for number, group in recitals:
        text = _join(group)
        parsed.append(Para(marks.split(_strip_marker(text)), number=number))
    result.recitals = tuple(parsed)

    # Only the footnotes the preamble actually cites: the closing page carries the
    # first of the operative part's as well, and those belong to the articles.
    cited = set(marks.used)
    result.footnotes = tuple((n, t) for n, t in footnotes if n in cited)

    if not parsed:
        result.notes.append("no recitals found between the citations and the "
                            "enacting formula")
    else:
        expected = [str(n) for n in range(1, len(parsed) + 1)]
        if [p.number for p in parsed] != expected:
            result.notes.append(
                f"recital numbering is not 1..{len(parsed)} — "
                f"read {parsed[0].number}..{parsed[-1].number}"
            )
    if marks.unplaced:
        result.notes.append(
            f"{len(marks.unplaced)} footnote reference(s) could not be placed in the "
            f"text: {', '.join(marks.unplaced)}"
        )
    dangling = sorted(cited - {n for n, _ in result.footnotes}, key=int)
    if dangling:
        result.notes.append(
            f"footnote(s) {', '.join(dangling)} referenced but their text was not "
            f"found at the foot of any page"
        )
    return result
