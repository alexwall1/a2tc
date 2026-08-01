"""Word tracked-changes writer.

`python-docx` has no tracked-changes API, so revision markup is written into the
OOXML directly:

    inserted text -> <w:ins><w:r><w:t>...</w:t></w:r></w:ins>
    deleted text  -> <w:del><w:r><w:delText>...</w:delText></w:r></w:del>

Paragraph marks carry their own revision flag in ``w:pPr/w:rPr``, which is what makes
Word merge or split paragraphs correctly when a change is accepted or rejected.

Runs are built revision-aware from the start rather than post-processing a finished
document, so a run is never in an inconsistent state.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH as ALIGN
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from a2t.textdiff import similarity, tokenize  # noqa: F401  (re-exported)

# Typography of the Official Journal, measured off the consolidated ETS Directive
# (`02003L0087 — EN — 01.03.2024`) rather than guessed: every font in the file is
# Times New Roman, body text is 10pt justified, and the headings are centred on the
# text column — article numbers in *italic*, article titles in **bold**, chapter
# headings a size smaller than the body at 8pt.
#
# What is deliberately not reproduced is the consolidation furniture: the ►M15◄
# change markers and the left-hand column they sit in exist to show which amending
# act introduced which words, and this document says that with tracked changes.
FONT = "Times New Roman"
BODY_PT = 10

# size, bold, italic, alignment, space before/after in points, left indent in cm
OJ_STYLES = {
    "OJ Title":         (12, True,  False, ALIGN.CENTER,   0, 12, 0.0),
    "OJ Note":          (9,  False, True,  ALIGN.CENTER,   0,  6, 0.0),
    "OJ Warning":       (9,  True,  False, ALIGN.LEFT,     0,  6, 0.0),
    "OJ Chapter":       (8,  False, False, ALIGN.CENTER,  18,  6, 0.0),
    "OJ Chapter Title": (8,  True,  False, ALIGN.CENTER,   0, 12, 0.0),
    "OJ Article":       (10, False, True,  ALIGN.CENTER,  18,  6, 0.0),
    "OJ Article Title": (10, True,  False, ALIGN.CENTER,   0, 12, 0.0),
    "OJ Text":          (10, False, False, ALIGN.JUSTIFY,  0,  6, 0.0),
    "OJ Point":         (10, False, False, ALIGN.JUSTIFY,  0,  6, 1.0),
    "OJ Subpoint":      (10, False, False, ALIGN.JUSTIFY,  0,  6, 2.0),
    "OJ Heading":       (10, True,  False, ALIGN.LEFT,    18,  6, 0.0),
}


@dataclass
class Block:
    """A written paragraph and its runs.

    Runs are returned explicitly because ``Paragraph.runs`` only sees direct ``w:r``
    children — once a run is wrapped in ``w:ins``/``w:del`` it disappears from that
    view, and comment anchoring needs the ``Run`` objects.
    """

    paragraph: Any
    runs: list = field(default_factory=list)
    mode: str | None = None

XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

# CT_PPr child order (ECMA-376). w:rPr sits near the end; these three may follow it.
_PPR_TAIL = ("w:sectPr", "w:pPrChange")


class TrackedDocx:
    """Builds a .docx whose revisions Word can accept or reject individually."""

    def __init__(
        self,
        author: str = "A2T",
        date: _dt.datetime | None = None,
        block_threshold: float = 0.4,
    ) -> None:
        self.doc = Document()
        self.author = author
        self.block_threshold = block_threshold
        stamp = date or _dt.datetime.now(_dt.timezone.utc)
        self.date = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._id = 0
        self._install_styles()

    # ---------------------------------------------------------------- internals

    def _install_styles(self) -> None:
        """Set the page up as A4 and define the Official Journal's paragraph styles.

        Styling lives in styles rather than on runs so that Word keeps it when a
        tracked change is accepted or rejected: a revision replaces runs, and any
        formatting held on the run it removed would go with it.
        """
        section = self.doc.sections[0]
        section.page_width, section.page_height = Cm(21), Cm(29.7)
        for side in ("left", "right", "top", "bottom"):
            setattr(section, f"{side}_margin", Cm(2.5))

        normal = self.doc.styles["Normal"]
        normal.font.name = FONT
        normal.font.size = Pt(BODY_PT)
        normal.paragraph_format.alignment = ALIGN.JUSTIFY
        normal.paragraph_format.space_after = Pt(6)
        # python-docx writes only w:ascii/w:hAnsi; without w:cs the complex-script
        # face stays on the template's default and shows through on some glyphs.
        normal.element.rPr.rFonts.set(qn("w:cs"), FONT)

        for name, (size, bold, italic, align, before, after, indent) in \
                OJ_STYLES.items():
            style = self.doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
            style.base_style = normal
            style.font.size = Pt(size)
            style.font.bold = bold
            style.font.italic = italic
            style.paragraph_format.alignment = align
            style.paragraph_format.space_before = Pt(before)
            style.paragraph_format.space_after = Pt(after)
            if indent:
                style.paragraph_format.left_indent = Cm(indent)
            # Headings belong with what they head.
            style.paragraph_format.keep_with_next = align is ALIGN.CENTER

    def _next_id(self) -> str:
        self._id += 1
        return str(self._id)

    def _revision_attrs(self, el) -> None:
        el.set(qn("w:id"), self._next_id())
        el.set(qn("w:author"), self.author)
        el.set(qn("w:date"), self.date)

    def _run(self, paragraph, text: str):
        """A plain run with whitespace preserved."""
        run = paragraph.add_run(text)
        for t in run._r.findall(qn("w:t")):
            t.set(XML_SPACE, "preserve")
        return run

    def _revise(self, run, kind: str):
        """Wrap an existing run in <w:ins> or <w:del>, in place."""
        r = run._r
        wrapper = OxmlElement(f"w:{kind}")
        self._revision_attrs(wrapper)
        r.getparent().replace(r, wrapper)
        wrapper.append(r)
        if kind == "del":
            # Deleted text must not be <w:t>, or Word shows it as still present.
            for t in r.findall(qn("w:t")):
                t.tag = qn("w:delText")
        return run

    def _mark_paragraph(self, paragraph, kind: str) -> None:
        """Flag the paragraph mark itself as inserted or deleted."""
        pPr = paragraph._p.get_or_add_pPr()
        rPr = pPr.find(qn("w:rPr"))
        if rPr is None:
            rPr = OxmlElement("w:rPr")
            anchor = next(
                (pPr.find(qn(t)) for t in _PPR_TAIL if pPr.find(qn(t)) is not None),
                None,
            )
            if anchor is None:
                pPr.append(rPr)
            else:
                anchor.addprevious(rPr)
        marker = OxmlElement(f"w:{kind}")
        self._revision_attrs(marker)
        rPr.insert(0, marker)

    # ------------------------------------------------------------------- public

    def unchanged(self, text: str, style: str | None = None) -> Block:
        p = self.doc.add_paragraph(style=style)
        return Block(p, [self._run(p, text)], "unchanged")

    def inserted(self, text: str, style: str | None = None) -> Block:
        """A wholly new paragraph."""
        p = self.doc.add_paragraph(style=style)
        run = self._revise(self._run(p, text), "ins")
        self._mark_paragraph(p, "ins")
        return Block(p, [run], "inserted")

    def deleted(self, text: str, style: str | None = None) -> Block:
        """A wholly removed paragraph."""
        p = self.doc.add_paragraph(style=style)
        run = self._revise(self._run(p, text), "del")
        self._mark_paragraph(p, "del")
        return Block(p, [run], "deleted")

    def diff(self, old: str, new: str, style: str | None = None) -> Block:
        """Word-level diff, falling back to whole-block replace when too dissimilar.

        ``Block.mode`` is "word" or "block" so callers can report which presentation
        was used.
        """
        p = self.doc.add_paragraph(style=style)
        a, b = tokenize(old), tokenize(new)
        matcher = SequenceMatcher(None, a, b, autojunk=False)

        if matcher.ratio() < self.block_threshold:
            runs = [
                self._revise(self._run(p, old), "del"),
                self._revise(self._run(p, new), "ins"),
            ]
            return Block(p, runs, "block")

        runs = []
        for op, i1, i2, j1, j2 in matcher.get_opcodes():
            if op == "equal":
                runs.append(self._run(p, "".join(a[i1:i2])))
            elif op == "delete":
                runs.append(self._revise(self._run(p, "".join(a[i1:i2])), "del"))
            elif op == "insert":
                runs.append(self._revise(self._run(p, "".join(b[j1:j2])), "ins"))
            else:  # replace
                runs.append(self._revise(self._run(p, "".join(a[i1:i2])), "del"))
                runs.append(self._revise(self._run(p, "".join(b[j1:j2])), "ins"))
        return Block(p, runs, "word")

    def from_spans(self, spans, style: str | None = None,
                   paragraph_mark: str | None = None) -> Block:
        """Render a pre-computed edit script (Phase 7) as tracked changes.

        Diffing happens once, in `a2t.apply.diff`, so what the document shows is
        exactly what the change set says — the two can never drift apart.
        """
        p = self.doc.add_paragraph(style=style)
        runs = []
        for span in spans:
            if not span.text:
                continue
            run = self._run(p, span.text)
            if span.op == "delete":
                run = self._revise(run, "del")
            elif span.op == "insert":
                run = self._revise(run, "ins")
            runs.append(run)
        if paragraph_mark in ("ins", "del"):
            # Without this the revision covers the text but not the paragraph break,
            # so accepting a deletion leaves an empty paragraph behind.
            self._mark_paragraph(p, paragraph_mark)
        return Block(p, runs, "spans")

    def flag(self, runs, message: str, author: str | None = None):
        """Anchor a Word comment to one or more runs (used for unresolved instructions)."""
        return self.doc.add_comment(
            runs, text=message, author=author or self.author, initials="A2T"
        )

    def save(self, path: str) -> str:
        self.doc.save(path)
        return path
