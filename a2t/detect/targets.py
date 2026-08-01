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
    r"^(?:Article\s+(?P<scope>[\w.]+)\s+of\s+)?"
    r"(?P<act>.+?)\s+(?:is|shall\s+be)\s+(?:hereby\s+)?amended\s+as\s+follows\s*:",
)

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


def _preceding_article_number(lines: list[Line], index: int) -> str | None:
    """Nearest ``Article N`` heading above `index` (the amending act's own article)."""
    for i in range(index - 1, max(index - 12, -1), -1):
        match = ARTICLE_HEADING.match(lines[i].stripped)
        if match:
            return match.group("number")
    return None


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

    for index, line in enumerate(operative):
        match = ANCHOR.match(line.stripped)
        if match is None:
            continue
        # An instruction container ("Article 3 is amended as follows:") has no
        # citation in the subject position; a target anchor does.
        citation = parse_citation(match.group("act"))
        if citation is None:
            continue

        celex = citation.celex
        detection.targets.append(
            Target(
                citation=citation,
                celex=celex,
                celex_source="eli" if celex in elis else "citation",
                root_scope=match.group("scope"),
                amending_article=_preceding_article_number(operative, index),
                anchor_line=line.lineno,
                header_line=headers.get(celex),
            )
        )

    # Footnotes cut both ways: they carry the ELI URIs that identify the amended acts,
    # but they also cite *other* acts' titles, which are themselves of the form
    # "... amending X and Y as regards Z". Those are not this document describing
    # itself, so corroboration runs over the text with furniture stripped.
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
