"""Detect which legal acts an amending proposal actually amends.

Detection is **structural, not citation-based**. COM(2026) 616 final cites 51 distinct
acts but amends 2; every other citation is a cross-reference. Extracting citations
would be ~96% false positives.

Amendment targets occupy one syntactic position — an operative-article header followed
by an anchor sentence:

    Article 1
    Amendments to Directive 2003/87/EC          <- header
    Directive 2003/87/EC is amended as follows: <- anchor

The anchor pattern alone is not sufficient, because instruction containers share it
("Article 3 is amended as follows:"). The discriminator is that a *target* anchor has
a **citation** in the subject position, while an instruction container has an internal
reference. That one test separates the two cleanly.

The operative part is authoritative. The cover title and the legislative financial
statement are corroboration only — and in this document they disagree with each other,
so disagreements are reported rather than resolved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from a2t.detect.celex import Citation, eli_index, iter_citations, parse_citation
from a2t.extract.deboilerplate import strip_furniture
from a2t.extract.pdf import Line

ANCHOR = re.compile(
    # "Accordingly, Regulation (EC) No 1257/96 is hereby amended as follows:" — a
    # linking word before the act (Regulation (EC) No 219/2009, which amends No 1257/96).
    r"^(?:(?:Accordingly|Therefore|Consequently|Thus),\s+)?"
    r"(?:Article\s+(?P<scope>[\w.]+)\s+of\s+)?"
    r"(?P<act>.+?)\s+(?:is|shall\s+be)\s+(?:hereby\s+)?amended\s+as\s+follows\s*:",
)

def match_anchor(text: str):
    """The anchor match for a line, or None when it is not one that opens an act.

    Shared with the segmenter so the two cannot disagree about where a block starts.

    A line that names an annex in its subject is not an anchor for the act's articles:
    "In Directive 2003/87/EC, in Annex I, the entry ‘Aviation’ is amended as follows:"
    would otherwise read as the whole of the Directive being amended, and its points
    would be looked for among the articles.
    """
    match = ANCHOR.match(text)
    if match is None or re.search(r"\bAnnex(?:es)?\b", match.group("act")):
        return None
    return match


HEADER = re.compile(r"^Amendments?\s+to\s+(?P<act>.+?)\s*$", re.I)

# Greedy to the *last* "as regards": a title may pair each act with its own
# clause — "amending X as regards ... and Y as regards ...".
AMENDING_LIST = re.compile(r"\bamending\s+(?P<acts>.+)\s*,?\s+as\s+regards\b", re.I)

ARTICLE_HEADING = re.compile(r"^Article\s+(?P<number>\d+)\s*$")

# Commission proposals carry a cover-page date: "Brussels, 17.7.2026".
PROPOSAL_DATE = re.compile(
    r"^\s*\w+,\s*(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{4})\s*$"
)

# Adopted acts date themselves in their own title: "of 19 November 2008".
MONTHS = ("January February March April May June July August September "
          "October November December").split()
ADOPTED_DATE = re.compile(
    r"\bof\s+(?P<day>\d{1,2})\s+(?P<month>" + "|".join(MONTHS) + r")\s+"
    r"(?P<year>\d{4})\b", re.I,
)


def find_proposal_date(lines: list[Line]):
    """The date on the proposal's cover page.

    Needed to pick the consolidated version the drafters worked against.
    """
    from datetime import date

    for line in lines[:200]:
        match = PROPOSAL_DATE.match(line.text)
        if match:
            return date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )

    # An adopted act has no cover page; its title states the date of adoption.
    for line in lines[:120]:
        match = ADOPTED_DATE.search(line.text)
        if match:
            return date(
                int(match.group("year")),
                MONTHS.index(match.group("month").capitalize()) + 1,
                int(match.group("day")),
            )
    return None


@dataclass(frozen=True)
class Target:
    """An act the proposal amends, with the scope the amendments apply within."""

    citation: Citation
    celex: str
    celex_source: str  # "eli" | "citation"
    root_scope: str | None  # e.g. "1" when only Article 1 of the act is amended
    amending_article: str | None  # article of the *amending* act carrying it
    anchor_line: int
    header_line: int | None = None
    # The article carries one instruction that names the act itself, with no
    # "is amended as follows" line above it. See `find_anchors`.
    inline: bool = False

    @property
    def consolidated_celex(self) -> str:
        return self.citation.consolidated_celex

    def describe(self) -> str:
        scope = f"Article {self.root_scope}" if self.root_scope else "whole act"
        return f"{self.citation.raw} -> {self.consolidated_celex} ({scope})"


@dataclass(frozen=True)
class Discrepancy:
    kind: str
    message: str
    line: int | None = None


@dataclass
class Detection:
    targets: list[Target] = field(default_factory=list)
    discrepancies: list[Discrepancy] = field(default_factory=list)
    # Acts whose annexes are what the document amends. Filled only when nothing else was
    # found: an annex-only amendment is out of scope, and without this it is
    # indistinguishable from a document the detector failed to read.
    annexes: list[str] = field(default_factory=list)

    def explain_empty(self) -> str:
        """Why there is nothing to convert, for the one who ran it."""
        if self.annexes:
            return (f"the document amends only annexes of {', '.join(self.annexes)}; "
                    f"annexes are out of scope")
        return "no amendment targets found in the operative part"


def _blocks(lines: list[Line]) -> list[tuple[int, str]]:
    """Group consecutive non-blank lines into ``(first_lineno, joined_text)``."""
    out: list[tuple[int, str]] = []
    buf: list[Line] = []
    for line in lines:
        if line.blank:
            if buf:
                out.append((buf[0].lineno, " ".join(b.stripped for b in buf)))
                buf = []
        else:
            buf.append(line)
    if buf:
        out.append((buf[0].lineno, " ".join(b.stripped for b in buf)))
    return out


# An amending sentence is one that carries an operation; a recital saying an annex
# "should be amended" does not.
_AMENDING_VERB = re.compile(
    r"\b(?:is|are|shall\s+be)\s+(?:hereby\s+)?"
    r"(?:amended|replaced|deleted|inserted|added|repealed)\b", re.I)
_ANNEX = re.compile(r"\bAnnex(?:es)?\b")


def annexes_amended(operative: list[Line]) -> list[str]:
    """The acts whose annexes the operative part amends, in order of first mention."""
    found: dict[str, str] = {}
    for _, text in _blocks(operative):
        for sentence in re.split(r"(?<=[:;.])\s+", text):
            if _ANNEX.search(sentence) and _AMENDING_VERB.search(sentence):
                for citation in iter_citations(sentence):
                    found.setdefault(citation.celex, citation.raw)
    return list(found.values())


def _preceding_article_number(lines: list[Line], index: int) -> str | None:
    """Nearest ``Article N`` heading above `index` (the amending act's own article)."""
    for i in range(index - 1, max(index - 12, -1), -1):
        match = ARTICLE_HEADING.match(lines[i].stripped)
        if match:
            return match.group("number")
    return None


@dataclass(frozen=True)
class Anchor:
    """A line in the operative part that opens the amendments of one act."""

    index: int
    citation: Citation
    scope: str | None
    inline: bool = False
    # For an inline anchor, the instruction as the amending act words it with the clause
    # naming the act cut out — a span removed from the source, never text written.
    text: str | None = None


# An article that is itself one instruction:
#   "Article 97 of Directive 2006/112/EC is replaced by the following:"
#   "In Article 12 of Directive 2003/87/EC, the following paragraphs are added:"
#   "The following Article is inserted in Directive 2006/112/EC:"
# The verb must be on the same line as the citation, so a wrapped sentence is left
# alone rather than guessed at.
_INLINE_VERB = re.compile(
    r"\b(?:is|are|shall\s+be)\s+(?:hereby\s+)?(?:replaced|deleted|inserted|added)\b"
    r"|\b(?:replaced|inserted|added)\s+by\s+the\s+following\b", re.I)
_QUOTE_OPENERS = "‘'“\""


def _without_act(sentence: str, citation: Citation) -> str | None:
    """`sentence` with the clause naming the act removed, or None if it cannot be."""
    raw = re.escape(citation.raw)
    # "In Directive 2006/112/EC, the following Articles are inserted:" — the whole
    # opening clause is about the act and nothing else.
    lead = re.match(rf"^In\s+{raw}\s*,\s*(?P<rest>\S.*)$", sentence, re.S)
    if lead:
        return lead.group("rest")
    cut = re.sub(rf"\s+(?:of|in|to)\s+{raw}", "", sentence, count=1)
    return cut if cut != sentence else None


def _opens_amending_article(operative: list[Line], index: int) -> bool:
    """Is line `index` the first text of an amending article?

    The line above is "Article N", or a short heading ("Amendment to Regulation …")
    that is itself below one. This is what keeps a sentence of quoted legislation that
    happens to say "is replaced" from being read as an instruction.
    """
    above = [l.stripped for l in operative[max(0, index - 6):index] if not l.blank]
    if not above:
        return False
    if ARTICLE_HEADING.match(above[-1]):
        return True
    return (len(above) >= 2 and ARTICLE_HEADING.match(above[-2]) is not None
            and len(above[-1]) < 100 and not above[-1].endswith((":", ";", ".")))


def find_anchors(operative: list[Line]) -> list[Anchor]:
    """Every line that opens the amendments of one act, in document order.

    Detection and segmentation both read this, so they cannot disagree about where
    a block starts.
    """
    found: list[Anchor] = []
    for index, line in enumerate(operative):
        text = line.stripped
        match = match_anchor(text)
        if match is not None:
            citation = parse_citation(match.group("act"))
            if citation is not None:
                found.append(Anchor(index, citation, match.group("scope")))
            continue
        if (not text or text[0] in _QUOTE_OPENERS or not _INLINE_VERB.search(text)
                or re.search(r"\bAnnex(?:es)?\b", text)
                or not _opens_amending_article(operative, index)):
            continue
        citation = parse_citation(text)
        cut = _without_act(text, citation) if citation is not None else None
        if citation is not None and cut is not None:
            found.append(Anchor(index, citation, None, inline=True, text=cut))
    return sorted(found + _heading_anchors(operative, found), key=lambda a: a.index)


def _directly_under_article(operative: list[Line], index: int) -> bool:
    """Is the nearest non-blank line above `index` an "Article N" heading?"""
    for line in reversed(operative[max(0, index - 4):index]):
        if not line.blank:
            return ARTICLE_HEADING.match(line.stripped) is not None
    return False


def _heading_anchors(operative: list[Line], found: list[Anchor]) -> list[Anchor]:
    """An article headed "Amendments to Regulation (EU) 2018/1862" whose text never says so.

    Some proposals name the act only in the article's heading and then amend it in
    sentences that do not repeat it: "(1) In Article 2, paragraph 2 is replaced by the
    following:" (COM(2020) 791), or "Article 18 is amended as follows:" under
    "Amendments to Article 18 of Regulation (EU) No 516/2014" (COM(2018) 719). With
    no anchor of its own, such an article was not read at all. The heading is the
    anchor only where nothing between it and the next article already is one, so an
    article that does name the act keeps the reading it always had.
    """
    taken = {a.index for a in found}
    out: list[Anchor] = []
    headings = [i for i, line in enumerate(operative) if ARTICLE_HEADING.match(line.stripped)]
    for index, line in enumerate(operative):
        match = HEADER.match(line.stripped)
        # A heading is the line right under "Article N". The same words also begin a
        # wrapped line of the entry-into-force article ("amendments to Article 10d of
        # Directive 2003/87/EC, Article 1(20) regarding …"), which is no heading.
        if match is None or not _directly_under_article(operative, index):
            continue
        citation = parse_citation(match.group("act"))
        if citation is None:
            continue
        following = next((h for h in headings if h > index), len(operative))
        if any(index < a < following for a in taken):
            continue
        scope = re.match(r"^Article\s+(?P<scope>[\w.]+)\s+of\s+", match.group("act"))
        out.append(Anchor(index, citation, scope.group("scope") if scope else None))
    return out


def detect_targets(operative: list[Line], all_lines: list[Line] | None = None) -> Detection:
    """Find amendment targets in the operative part.

    `all_lines` is the whole document, used to resolve ELI URIs — they live in
    footnotes, which Phase 1 strips from the operative part as furniture.
    """
    elis = eli_index(all_lines if all_lines is not None else operative)
    detection = Detection()

    headers: dict[str, int] = {}
    for line in operative:
        match = HEADER.match(line.stripped)
        if match:
            citation = parse_citation(match.group("act"))
            if citation is not None:
                headers.setdefault(citation.celex, line.lineno)

    for anchor in find_anchors(operative):
        index, citation = anchor.index, anchor.citation
        line = operative[index]
        celex = citation.celex
        detection.targets.append(
            Target(
                citation=citation,
                celex=celex,
                celex_source="eli" if celex in elis else "citation",
                root_scope=anchor.scope,
                amending_article=_preceding_article_number(operative, index),
                anchor_line=line.lineno,
                header_line=headers.get(celex),
                inline=anchor.inline,
            )
        )

    # Footnotes cut both ways: they carry the ELI URIs that identify the amended acts,
    # but they also cite *other* acts' titles, which are themselves of the form
    # "... amending X and Y as regards Z". Those are not this document describing
    # itself, so corroboration runs over the text with furniture stripped.
    if not detection.targets:
        detection.annexes = annexes_amended(operative)

    source = all_lines if all_lines is not None else operative
    detection.discrepancies = corroborate(detection.targets, strip_furniture(source).lines)
    return detection


def corroborate(targets: list[Target], all_lines: list[Line]) -> list[Discrepancy]:
    """Compare the operative part against the document's own summaries of itself.

    `all_lines` must already have page furniture and document footnotes removed.
    """
    found: list[Discrepancy] = []
    detected = {t.celex for t in targets}

    for target in targets:
        if target.header_line is None:
            found.append(
                Discrepancy(
                    "missing-header",
                    f"{target.citation.raw} is amended but has no "
                    f"'Amendments to ...' header",
                    target.anchor_line,
                )
            )

    seen: set[frozenset[str]] = set()
    for lineno, text in _blocks(all_lines):
        match = AMENDING_LIST.search(text)
        if match is None:
            continue
        claimed = {c.celex: c for c in iter_citations(match.group("acts"))}
        if not claimed or frozenset(claimed) in seen:
            continue
        seen.add(frozenset(claimed))

        extra = set(claimed) - detected
        missing = detected - set(claimed)
        if extra:
            names = ", ".join(claimed[c].raw for c in sorted(extra))
            found.append(
                Discrepancy(
                    "claimed-not-amended",
                    f"document claims to amend {names}, but the operative part "
                    f"does not — ignored",
                    lineno,
                )
            )
        if missing:
            found.append(
                Discrepancy(
                    "amended-not-claimed",
                    f"operative part amends {', '.join(sorted(missing))} but this "
                    f"summary omits it",
                    lineno,
                )
            )
    return found
