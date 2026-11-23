"""The retrieval boundary.

Everything specific to `eurlex-builder` lives behind this protocol, so the rest of the
app depends on a retrieved act rather than on a 0.x library's output schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from a2t.detect.celex import Citation


class RetrievalError(RuntimeError):
    pass


@dataclass
class RetrievedAct:
    """A base act as fetched, before structural assembly."""

    celex: str  # the pinned CELEX actually retrieved
    title: str
    full_text_html: str
    text_units: Any  # polars DataFrame — coordinates and cross-check
    version_date: str | None = None

    @property
    def has_structure(self) -> bool:
        return bool(self.full_text_html and self.full_text_html.strip())


class BaseActProvider(Protocol):
    def fetch(self, celex: str) -> RetrievedAct: ...


def title_matches(citation: Citation, title: str) -> bool:
    """Confirm a constructed CELEX actually named the act we meant.

    A CELEX built from a prose citation is a hypothesis. PDF extraction glues footnote
    markers onto citation numbers ("Regulation (EU) 2019/33141" is really 2019/331 plus
    footnote 41), so a wrong-but-plausible CELEX is a real possibility. The act's own
    title carries its number, which settles it.
    """
    if not title:
        return False
    normalised = re.sub(r"\s+", " ", title)
    # "2003/87/EC" or "(EU) 2015/1814" — the number pair is the discriminator.
    forms = [
        rf"\b{citation.year}/{citation.number}\b",
        # Number-first citation styles: "No 219/2009".
        rf"\b{citation.number}/{citation.year}\b",
    ]
    if citation.year < 2000:
        # Pre-2000 acts are cited with a two-digit year, and that is how their own
        # titles read: "Council Directive 92/106/EEC", never "1992/106".
        forms.append(rf"\b{citation.year % 100:02d}/{citation.number}\b")
        # …and a number-first citation does the same: "No 1257/96", "No 2913/92".
        forms.append(rf"\b{citation.number}/{citation.year % 100:02d}\b")
    return any(re.search(form, normalised) for form in forms)
