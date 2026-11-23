"""Resolve legal-act citations to CELEX identifiers.

Two citation styles coexist in EU drafting and they order year and number
*oppositely*:

    Directive 2003/87/EC        year 2003, number   87  -> 32003L0087
    Regulation (EC) No 219/2009 year 2009, number  219  -> 32009R0219

The ``No`` prefix is the reliable signal for number-first. Where a document carries
ELI URIs — which most modern proposals do, in their footnotes — those are unambiguous
and are preferred over parsing the prose citation.

Nothing here is authoritative on its own: a constructed CELEX is a hypothesis, to be
confirmed in Phase 3 by checking the retrieved act's title against the citation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# CELEX descriptor letters. Delegated and implementing acts keep the parent letter.
ACT_LETTER = {"Directive": "L", "Regulation": "R", "Decision": "D"}

ELI_LETTER = {
    "dir": "L", "dir_del": "L", "dir_impl": "L",
    "reg": "R", "reg_del": "R", "reg_impl": "R",
    "dec": "D", "dec_del": "D", "dec_impl": "D",
}

EARLIEST_YEAR = 1951  # ECSC Treaty; nothing citable predates it.
LATEST_YEAR = 2099

CITATION = re.compile(
    r"\b(?P<kind>Directive|Regulation|Decision)\s+"
    r"(?:\((?:EU|EC|EEC|Euratom|EU,\s*Euratom)\)\s*)?"
    r"(?P<no>No\s+)?"
    r"(?P<a>\d{1,4})/(?P<b>\d{1,4})"
    r"(?:/(?:EC|EU|EEC|Euratom))?",
)

ELI = re.compile(
    r"data\.europa\.eu/eli/(?P<type>[a-z_]+)/(?P<year>\d{4})/(?P<num>\d+)"
)


def _plausible_year(value: int) -> bool:
    return EARLIEST_YEAR <= value <= LATEST_YEAR


@dataclass(frozen=True)
class Citation:
    """A parsed reference to a legal act."""

    raw: str
    kind: str  # Directive | Regulation | Decision
    year: int
    number: int

    @property
    def letter(self) -> str:
        return ACT_LETTER[self.kind]

    @property
    def celex(self) -> str:
        """Sector 3 — the act as adopted."""
        return f"3{self.year:04d}{self.letter}{self.number:04d}"

    @property
    def consolidated_celex(self) -> str:
        """Sector 0 — the consolidated family, unpinned."""
        return f"0{self.year:04d}{self.letter}{self.number:04d}"

    def __str__(self) -> str:
        return self.raw


def parse_citation(text: str) -> Citation | None:
    """Parse the first act citation in `text`, or None if there isn't a clear one."""
    match = CITATION.search(text)
    if match is None:
        return None

    kind = match.group("kind")
    a, b = int(match.group("a")), int(match.group("b"))
    a_raw = match.group("a")

    if match.group("no"):
        # "Regulation (EC) No 219/2009" — number first. Before 1999 the year had two
        # digits, "Regulation (EEC) No 2913/92", and always meant the 20th century.
        number, year = a, (1900 + b if len(match.group("b")) == 2 else b)
    elif len(a_raw) == 4 and _plausible_year(a):
        # "Directive 2003/87/EC", "Regulation (EU) 2015/757" — year first.
        year, number = a, b
    elif len(a_raw) == 2:
        # "Directive 96/61/EC" — two-digit year, always 20th century.
        year, number = 1900 + a, b
    elif len(match.group("b")) == 2 and not _plausible_year(a):
        # "Regulation (EC) 1257/96" — number first with no "No", year in two digits.
        number, year = a, 1900 + b
    elif _plausible_year(b):
        # Fall back to whichever group can be a year.
        number, year = a, b
    else:
        return None

    if not _plausible_year(year):
        return None
    return Citation(match.group(0), kind, year, number)


def iter_citations(text: str) -> list[Citation]:
    """Every act citation in `text`, in order, de-duplicated by CELEX."""
    found: dict[str, Citation] = {}
    for match in CITATION.finditer(text):
        citation = parse_citation(match.group(0))
        if citation is not None:
            found.setdefault(citation.celex, citation)
    return list(found.values())


def parse_eli(text: str) -> str | None:
    """CELEX from the first ELI URI in `text`, or None."""
    match = ELI.search(text)
    if match is None:
        return None
    letter = ELI_LETTER.get(match.group("type"))
    if letter is None:
        return None  # e.g. agree_interinstit — not a citable act type
    return f"3{int(match.group('year')):04d}{letter}{int(match.group('num')):04d}"


def eli_index(lines) -> dict[str, str]:
    """Map CELEX -> the ELI URI that produced it, across a set of lines.

    Built over the *whole* document: ELI URIs live in footnotes, which Phase 1 strips
    from the operative part as furniture.
    """
    index: dict[str, str] = {}
    for line in lines:
        for match in ELI.finditer(getattr(line, "text", str(line))):
            letter = ELI_LETTER.get(match.group("type"))
            if letter is None:
                continue
            celex = f"3{int(match.group('year')):04d}{letter}{int(match.group('num')):04d}"
            index.setdefault(celex, match.group(0))
    return index


def resolve(citation: Citation, elis: dict[str, str]) -> tuple[str, str]:
    """Resolve a citation to ``(celex, source)``.

    An ELI that agrees with the parsed citation raises confidence; the citation
    grammar alone still resolves, just with a weaker provenance label.
    """
    if citation.celex in elis:
        return citation.celex, "eli"
    return citation.celex, "citation"
