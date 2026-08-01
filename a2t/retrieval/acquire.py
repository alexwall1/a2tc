"""Obtain the best usable consolidated version of a base act.

The version in force when the proposal was drafted is the one to use, but it is not
always retrievable: Cellar lists consolidations that have no English expression. The
first consolidation of Regulation (EU) 2020/1056 (2020-07-31) is one — its metadata
comes back in Bulgarian and it carries no text at all.

Rather than abort, fall back to the nearest version that does have content and say so
loudly. A substituted version is a real risk to accuracy — provision numbering may have
moved between consolidations — so the warning travels into the run report and into the
emitted document's front matter.

A second, independent doubt attaches to the same choice: the version in force at the
proposal date may not have been published yet. See `versions.publication_warning`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from a2t.retrieval.base import BaseActProvider, RetrievalError, RetrievedAct
from a2t.retrieval.versions import (
    list_versions, parse_pinned, publication_warning, select_version,
)


@dataclass
class Acquisition:
    retrieved: RetrievedAct
    pinned: str          # the version actually used
    requested: str       # the version in force at the proposal date
    warnings: tuple[str, ...] = ()

    @property
    def substituted(self) -> bool:
        return self.pinned != self.requested

    @property
    def warning(self) -> str | None:
        """Everything the reader needs to know about this version, or None."""
        return "\n".join(self.warnings) or None


def acquire(provider: BaseActProvider, base_celex: str,
            on_date: date) -> Acquisition:
    """Fetch the act, falling back to the nearest usable consolidation."""
    requested = select_version(base_celex, on_date)
    # Independent of retrievability: the version in force may postdate the proposal.
    pending = publication_warning(base_celex, on_date)
    notes = (pending,) if pending else ()

    try:
        return Acquisition(provider.fetch(requested), requested, requested, notes)
    except RetrievalError as first_error:
        pass

    available = list_versions(base_celex)
    requested_date = parse_pinned(requested)
    anchor = requested_date[1] if requested_date else on_date

    # Nearest first; where two are equally distant, prefer the earlier one, since a
    # later consolidation contains changes the drafters had not seen.
    candidates = sorted(
        (d for d in available if d != anchor),
        key=lambda d: (abs((d - anchor).days), d > anchor),
    )

    for candidate in candidates:
        pinned = f"{base_celex}-{candidate.strftime('%Y%m%d')}"
        try:
            retrieved = provider.fetch(pinned)
        except RetrievalError:
            continue
        direction = "later" if candidate > anchor else "earlier"
        return Acquisition(
            retrieved, pinned, requested,
            warnings=notes + (
                f"the consolidated version in force at {on_date.isoformat()} "
                f"({requested}) has no retrievable English text; used {pinned} "
                f"instead, which is {direction}. Provision numbering may differ from "
                f"what the drafters worked against — verify the affected changes.",
            ),
        )

    raise RetrievalError(
        f"no usable consolidated version of {base_celex}: {first_error}"
    )
