"""Obtain the best usable consolidated version of a base act.

The version in force when the proposal was drafted is the one to use, but it is not
always retrievable: Cellar lists consolidations that have no English expression. The
first consolidation of Regulation (EU) 2020/1056 (2020-07-31) is one — its metadata
comes back in Bulgarian and it carries no text at all.

Rather than abort, fall back to the nearest *earlier* version that does have content and
say so loudly. A substituted version is a real risk to accuracy — provision numbering may
have moved between consolidations — so the warning travels into the run report and into
the emitted document's front matter.

The fallback only ever goes backwards. A later consolidation carries amendments made
after the proposal was drafted, and where the proposal has since been adopted those are
its own: the conversion then compares the proposal with its own result, which no warning
makes readable. Where nothing earlier exists the act had not been amended when the
proposal was written, so the text in force was the act as adopted — sector 3, which is
retrieved instead. That is the right base text rather than a substitution, but the
document still says which it is, since a reader expects a consolidation.

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
    except RetrievalError as exc:
        # Bound to a name of its own: Python unbinds an `except ... as` variable when
        # the block ends, and both failure paths below still need to report it.
        first_error = exc

    available = list_versions(base_celex)
    requested_date = parse_pinned(requested)
    anchor = requested_date[1] if requested_date else on_date

    # Backwards only, nearest first. An earlier consolidation is out of date, which the
    # warning can carry; a later one holds amendments the drafters had not seen — and
    # where the proposal has since been adopted, its own. COM(2025) 524 against
    # 02021R1119-20260407 met its new Article 4(3) already enacted, so the amendment
    # diffed to nothing and the rest was inserted alongside the adopted text: the
    # proposal compared with its own result. There is no reading of that document a
    # warning could make safe, so it is not written.
    candidates = sorted((d for d in available if d < anchor), reverse=True)

    for candidate in candidates:
        pinned = f"{base_celex}-{candidate.strftime('%Y%m%d')}"
        try:
            retrieved = provider.fetch(pinned)
        except RetrievalError:
            continue
        return Acquisition(
            retrieved, pinned, requested,
            warnings=notes + (
                f"the consolidated version in force at {on_date.isoformat()} "
                f"({requested}) has no retrievable English text; used {pinned} "
                f"instead, which is earlier. Provision numbering may differ from "
                f"what the drafters worked against — verify the affected changes.",
            ),
        )

    later = sorted(d for d in available if d > anchor)
    if later:
        # Nothing consolidated before the proposal date means the act had not been
        # amended by then, so the text the drafters worked against was the act as
        # adopted. That is not a substitution but the right answer: sector 3 is the
        # law as it stood, where a later consolidation is the law as it became.
        as_adopted = f"3{base_celex[1:]}"
        try:
            retrieved = provider.fetch(as_adopted)
        except RetrievalError:
            raise RetrievalError(
                f"{requested} — the version of {base_celex} in force at "
                f"{on_date.isoformat()} — has no retrievable English text, neither "
                f"does any earlier consolidation, and the act as adopted "
                f"({as_adopted}) could not be retrieved either. The only "
                f"consolidations that exist are later (from {later[0].isoformat()}) "
                f"and already carry amendments made after the proposal was drafted, "
                f"possibly its own if it has since been adopted; converting against "
                f"one would compare the proposal with its own result."
            ) from first_error
        return Acquisition(
            retrieved, as_adopted, requested,
            warnings=notes + (
                f"{base_celex} had not been amended when the proposal was drafted, so "
                f"no consolidation of it existed; the base text is the act as adopted "
                f"({as_adopted}). Its first consolidation is "
                f"{later[0].isoformat()}, after the proposal date.",
            ),
        )

    raise RetrievalError(
        f"no usable consolidated version of {base_celex}: {first_error}"
    )
