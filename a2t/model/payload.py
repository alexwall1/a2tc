"""Recover the structure of a quoted payload.

By the time a payload reaches the emitter it is one flat string: `a2t.parse.segment`
joins its lines and collapses whitespace, because everything downstream of it —
resolution, phrase matching, diffing — compares payloads against node text that
EUR-Lex has already normalised the same way. That is right for comparing and wrong
for printing.

A payload that inserts a whole article carries the article's number, its title, its
numbered paragraphs, the unnumbered subparagraphs that follow them and their points.
Written as a single Word paragraph it reads as an undifferentiated block of justified
text — not what the Official Journal prints, and not something a reader can work from.

The structure is recovered from the payload's own source lines, which `pdftotext
-layout` preserves. In a Commission proposal the geometry is unambiguous:

    ‘Article 25b                <- heading, alone on a centred line
    ETS as a service            <- title, centred, above the body indent
    1.      Administrative ...  <- numbered paragraph, at the article's body indent
            (a)   aviation ...  <- point, indented past it
    Such support shall be ...   <- back at the body indent: a new subparagraph

Indentation is trusted only *within* one payload, where it is internally consistent;
nesting across instructions still comes from marker sequence (see `a2t.parse.segment`,
which documents why layout cannot be trusted there).

Where the source lines are unavailable the markers in the flat text are used instead.
That recovers paragraphs and points but not unnumbered subparagraphs, which have no
marker to find. When neither yields anything the payload is returned whole: a missed
split prints the text exactly as it is printed today, whereas a wrong split would
silently attribute words to the wrong provision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from a2t.model.tree import Kind, Node

OPEN_QUOTES = "‘“\""
CLOSE_QUOTES = "’”\""

ARTICLE_HEADING = re.compile(r"^Article\s+(?P<key>\d+[a-z]*)$", re.I)

ROMAN = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
         "xi", "xii", "xiii", "xiv", "xv"]
NUMBERED = re.compile(r"^(?P<key>\d{1,2}[a-z]?)\.\s+(?P<rest>\S.*)$", re.S)
LETTERED = re.compile(r"^\((?P<key>[a-z]{1,4})\)\s+(?P<rest>\S.*)$", re.S)

# Wrapped lines are re-joined at their own indent, so a marker is recognised at the
# level it sits on rather than at an exact column. Two columns of tolerance absorbs
# the odd ragged line without letting a point pass for body text.
SLACK = 2


@dataclass
class Provision:
    """One printable block of a payload.

    Field names match `a2t.model.tree.Node` so a recovered provision and one read from
    the base act can be styled, printed and compared by the same code — which is what
    lets a replacement be shown block against block.
    """

    kind: Kind
    key: str | None = None
    text: str = ""
    title: str | None = None

    @property
    def is_article(self) -> bool:
        return self.kind is Kind.ARTICLE


def printed(block: Node | Provision) -> str:
    """A provision as it appears on the page.

    Numbered provisions carry their marker, so renumbering shows up as a revision
    rather than happening silently between two paragraphs that look unchanged. An
    article's title is not included: it is printed on a line of its own.
    """
    if block.kind is Kind.ARTICLE:
        # An article block with no text of its own is a heading; one carrying text is
        # a provision whose text sits directly under the article, with no paragraph.
        return block.text or (f"Article {block.key}" if block.key else "")
    if block.kind is Kind.PARAGRAPH and block.key:
        return f"{block.key}. {block.text}"
    if block.kind in (Kind.POINT, Kind.SUBPOINT) and block.key:
        return f"({block.key}) {block.text}"
    return block.text


def opening_number(node: Node) -> str:
    """The paragraph number printed at the head of this provision, if any.

    EUR-Lex keeps a paragraph's number outside its words — the number is a span of
    its own and the text lives in child subparagraphs — so a numbered paragraph is a
    node with a key and nothing else, and the number has to be printed by whatever
    carries its first words. Without this the act loses its paragraph numbering
    entirely: all 262 of them in the ETS Directive.
    """
    if node.kind is Kind.PARAGRAPH and node.key and not node.text:
        return f"{node.key}. "
    parent = node.parent
    if (parent is not None and parent.kind is Kind.PARAGRAPH and parent.key
            and not parent.text and parent.children
            and parent.children[0] is node):
        return f"{parent.key}. "
    return ""


def from_node(node: Node) -> list[Provision]:
    """The blocks a provision already in the act is printed as.

    In document order, and carrying the same text in the same pieces as
    `Node.full_text` joins, plus the paragraph number the act keeps outside them.
    That number is part of the old text so that a replacement restating it — which
    is how payloads are drafted — diffs against it instead of striking it and
    inserting the same digit back.
    """
    blocks = [Provision(n.kind, n.key, n.text) for n in node.walk() if n.text]
    number = opening_number(node)
    # Never in front of a marker of its own, which would read "(a) 1. …".
    if number and blocks and blocks[0].key is None:
        blocks[0].text = number + blocks[0].text
    return blocks


# ------------------------------------------------------------------------ layout


def _dequote(lines: list[str]) -> list[str]:
    """Drop the payload's outer quotes without disturbing anyone's indentation."""
    out = list(lines)
    out[0] = re.sub(rf"^(\s*)[{OPEN_QUOTES}{CLOSE_QUOTES}]\s?", r"\1", out[0])
    out[-1] = re.sub(rf"[{CLOSE_QUOTES}]\s*[;.]?\s*$", "", out[-1])
    return [line for line in out if line.strip()]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _split_articles(lines: list[str]) -> list[tuple[str | None, list[str]]]:
    """Cut the payload at its article headings.

    "the following Articles are inserted:" is followed by one quoted block holding
    all of them, so a payload may carry several.
    """
    groups: list[tuple[str | None, list[str]]] = []
    current: tuple[str | None, list[str]] = (None, [])
    for line in lines:
        match = ARTICLE_HEADING.match(line.strip())
        if match is not None:
            if current[1] or current[0] is not None:
                groups.append(current)
            current = (match.group("key"), [])
        else:
            current[1].append(line)
    groups.append(current)
    return [g for g in groups if g[0] is not None or g[1]]


def _take_title(lines: list[str], body_indent: int) -> tuple[str | None, list[str]]:
    """An article's title: the centred lines between its number and its body.

    Centring means the indent varies with the line's length rather than marking a
    level, so the test is only that the line is indented past the body at all — a
    title long enough to fill the column clears it by a single space.
    """
    taken: list[str] = []
    for line in lines:
        text = line.strip()
        if _indent(line) <= body_indent:
            break
        if NUMBERED.match(text) or LETTERED.match(text):
            break
        taken.append(text)
    if not taken:
        return None, lines
    return " ".join(taken), lines[len(taken):]


@dataclass
class _Level:
    """An open list of points: where it sits, and the last marker seen in it.

    `wrapped` is where the current point's continuation lines were found to sit, once
    one has been seen. Payloads differ: some indent a point's wrapped lines past its
    marker, some set them flush with it, and which of the two this payload does is
    what decides whether an unmarked line back at the marker's own indent is more of
    the point or the text that follows the list.
    """

    indent: int
    key: str
    wrapped: int | None = None


def _place(levels: list[_Level], key: str) -> int | None:
    """The depth `key` belongs at, or None if it opens nothing and continues nothing.

    Indentation is the clearer signal but it is not always there: a payload of points
    is routinely set as one wrapped block at a single margin — points (aj) to (ao)
    added to Article 3 of the ETS Directive, or point (e) with its two sub-points.
    Sequence is what remains, and it is the same test `a2t.parse.segment` applies to
    the instructions themselves: a marker either succeeds the last one at some open
    level, or opens the next level down with that sequence's first marker.
    """
    for depth in range(len(levels) - 1, -1, -1):
        if _succeeds(levels[depth].key, key):
            return depth
    if not levels:
        # The first marker of a payload sets the sequence, whatever it starts at:
        # points added to an article continue its existing lettering.
        return 0
    return len(levels) if key in ("a", "i", "1") else None


def _succeeds(previous: str, key: str) -> bool:
    """Is `key` the next marker after `previous` in the same sequence?"""
    if previous.isdigit() and key.isdigit():
        return int(key) == int(previous) + 1
    if previous in ROMAN and key in ROMAN:
        # "i" after "h" is a letter, not a numeral; the alpha reading is tried first.
        if _alpha_successor(previous) == key:
            return True
        return ROMAN.index(key) == ROMAN.index(previous) + 1
    if previous.isalpha() and key.isalpha():
        return _alpha_successor(previous) == key
    return False


def _leaves_points(levels: list[_Level], indent: int) -> bool:
    """Has an unmarked line at `indent` stepped back out of the open points?

    Two ways of telling, and a payload supplies whichever it can. Where the point's
    own wrapped lines are indented past its marker, anything set back at the marker
    has left it — this is Article 3c(6), whose points wrap at column 11 and whose
    closing subparagraph ("Fuels shall be eligible …") returns to column 5. Where the
    payload wraps flush instead, that test says nothing, and only a line outdented
    past where the list itself begins can be read as leaving it.

    A payload that wraps flush *and* never outdents keeps its closing text inside the
    last point. Nothing in the layout distinguishes the two there, and inventing a
    boundary would move words from one provision to another.
    """
    innermost = levels[-1]
    if innermost.wrapped is not None:
        return indent < innermost.wrapped
    return indent + SLACK < levels[0].indent


def _body(lines: list[str], body_indent: int) -> list[Provision]:
    """Numbered paragraphs, their subparagraphs and their points, in reading order."""
    out: list[Provision] = []
    levels: list[_Level] = []  # the open point levels, outermost first

    for line in lines:
        indent, text = _indent(line), line.strip()
        numbered, lettered = NUMBERED.match(text), LETTERED.match(text)
        at_body = indent <= body_indent + SLACK

        if numbered and at_body:
            levels = []
            out.append(Provision(Kind.PARAGRAPH, numbered.group("key"),
                                 numbered.group("rest")))
            continue

        marker = lettered or numbered
        depth = _place(levels, marker.group("key")) if marker else None
        if depth is not None:
            del levels[depth:]
            levels.append(_Level(indent, marker.group("key")))
            kind = Kind.POINT if depth == 0 else Kind.SUBPOINT
            out.append(Provision(kind, marker.group("key"), marker.group("rest")))
            continue

        # A line set back outside the points that precede it has left them: the
        # paragraph continues with a subparagraph, which carries no marker of its own.
        if levels and _leaves_points(levels, indent):
            levels = []
            out.append(Provision(Kind.SUBPARAGRAPH, None, text))
            continue
        if levels and levels[-1].wrapped is None and indent > levels[-1].indent:
            levels[-1].wrapped = indent

        if not out:
            out.append(Provision(Kind.SUBPARAGRAPH, None, text))
            continue

        # Anything else is the wrapped continuation of the block above it.
        out[-1].text = f"{out[-1].text} {text}".strip()

    return out


def _from_layout(lines: Sequence[str]) -> list[Provision]:
    provisions: list[Provision] = []
    for key, group in _split_articles(_dequote(list(lines))):
        if key is not None and not group:
            provisions.append(Provision(Kind.ARTICLE, key))
            continue
        title, rest = (None, group)
        if key is not None:
            # A centred title stands clear of everything under it, so the shallowest
            # line in the article is the one to measure it against.
            title, rest = _take_title(group, min(_indent(l) for l in group))
            provisions.append(Provision(Kind.ARTICLE, key, title=title))
        # The body's own left margin is where it starts. Taking the shallowest line
        # instead would let one stray outdented block — proposals contain them —
        # re-level the whole payload, and a numbered paragraph indented past it would
        # read as a point of something.
        provisions.extend(_body(rest, _indent(rest[0]) if rest else 0))
    return provisions


# -------------------------------------------------------------------------- flat


def _alpha_successor(marker: str) -> str:
    chars = list(marker)
    for i in range(len(chars) - 1, -1, -1):
        if chars[i] != "z":
            chars[i] = chr(ord(chars[i]) + 1)
            return "".join(chars)
        chars[i] = "a"
    return "a" + "".join(chars)


FLAT_MARKER = re.compile(
    r"(?<=\S)\s+(?P<marker>\d{1,2}[a-z]?\.|\([a-z]{1,4}\))(?=\s+\S)"
)

# A marker is recognised by what precedes it, so a payload that opens with one has
# to be given something to follow. A sentence end is what the payload's first block
# would have had if it were not the first.
PAD = ". "

# An article's title is what stands between its number and its first paragraph: short,
# and — unlike a sentence of the body — not closed off.
TITLE_LIMIT = 120


def _accepted(hits: list[re.Match], titled: bool = False) -> list[re.Match]:
    """Keep only markers that continue their own sequence.

    A payload is dense with text shaped like a marker — "(a)" opening a citation,
    "2. " inside a figure. A marker earns a split twice over: by being the successor
    of the last one accepted at its level, which is the test `a2t.parse.segment`
    applies to the instructions themselves, and by following the end of a sentence.

    The one marker excused the second test is the "1." that opens the paragraphs of
    an article the payload has just named, because what precedes it is the article's
    title, which carries no full stop. Outside that one position the exemption would
    cut a provision at any figure — "reduced by 1. Member States shall ...".
    """
    kept: list[re.Match] = []
    last: dict[str, str] = {}
    for hit in hits:
        raw = hit.group("marker")
        numeric = raw.endswith(".")
        key = raw.rstrip(".") if numeric else raw.strip("()")
        level = "num" if numeric else "alpha"
        previous = last.get(level)
        if previous is None:
            ok = key in ("1", "a")
        elif numeric:
            ok = key.isdigit() and previous.isdigit() and int(key) == int(previous) + 1
        else:
            ok = key == _alpha_successor(previous)
        opens_paragraphs = titled and numeric and key == "1" and previous is None
        if not opens_paragraphs:
            ok = ok and hit.string[hit.start() - 1] in ".;:"
        if not ok:
            continue
        last[level] = key
        kept.append(hit)
    return kept


def _titleish(text: str) -> bool:
    return len(text) <= TITLE_LIMIT and not text.rstrip().endswith((".", ";", ":"))


def _from_flat(text: str, named: str | None = None) -> list[Provision]:
    provisions: list[Provision] = []
    head = re.match(rf"^\s*Article\s+({re.escape(named)})\b[.:]?\s+(.*)$", text,
                    re.I | re.S) if named else None
    if head is not None and head.group(2).strip():
        provisions.append(Provision(Kind.ARTICLE, head.group(1)))
        text = head.group(2).strip()

    padded = PAD + text
    hits = _accepted(list(FLAT_MARKER.finditer(padded)), titled=bool(provisions))
    if not hits:
        if text:
            provisions.append(Provision(Kind.SUBPARAGRAPH, None, text))
        return provisions

    lead = padded[len(PAD):hits[0].start()].strip()
    if lead and provisions and provisions[-1].is_article and _titleish(lead):
        provisions[-1].title = lead
    elif lead:
        provisions.append(Provision(Kind.SUBPARAGRAPH, None, lead))
    for index, hit in enumerate(hits):
        end = hits[index + 1].start() if index + 1 < len(hits) else len(padded)
        raw = hit.group("marker")
        numeric = raw.endswith(".")
        provisions.append(Provision(
            Kind.PARAGRAPH if numeric else Kind.POINT,
            raw.rstrip(".") if numeric else raw.strip("()"),
            padded[hit.end():end].strip(),
        ))
    return provisions


# -------------------------------------------------------------------------- api


def structure(text: str, layout: Sequence[str] | None = None,
              named: str | None = None) -> list[Provision]:
    """Split a payload into the blocks it should be printed as.

    `layout` is the payload's source lines with their indentation, `named` the article
    the instruction says it inserts — used only when there is no layout to read.
    Returns at least one provision for any non-empty payload, so a caller can always
    render the result instead of special-casing an empty one.
    """
    text = (text or "").strip()
    if not text:
        return []
    if layout:
        provisions = _from_layout(layout)
        if provisions:
            return provisions
    return _from_flat(text, named)


__all__ = ["Provision", "from_node", "printed", "structure"]
