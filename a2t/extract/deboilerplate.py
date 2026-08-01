"""Remove page furniture and isolate the operative part of an amending act.

Two kinds of footnote look alike after PDF extraction and must be treated oppositely:

* **document footnotes** — the numbered apparatus at the bottom of a page. Noise.
* **payload footnotes** — introduced by a ``___________`` rule *inside* a quoted
  replacement text, carrying ``*`` markers. These are part of the amendment and must
  survive, or the emitted legislation loses its footnotes.

Everything removed is recorded with a reason, so the reconciliation report can account
for every dropped line rather than silently discarding text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from a2t.extract.pdf import Line

# "EN            57                    EN"  (also "EN     EN" on unnumbered pages)
PAGE_FOOTER = re.compile(r"\s*([A-Z]{2})\s+\d*\s*\1\s*$")

# Official Journal page header, used by adopted acts instead of the "EN … EN"
# footer that COM proposals carry:
#     "L 130/158        EN        Official Journal of the European Union"
#     "16.5.2023        EN        Official Journal of the European Union"
# The layout constraint matters: inline citations such as "(OJ L 275, 25.10.2003,
# p. 32)" must survive.
OJ_HEADER = re.compile(
    r"\s*(?:L\s*\d+/\d+|\d{1,2}\.\d{1,2}\.\d{4})\s+[A-Z]{2}\s+"
    r"Official Journal of the European Union"
    r"(?:\s+(?:L\s*\d+/\d+|\d{1,2}\.\d{1,2}\.\d{4}))?\s*"
)

# Reading-order extraction (two-column acts) puts these inline rather than at the
# foot of a page, so the block detector never sees them.
INLINE_FURNITURE = re.compile(
    r"\s*(?:ELI:\s*http\S*|[A-Z]{2}|\d{1,4}/\d{1,4})\s*"
)

# A standalone footnote number at the foot of a page.
FOOTNOTE_MARKER = re.compile(r"\s{0,12}\d{1,3}\s*$")

# The rule introducing footnotes inside a quoted payload.
PAYLOAD_RULE = re.compile(r"\s*_{3,}\s*$")

# Operative-part boundaries.
# "HAVE ADOPTED" for Parliament and Council acts, "HAS ADOPTED" for Commission ones.
ENACTING_FORMULA = re.compile(r"HA(?:VE|S)\s+ADOPTED\s+THIS\s+\w+", re.I)
CLOSING_FORMULA = re.compile(r"^\s*(Done at\b|For the European Parliament\b)", re.I)

# A document footnote must sit in the bottom of its page.
FOOTNOTE_ZONE = 0.55


@dataclass
class CleanResult:
    lines: list[Line]
    removed: list[tuple[Line, str]] = field(default_factory=list)

    @property
    def pages(self) -> int:
        return len({line.page for line in self.lines})

    def reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, reason in self.removed:
            counts[reason] = counts.get(reason, 0) + 1
        return counts

    def removed_for(self, reason: str) -> list[Line]:
        """Lines dropped for a given reason.

        Document footnotes are furniture for parsing but *evidence* for CELEX
        resolution — the ELI URIs identifying the amended acts live there, not in the
        operative text. Phase 3 reads them back through this accessor.
        """
        return [ln for ln, r in self.removed if r == reason]


def find_operative_part(lines: list[Line]) -> tuple[int, int]:
    """Index range ``[start, end)`` of the operative part.

    Starts at the enacting formula ("HAVE ADOPTED THIS DIRECTIVE:") and ends at the
    closing formula ("Done at Brussels,"), which excludes both the explanatory
    memorandum and the legislative financial statement that follows the signature.
    """
    start = next(
        (i for i, ln in enumerate(lines) if ENACTING_FORMULA.search(ln.text)), None
    )
    if start is None:
        raise ValueError("no enacting formula found — is this an amending act?")

    end = next(
        (i for i in range(start + 1, len(lines)) if CLOSING_FORMULA.match(lines[i].text)),
        None,
    )
    if end is None:
        raise ValueError("no closing formula found after the enacting formula")
    return start, end


def _page_spans(lines: list[Line]) -> dict[int, tuple[int, int]]:
    """Map page number -> [first, last] index within `lines`."""
    spans: dict[int, tuple[int, int]] = {}
    for i, ln in enumerate(lines):
        lo, hi = spans.get(ln.page, (i, i))
        spans[ln.page] = (min(lo, i), max(hi, i))
    return spans


def _footnote_block_start(lines: list[Line], lo: int, hi: int) -> int | None:
    """Index where a page's document-footnote block begins, if any.

    A page may carry several footnotes. Once the block starts, everything below it to
    the foot of the page is apparatus — so we look for the *topmost* marker, not the
    lowest, or earlier footnotes on the same page survive.

    A payload rule (``___________``) anywhere on the page means the numbered markers
    below it belong to quoted text, so nothing is stripped.
    """
    if any(PAYLOAD_RULE.match(lines[i].text) for i in range(lo, hi + 1)):
        return None

    height = hi - lo + 1
    threshold = lo + int(height * FOOTNOTE_ZONE)

    for i in range(threshold, hi + 1):
        if lines[i].blank or not FOOTNOTE_MARKER.fullmatch(lines[i].text):
            continue
        # Must be followed by indented continuation text.
        nxt = next((j for j in range(i + 1, hi + 1) if not lines[j].blank), None)
        if nxt is None or lines[nxt].indent <= lines[i].indent:
            continue
        # Everything from here to the foot of the page must look like apparatus:
        # further markers, their indented text, or blanks. This stops a stray
        # numeral in body text from swallowing real content.
        marker_indent = lines[i].indent
        if all(
            lines[j].blank
            or lines[j].indent > marker_indent
            or FOOTNOTE_MARKER.fullmatch(lines[j].text)
            for j in range(i, hi + 1)
        ):
            return i
    return None


def strip_furniture(lines: list[Line]) -> CleanResult:
    """Drop page footers and document-footnote blocks."""
    drop: dict[int, str] = {}

    for i, ln in enumerate(lines):
        if PAGE_FOOTER.fullmatch(ln.text):
            drop[i] = "page footer"
        elif OJ_HEADER.fullmatch(ln.text):
            drop[i] = "OJ page header"
        elif INLINE_FURNITURE.fullmatch(ln.text) and ln.text.strip():
            drop[i] = "inline furniture"

    body = [i for i in range(len(lines)) if i not in drop]
    if body:
        for _, (lo, hi) in _page_spans(lines).items():
            # Recompute the page span over non-footer lines.
            page_idx = [i for i in range(lo, hi + 1) if i not in drop]
            if not page_idx:
                continue
            start = _footnote_block_start(lines, page_idx[0], page_idx[-1])
            if start is not None:
                for i in range(start, page_idx[-1] + 1):
                    drop.setdefault(i, "document footnote")

    kept = [ln for i, ln in enumerate(lines) if i not in drop]
    removed = [(lines[i], reason) for i, reason in sorted(drop.items())]
    return CleanResult(kept, removed)


def collapse_blank_runs(lines: list[Line]) -> list[Line]:
    """Reduce runs of blank lines to a single separator."""
    out: list[Line] = []
    for ln in lines:
        if ln.blank and out and out[-1].blank:
            continue
        out.append(ln)
    return out


def operative_part(lines: list[Line]) -> CleanResult:
    """Full Phase 1 pipeline: bound the operative part, then strip furniture."""
    start, end = find_operative_part(lines)
    result = strip_furniture(lines[start:end])
    result.lines = collapse_blank_runs(result.lines)
    return result
