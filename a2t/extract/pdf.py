"""PDF text extraction.

Uses ``pdftotext -layout``, which preserves the column alignment that EU legislative
drafting relies on: indentation is what distinguishes an instruction's nesting level
from its quoted payload.

Every line keeps its provenance (page number and raw line number) so that instructions
parsed later can be reported against a location a human can find in the source PDF.

What ``-layout`` does not give is where one paragraph ends and the next begins: it
writes a newline per line and nothing between blocks. A second pass over poppler's own
layout analysis supplies that, and only that — the text still comes from ``-layout``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path

from lxml import etree as ET

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
    # Begins a new paragraph in the source. `-layout` writes one newline per line and
    # none between paragraphs, so a break that is not marked by a number or an indent
    # is otherwise unrecoverable from the text. See `paragraph_starts`.
    starts_paragraph: bool = False

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


XHTML = "{http://www.w3.org/1999/xhtml}"


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def paragraph_starts(pdf_path: str | Path) -> list[tuple[str, bool]]:
    """Every line of the PDF, with whether it opens a paragraph.

    `-layout` writes a newline per line and nothing between paragraphs, so a break
    that carries no number and no indentation leaves no trace in it — and amendment
    payloads are full of them: a new subparagraph simply starts at the left margin
    like the wrapped line above it. Line length cannot stand in for the break either,
    because the text is justified, so a line routinely ends with room to spare.

    `-bbox-layout` runs poppler's own layout analysis and groups lines into blocks
    using the vertical gaps and font metrics that the flat text throws away. Only the
    grouping is taken from it; the text itself still comes from `-layout`, whose line
    numbering everything downstream reports against.
    """
    proc = subprocess.run(
        ["pdftotext", "-bbox-layout", str(pdf_path), "-"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        return []
    try:
        root = ET.fromstring(proc.stdout.encode())
    except Exception:  # noqa: BLE001 — a layout we cannot read is not a failure
        return []

    out: list[tuple[str, bool]] = []
    for block in root.iter(f"{XHTML}block"):
        for index, line in enumerate(block.iter(f"{XHTML}line")):
            words = " ".join(w.text or "" for w in line.iter(f"{XHTML}word"))
            out.append((_collapse(words), index == 0))
    return out


def _mark_paragraphs(lines: list[Line], blocks: list[tuple[str, bool]]) -> list[Line]:
    """Carry poppler's paragraph boundaries onto the lines we extracted.

    The two passes do not agree line for line — `-bbox-layout` splits a running
    header into its parts, and a two-column page is read in a different order — so
    they are aligned rather than zipped, and a line that finds no counterpart simply
    keeps the default. A missed break prints the text as it is printed today; an
    invented one would start a provision in the middle of a sentence.
    """
    if not blocks:
        return lines
    matcher = SequenceMatcher(
        None, [_collapse(line.text) for line in lines], [text for text, _ in blocks],
        autojunk=False,
    )
    marked = list(lines)
    for start, block_start, size in matcher.get_matching_blocks():
        for offset in range(size):
            if blocks[block_start + offset][1]:
                line = lines[start + offset]
                marked[start + offset] = replace(line, starts_paragraph=True)
    return marked


def extract_lines(pdf_path: str | Path) -> list[Line]:
    lines = to_lines(extract_text(pdf_path))
    return _mark_paragraphs(lines, paragraph_starts(pdf_path))
