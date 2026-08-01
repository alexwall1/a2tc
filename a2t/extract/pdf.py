"""PDF text extraction.

Uses ``pdftotext -layout``, which preserves the column alignment that EU legislative
drafting relies on: indentation is what distinguishes an instruction's nesting level
from its quoted payload.

Every line keeps its provenance (page number and raw line number) so that instructions
parsed later can be reported against a location a human can find in the source PDF.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

FORM_FEED = "\f"

# A "gutter" is a wide run of spaces with text on both sides. Official Journal acts
# are typeset in two columns, and `-layout` places both columns on one physical line,
# scrambling reading order. Commission proposals are single-column (~10% of lines have
# a gutter); OJ acts run 30-50%.
GUTTER = re.compile(r"\S {6,}\S")
TWO_COLUMN_RATIO = 0.25


class ExtractionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Line:
    """One line of extracted text, with provenance back to the source PDF."""

    text: str
    page: int
    lineno: int  # 1-based, matches `pdftotext -layout file.pdf - | grep -n`

    @property
    def stripped(self) -> str:
        return self.text.strip()

    @property
    def indent(self) -> int:
        return len(self.text) - len(self.text.lstrip())

    @property
    def blank(self) -> bool:
        return not self.text.strip()

    def __repr__(self) -> str:  # keeps failing-test output readable
        return f"Line(p{self.page}:{self.lineno} {self.text[:60]!r})"


def pdftotext_available() -> bool:
    return shutil.which("pdftotext") is not None


def _run_pdftotext(pdf_path: Path, layout: bool) -> str:
    command = ["pdftotext"] + (["-layout"] if layout else []) + [str(pdf_path), "-"]
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise ExtractionError(f"pdftotext failed: {proc.stderr.strip()}")
    return proc.stdout


def gutter_ratio(text: str) -> float:
    """Fraction of non-blank lines that look like two columns side by side."""
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return 0.0
    return sum(1 for line in lines if GUTTER.search(line)) / len(lines)


def extract_text(pdf_path: str | Path) -> str:
    """Text of the PDF, in reading order.

    ``-layout`` preserves the column alignment that single-column drafting relies on,
    but interleaves the columns of a two-column Official Journal act — putting body
    text and the signature block on the same line. Two-column documents are therefore
    re-extracted in reading-order mode.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise ExtractionError(f"no such PDF: {pdf_path}")
    if not pdftotext_available():
        raise ExtractionError(
            "pdftotext not found on PATH (install poppler-utils)"
        )
    text = _run_pdftotext(pdf_path, layout=True)
    if gutter_ratio(text) > TWO_COLUMN_RATIO:
        return _run_pdftotext(pdf_path, layout=False)
    return text


def to_lines(text: str) -> list[Line]:
    """Split raw output into `Line`s, tracking page breaks.

    Note: `str.splitlines()` also splits on form feed, which would desynchronise line
    numbers from tools like `grep -n`. We split on "\\n" only and handle "\\f" ourselves.
    """
    lines: list[Line] = []
    page = 1
    for lineno, raw in enumerate(text.split("\n"), start=1):
        if FORM_FEED in raw:
            # In this corpus the form feed always leads the line, so the remaining
            # content belongs to the new page.
            page += raw.count(FORM_FEED)
            raw = raw.replace(FORM_FEED, "")
        lines.append(Line(raw, page, lineno))
    return lines


def extract_lines(pdf_path: str | Path) -> list[Line]:
    return to_lines(extract_text(pdf_path))
