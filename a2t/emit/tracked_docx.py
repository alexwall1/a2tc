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
from docx.opc.constants import CONTENT_TYPE as CT
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import XmlPart
from docx.oxml import OxmlElement
from docx.oxml.ns import nsmap, qn
from docx.oxml.parser import parse_xml
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
    # The amending act's own preamble, as a Commission proposal sets it: the procedure
    # number against the right margin, the title block centred over it, the citations
    # and the enacting formula ranged left, and the recitals with their numbers hanging
    # in the margin beside the text.
    "OJ Procedure":     (10, False, False, ALIGN.RIGHT,    0, 18, 0.0),
    "OJ Proposal Lead": (10, False, False, ALIGN.CENTER,   0,  6, 0.0),
    "OJ Proposal Title":(12, True,  False, ALIGN.CENTER,   0,  6, 0.0),
    "OJ Proposal Subtitle": (11, True, False, ALIGN.CENTER, 0, 12, 0.0),
    "OJ Proposal Note": (10, False, False, ALIGN.CENTER,    0, 18, 0.0),
    "OJ Formula":       (10, False, False, ALIGN.LEFT,     0, 10, 0.0),
    "OJ Recital":       (10, False, False, ALIGN.JUSTIFY,  0,  8, 1.25),
    # Word's own names for the footnote apparatus, so the notes are set the way a
    # reader's Word sets them.
    "Footnote Text":    (8,  False, False, ALIGN.JUSTIFY,  0,  2, 0.0),
}

# Styles whose first line hangs back into the left indent, in cm. A recital's number
# stands in the margin beside its text rather than running into it, which is what makes
# a page of them readable at a glance.
OJ_HANGING = {"OJ Recital": 1.25}


# A footnotes part has to exist before a reference can point into it, and Word wants the
# two separator entries even when the document defines no footnotes of its own: they are
# the rule drawn above the notes and its continuation on an overflowing page.
FOOTNOTES_XML = (
    '<w:footnotes xmlns:w="%s">'
    '<w:footnote w:type="separator" w:id="-1"><w:p><w:pPr>'
    '<w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
    '<w:r><w:separator/></w:r></w:p></w:footnote>'
    '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:pPr>'
    '<w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
    '<w:r><w:continuationSeparator/></w:r></w:p></w:footnote>'
    "</w:footnotes>"
) % nsmap["w"]

FOOTNOTES_PARTNAME = "/word/footnotes.xml"


@dataclass(frozen=True)
class Ins:
    """One run of an inserted paragraph.

    `note` turns the run into a reference to a footnote carrying that text, which Word
    numbers itself and prints at the foot of whichever page the reference lands on.
    """

    text: str = ""
    superscript: bool = False
    note: str | None = None


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
        self._footnotes = None
        self._footnote_id = 0
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
            hanging = OJ_HANGING.get(name)
            if hanging:
                style.paragraph_format.first_line_indent = Cm(-hanging)
                # Without an explicit stop the tab after the number goes to Word's
                # default grid instead of the indent the text is set at.
                style.paragraph_format.tab_stops.add_tab_stop(Cm(hanging))
            # Headings belong with what they head.
            style.paragraph_format.keep_with_next = align is ALIGN.CENTER

        # The reference itself is a character style, because it marks a few digits
        # inside a paragraph rather than the paragraph.
        reference = self.doc.styles.add_style(
            "Footnote Reference", WD_STYLE_TYPE.CHARACTER
        )
        reference.font.superscript = True

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

    # ---------------------------------------------------------------- footnotes

    def _footnotes_element(self):
        """The document's footnotes part, created on first use.

        `python-docx` has no footnote API — as it has none for revisions — so the part
        is built and related here. Everything the format needs is already in the
        library: the content type, the relationship type, and an XML part that
        serialises itself into the package like any other.
        """
        if self._footnotes is not None:
            return self._footnotes
        document = self.doc.part
        try:
            part = document.part_related_by(RT.FOOTNOTES)
        except KeyError:
            part = XmlPart(
                PackURI(FOOTNOTES_PARTNAME), CT.WML_FOOTNOTES,
                parse_xml(FOOTNOTES_XML.encode()), document.package,
            )
            document.relate_to(part, RT.FOOTNOTES)
        self._footnotes = part.element
        return self._footnotes

    def add_footnote(self, text: str, inserted: bool = False) -> int:
        """Add a footnote carrying `text`, returning the id a reference points at.

        Word numbers footnotes itself, by the order their references appear in the
        body — so a reference added later on an earlier page renumbers the rest, and
        the numbering cannot drift out of step with the text the way a hand-written
        one could.
        """
        footnotes = self._footnotes_element()
        self._footnote_id += 1

        note = OxmlElement("w:footnote")
        note.set(qn("w:id"), str(self._footnote_id))
        paragraph = OxmlElement("w:p")
        pPr = OxmlElement("w:pPr")
        style = OxmlElement("w:pStyle")
        style.set(qn("w:val"), "FootnoteText")
        pPr.append(style)
        paragraph.append(pPr)

        mark = OxmlElement("w:r")
        mark_pr = OxmlElement("w:rPr")
        mark_style = OxmlElement("w:rStyle")
        mark_style.set(qn("w:val"), "FootnoteReference")
        mark_pr.append(mark_style)
        mark.append(mark_pr)
        mark.append(OxmlElement("w:footnoteRef"))

        body = OxmlElement("w:r")
        t = OxmlElement("w:t")
        t.text = f"\t{text}"
        t.set(XML_SPACE, "preserve")
        body.append(t)

        if inserted:
            # The note travels with its reference: rejecting the insertion takes both.
            for run in (mark, body):
                wrapper = OxmlElement("w:ins")
                self._revision_attrs(wrapper)
                wrapper.append(run)
                paragraph.append(wrapper)
        else:
            paragraph.append(mark)
            paragraph.append(body)

        note.append(paragraph)
        footnotes.append(note)
        return self._footnote_id

    def _reference(self, paragraph, note_id: int):
        """A run referring to footnote `note_id`."""
        run = paragraph.add_run()
        rPr = run._r.get_or_add_rPr()
        style = OxmlElement("w:rStyle")
        style.set(qn("w:val"), "FootnoteReference")
        rPr.append(style)
        reference = OxmlElement("w:footnoteReference")
        reference.set(qn("w:id"), str(note_id))
        run._r.append(reference)
        return run

    # ------------------------------------------------------------------- public

    def unchanged(self, text: str, style: str | None = None) -> Block:
        p = self.doc.add_paragraph(style=style)
        return Block(p, [self._run(p, text)], "unchanged")

    def inserted(self, text: str, style: str | None = None) -> Block:
        """A wholly new paragraph."""
        return self.inserted_pieces([Ins(text)], style)

    def inserted_pieces(self, pieces, style: str | None = None) -> Block:
        """A wholly new paragraph built from `Ins` runs.

        Superscript is set on the run rather than in the style, because it applies to
        part of a paragraph and there is nowhere else to put it. That is safe here in a
        way it would not be for a deletion: accepting an insertion keeps the run and the
        formatting on it, and rejecting one takes both away together.
        """
        p = self.doc.add_paragraph(style=style)
        runs = []
        for piece in pieces:
            if piece.note is not None:
                run = self._reference(p, self.add_footnote(piece.note, inserted=True))
            elif piece.text:
                run = self._run(p, piece.text)
                if piece.superscript:
                    run.font.superscript = True
            else:
                continue
            runs.append(self._revise(run, "ins"))
        self._mark_paragraph(p, "ins")
        return Block(p, runs, "inserted")

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
