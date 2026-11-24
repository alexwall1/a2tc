"""Early-rejection sanity check, reusing the CLI's detection building blocks."""

from __future__ import annotations

from pathlib import Path

from a2t.detect.targets import Detection, detect_targets, find_proposal_date
from a2t.extract.deboilerplate import operative_part
from a2t.extract.pdf import ExtractionError, extract_lines, pdftotext_available

NOT_A_PDF_PREFIX = "This does not look like an EU legislative act: "
NOT_AN_AMENDMENT = (
    "This does not appear to be an EU Commission proposal to amend legislation: "
    "no amendment targets were found in the operative part."
)
NO_PROPOSAL_DATE = (
    "Could not read the proposal date from the cover page — a2t convert "
    "needs it to pick the correct consolidation."
)


class SanityError(RuntimeError):
    pass


def sanity_check(pdf_path: Path) -> Detection:
    """Reject files that `a2t convert` could never turn into tracked changes.

    Accepts iff the operative part names at least one amendment target and the
    cover page carries a proposal date — `convert` hard-fails on a missing date,
    and failing five minutes into a run is worse than rejecting at upload.
    """
    if not pdftotext_available():
        raise SanityError("pdftotext (poppler-utils) is not installed")
    try:
        lines = extract_lines(pdf_path)
        operative = operative_part(lines).lines
        detection = detect_targets(operative, lines)
    except (ExtractionError, ValueError) as exc:
        raise SanityError(NOT_A_PDF_PREFIX + str(exc)) from exc
    if not detection.targets:
        raise SanityError(NOT_AN_AMENDMENT)
    if find_proposal_date(lines) is None:
        raise SanityError(NO_PROPOSAL_DATE)
    return detection