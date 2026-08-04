"""Phase 8: emitting the tracked-changes document and the report."""

import re
import zipfile
from pathlib import Path as FsPath

import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt
from lxml import etree

from a2t.apply.changeset import Change, ChangeKind, ChangeSet, build_changes
from a2t.apply.diff import diff
from a2t.emit.document import DocumentEmitter, emit_document
from a2t.emit.report import ActOutcome, RunReport
from a2t.emit.tracked_docx import TrackedDocx
from a2t.model.instruction import Instruction, Op, TargetBlock
from a2t.model.payload import structure
from a2t.model.tree import Act, Kind, Node
from a2t.resolve.resolver import Resolution, ResolutionReport

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def make_act() -> Act:
    act = Act(celex="TESTACT", title="Test Directive", version_date="2024-03-01")
    chapter = act.root.add(Node(Kind.CHAPTER, key="I", title="GENERAL PROVISIONS"))
    article = chapter.add(Node(Kind.ARTICLE, key="1", title="Scope"))
    article.add(Node(Kind.PARAGRAPH, key="1", text="The original first paragraph."))
    article.add(Node(Kind.PARAGRAPH, key="2", text="The original second paragraph."))
    return act


def instruction(op=Op.REPLACE_NODE, **kwargs) -> Instruction:
    base = dict(id="1.1", marker="1", level=1, raw="…", source_line=100, op=op)
    base.update(kwargs)
    return Instruction(**base)


def change(kind, node=None, old="", new="", instr=None, **kwargs) -> Change:
    return Change(kind, instr or instruction(), node=node, old_text=old,
                  new_text=new, edit=diff(old, new), **kwargs)


def load(path) -> etree._Element:
    return etree.fromstring(zipfile.ZipFile(path).read("word/document.xml"))


def texts(doc) -> str:
    return " ".join(t.text or "" for t in doc.iter(f"{W}t"))


def deleted_texts(doc) -> str:
    return " ".join(t.text or "" for t in doc.iter(f"{W}delText"))


# The payload of COM(2026) 616 final, Article 1(30), as `pdftotext -layout` renders
# it — abridged, but with every column kept where the source has it. The geometry is
# the whole point: the heading and title are centred, paragraphs sit at the article's
# body indent, points are indented past it, and a subparagraph closing a paragraph
# returns to the body indent.
ETS_AS_A_SERVICE = [
    "                                              ‘Article 25b",
    "                                           ETS as a service",
    "     1.      Administrative and technical support and coordination may be provided for third",
    "     countries to facilitate greenhouse gas emissions pricing (‘ETS as a service’), in particular in",
    "     respect of emissions released from the following activities:",
    "             (a)   aviation as set out in Annex I in respect of the emissions from flights between",
    "                   the EEA and the third country;",
    "             (b)   maritime transport as set out in Annex I in respect of the remaining fifty",
    "                   percent (50%) of the emissions released during voyages between a port of call",
    "                   under the jurisdiction of a Member State and a port of call under the",
    "                   jurisdiction of the third country that fall outside the scope laid down in Article",
    "                   3ga.",
    "     Such support shall be provided in respect of emissions from aviation from reporting year 2027",
    "     at the earliest, and in respect of emissions from maritime transport from reporting year 2029",
    "     at the earliest.",
    "     2.      Where aviation or maritime emissions are priced pursuant to paragraph 1 of this",
    "     Article, with an absolute emissions cap, the third country shall benefit from revenues to tackle",
    "     climate change as follows:",
    "             (a)   for aviation, for fifty percent (50%) of the aviation activity between the EEA",
    "                   and the third country;",
    "     3.      The Commission is empowered to adopt delegated acts in accordance with Article 23",
    "     to supplement this Article with rules on the operation of ETS as a service.’;",
]


# ------------------------------------------------------------------- structure


def test_unchanged_act_emits_every_provision(tmp_path):
    act = make_act()
    result = emit_document(act, ChangeSet(act=act), tmp_path / "a.docx")
    body = texts(load(result.path))
    assert "The original first paragraph." in body
    assert "The original second paragraph." in body
    assert result.revisions == 0


def test_headings_are_emitted(tmp_path):
    act = make_act()
    result = emit_document(act, ChangeSet(act=act), tmp_path / "a.docx")
    body = texts(load(result.path))
    assert "CHAPTER I" in body
    assert "Article 1" in body
    assert "GENERAL PROVISIONS" in body


def test_replacement_produces_revisions(tmp_path):
    act = make_act()
    node = act.article("1").by_key(Kind.PARAGRAPH, "1")
    changeset = ChangeSet(act=act, changes=[
        change(ChangeKind.REPLACE, node, node.text, "The amended first paragraph.")
    ])
    result = emit_document(act, changeset, tmp_path / "a.docx")
    doc = load(result.path)

    assert doc.findall(f".//{W}ins")
    assert doc.findall(f".//{W}del")
    assert "amended" in texts(doc)
    assert "original" in deleted_texts(doc)


def test_deleted_provision_also_deletes_its_paragraph_mark(tmp_path):
    """Otherwise accepting the change leaves an empty paragraph behind."""
    act = make_act()
    node = act.article("1").by_key(Kind.PARAGRAPH, "2")
    changeset = ChangeSet(act=act, changes=[
        change(ChangeKind.DELETE, node, node.text, "")
    ])
    result = emit_document(act, changeset, tmp_path / "a.docx")
    doc = load(result.path)
    assert doc.findall(f".//{W}pPr/{W}rPr/{W}del")


def test_inserted_provision_marks_its_paragraph(tmp_path):
    act = make_act()
    changeset = ChangeSet(act=act, changes=[
        change(ChangeKind.INSERT, None, "", "A wholly new paragraph.",
               anchor=act.article("1"))
    ])
    result = emit_document(act, changeset, tmp_path / "a.docx")
    doc = load(result.path)
    assert doc.findall(f".//{W}pPr/{W}rPr/{W}ins")
    assert "A wholly new paragraph." in texts(doc)


def test_replaced_node_children_are_not_rendered_twice(tmp_path):
    """A provision replaced in full takes its children with it."""
    act = make_act()
    article = act.article("1")
    article.add(Node(Kind.PARAGRAPH, key="3", text="Child text."))
    changeset = ChangeSet(act=act, changes=[
        change(ChangeKind.REPLACE, article, article.full_text, "Replacement.")
    ])
    result = emit_document(act, changeset, tmp_path / "a.docx")
    body = texts(load(result.path)) + deleted_texts(load(result.path))
    assert body.count("Child text.") == 1


def test_every_change_is_placed(tmp_path):
    act = make_act()
    paragraphs = act.article("1").of_kind(Kind.PARAGRAPH)
    changeset = ChangeSet(act=act, changes=[
        change(ChangeKind.REPLACE, paragraphs[0], paragraphs[0].text, "New one."),
        change(ChangeKind.DELETE, paragraphs[1], paragraphs[1].text, ""),
    ])
    result = emit_document(act, changeset, tmp_path / "a.docx")
    assert result.unrendered == []


def test_multiple_edits_to_one_provision_compose(tmp_path):
    """Two instructions amending the same text must not render it twice."""
    act = make_act()
    node = act.article("1").by_key(Kind.PARAGRAPH, "1")
    first = change(ChangeKind.TEXT_EDIT, node, "The original first paragraph.",
                   "The revised first paragraph.", instr=instruction(id="1.a"))
    second = change(ChangeKind.TEXT_EDIT, node, "The revised first paragraph.",
                    "The revised opening paragraph.", instr=instruction(id="1.b"))
    changeset = ChangeSet(act=act, changes=[first, second])
    result = emit_document(act, changeset, tmp_path / "a.docx")

    assert result.unrendered == []
    body = texts(load(result.path))
    assert body.count("paragraph.") >= 1
    assert "opening" in body
    assert "original" not in body.replace("The original second paragraph.", "")


# --------------------------------------------------------------------- comments


def test_flagged_change_becomes_a_comment(tmp_path):
    act = make_act()
    node = act.article("1").by_key(Kind.PARAGRAPH, "1")
    instr = instruction(confidence=0.4)
    instr.note("replacement text matches a sibling far better")
    changeset = ChangeSet(act=act, changes=[
        change(ChangeKind.REPLACE, node, node.text, "New text.", instr=instr)
    ])
    result = emit_document(act, changeset, tmp_path / "a.docx")

    assert result.comments == 1
    with zipfile.ZipFile(result.path) as archive:
        assert "word/comments.xml" in archive.namelist()
        body = etree.fromstring(archive.read("word/comments.xml"))
        text = " ".join(t.text or "" for t in body.iter(f"{W}t"))
    assert "matches a sibling" in text
    assert "confidence 0.40" in text


def test_confident_change_gets_no_comment(tmp_path):
    act = make_act()
    node = act.article("1").by_key(Kind.PARAGRAPH, "1")
    changeset = ChangeSet(act=act, changes=[
        change(ChangeKind.REPLACE, node, node.text, "New text.")
    ])
    assert emit_document(act, changeset, tmp_path / "a.docx").comments == 0


def test_unapplied_instruction_is_listed_and_flagged(tmp_path):
    act = make_act()
    skipped = Change(ChangeKind.SKIPPED, instruction(), note="could not resolve")
    result = emit_document(act, ChangeSet(act=act, changes=[skipped]),
                           tmp_path / "a.docx")
    body = texts(load(result.path))
    assert "Instructions not applied" in body
    assert result.comments == 1


def test_skip_that_names_a_provision_is_flagged_there_not_at_the_end(tmp_path):
    """A provision left unamended has to say so where the reader meets it.

    Listing it at the foot of the document instead leaves the provision reading as
    though the proposal had nothing to say about it.
    """
    act = make_act()
    node = act.article("1").by_key(Kind.PARAGRAPH, "2")
    skipped = Change(ChangeKind.SKIPPED, instruction(), node=node,
                     note="replaced by the payload applied at Article 1 / paragraph 1")
    result = emit_document(act, ChangeSet(act=act, changes=[skipped]),
                           tmp_path / "a.docx")

    assert result.comments == 1
    assert "Instructions not applied" not in texts(load(result.path))
    comments = etree.fromstring(
        zipfile.ZipFile(result.path).read("word/comments.xml")
    )
    assert "payload applied at Article 1 / paragraph 1" in " ".join(
        t.text or "" for t in comments.iter(f"{W}t")
    )


def test_container_notes_do_not_clutter_the_document(tmp_path):
    act = make_act()
    skipped = Change(ChangeKind.SKIPPED, instruction(op=Op.CONTAINER),
                     note="container — edits are on its children")
    result = emit_document(act, ChangeSet(act=act, changes=[skipped]),
                           tmp_path / "a.docx")
    assert "Instructions not applied" not in texts(load(result.path))


def test_author_is_recorded_on_revisions(tmp_path):
    act = make_act()
    node = act.article("1").by_key(Kind.PARAGRAPH, "1")
    changeset = ChangeSet(act=act, changes=[
        change(ChangeKind.REPLACE, node, node.text, "New.")
    ])
    result = emit_document(act, changeset, tmp_path / "a.docx", author="Reviewer")
    doc = load(result.path)
    assert all(e.get(f"{W}author") == "Reviewer" for e in doc.iter(f"{W}ins"))


# -------------------------------------------------- Official Journal typography
#
# The reference measurements come from the consolidated ETS Directive as EUR-Lex
# publishes it (`02003L0087 — EN — 01.03.2024`): Times New Roman throughout, 10pt
# justified body, chapter headings 8pt, article numbers italic and titles bold, all
# headings centred.


def test_body_is_times_new_roman_at_ten_point(tmp_path):
    act = make_act()
    result = emit_document(act, ChangeSet(act=act), tmp_path / "a.docx")
    normal = Document(str(result.path)).styles["Normal"]
    assert normal.font.name == "Times New Roman"
    assert normal.font.size == Pt(10)
    assert normal.paragraph_format.alignment == WD_ALIGN_PARAGRAPH.JUSTIFY


def test_articles_and_their_titles_are_centred(tmp_path):
    act = make_act()
    result = emit_document(act, ChangeSet(act=act), tmp_path / "a.docx")
    styles = Document(str(result.path)).styles
    for name in ("OJ Chapter", "OJ Chapter Title", "OJ Article", "OJ Article Title"):
        assert styles[name].paragraph_format.alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert styles["OJ Article"].font.italic       # "Article 3h" is italic in the OJ
    assert styles["OJ Article Title"].font.bold   # its title is bold
    assert styles["OJ Chapter"].font.size == Pt(8)


def test_every_element_carries_an_oj_style(tmp_path):
    """No paragraph may fall back on Word's built-in Heading styles — they bring
    their own Calibri Light and would override the OJ face."""
    act = make_act()
    node = act.article("1").by_key(Kind.PARAGRAPH, "1")
    changeset = ChangeSet(act=act, changes=[
        change(ChangeKind.REPLACE, node, node.text, "New text.")
    ])
    result = emit_document(act, changeset, tmp_path / "a.docx")
    used = {p.style.name for p in Document(str(result.path)).paragraphs}
    assert {"OJ Chapter", "OJ Chapter Title", "OJ Article", "OJ Article Title",
            "OJ Text"} <= used
    assert not any(name.startswith("Heading") for name in used)


def _styled(path):
    """(style, text) per paragraph.

    Text is read off the ``w:t``/``w:delText`` nodes rather than with lxml's
    ``itertext()``: python-docx's element classes expose their own ``.text``
    properties, which ``itertext()`` walks in addition to the real text nodes and so
    returns every string several times over.
    """
    return [
        (p.style.name,
         "".join(t.text or "" for t in p._p.iter(f"{W}t", f"{W}delText")).strip())
        for p in Document(str(path)).paragraphs
    ]


def test_inserted_article_heading_gets_its_own_centred_paragraph(tmp_path):
    """An inserted article arrives as one flat payload; left whole, its heading
    would sit in the body text instead of joining the run of centred headings."""
    act = make_act()
    instr = instruction(op=Op.INSERT_NODE,
                        raw="the following Article 1a is inserted:")
    inserted = change(ChangeKind.INSERT, act.root, instr=instr,
                      new="Article 1a Member States shall report annually.")
    result = emit_document(act, ChangeSet(act=act, changes=[inserted]),
                           tmp_path / "a.docx")
    paragraphs = _styled(result.path)
    assert ("OJ Article", "Article 1a") in paragraphs
    assert ("OJ Text", "Member States shall report annually.") in paragraphs


def test_inserted_article_is_printed_as_an_article(tmp_path):
    """The reported defect: an inserted article arrived as one justified blob —
    its title glued to the front of paragraph 1, its paragraphs and points run
    together. Shape and indentation come from COM(2026) 616 final, Article 1(30)."""
    act = make_act()
    instr = instruction(op=Op.INSERT_NODE,
                        raw="the following Article 25b is inserted:",
                        payload_layout=ETS_AS_A_SERVICE)
    inserted = change(ChangeKind.INSERT, act.root, instr=instr,
                      new=" ".join(line.strip() for line in ETS_AS_A_SERVICE))
    result = emit_document(act, ChangeSet(act=act, changes=[inserted]),
                           tmp_path / "a.docx")
    paragraphs = _styled(result.path)

    assert ("OJ Article", "Article 25b") in paragraphs
    assert ("OJ Article Title", "ETS as a service") in paragraphs
    assert any(style == "OJ Text" and text.startswith("1. Administrative")
               for style, text in paragraphs)
    assert any(style == "OJ Point" and text.startswith("(a) aviation")
               for style, text in paragraphs)
    # An unnumbered subparagraph closing a paragraph, after its points.
    assert any(style == "OJ Text" and text.startswith("Such support")
               for style, text in paragraphs)


def test_inserted_article_needs_no_number_in_the_instruction(tmp_path):
    """Most insertions do not name what they insert — "the following Article is
    inserted:" — so the number has to come out of the payload itself."""
    act = make_act()
    instr = instruction(op=Op.INSERT_NODE, raw="the following Article is inserted:",
                        payload_layout=ETS_AS_A_SERVICE)
    inserted = change(ChangeKind.INSERT, act.root, instr=instr,
                      new=" ".join(line.strip() for line in ETS_AS_A_SERVICE))
    result = emit_document(act, ChangeSet(act=act, changes=[inserted]),
                           tmp_path / "a.docx")
    assert ("OJ Article", "Article 25b") in _styled(result.path)


def test_every_block_of_an_inserted_article_is_a_revision(tmp_path):
    """Splitting the payload must not leave any of it outside a <w:ins>, or
    rejecting the change would keep part of the new article."""
    act = make_act()
    instr = instruction(op=Op.INSERT_NODE,
                        raw="the following Article 25b is inserted:",
                        payload_layout=ETS_AS_A_SERVICE)
    inserted = change(ChangeKind.INSERT, act.root, instr=instr,
                      new=" ".join(line.strip() for line in ETS_AS_A_SERVICE))
    result = emit_document(act, ChangeSet(act=act, changes=[inserted]),
                           tmp_path / "a.docx")

    doc = Document(str(result.path))
    written = [p for p in doc.paragraphs if p.style.name.startswith("OJ")]
    new_blocks = [p for p in written
                  if "Administrative" in p._p.xml or "Article 25b" in p._p.xml
                  or "aviation" in p._p.xml]
    assert new_blocks
    for paragraph in new_blocks:
        assert paragraph._p.findall(f".//{W}ins")
        assert paragraph._p.findall(f"{W}pPr/{W}rPr/{W}ins")


def test_appended_points_are_indented_as_points(tmp_path):
    """Points added to an article print at the points' indent, not as body text."""
    act = make_act()
    article = act.article("1")
    instr = instruction(op=Op.APPEND_NODE, raw="the following points are added:",
                        payload_layout=[
                            "       ‘(aj) ‘hazardous waste’ means hazardous waste;",
                            "       (ak) ‘municipal waste’ means municipal waste;’;",
                        ])
    inserted = change(ChangeKind.INSERT, None, anchor=article, instr=instr,
                      new="(aj) ‘hazardous waste’ means hazardous waste; "
                          "(ak) ‘municipal waste’ means municipal waste;")
    result = emit_document(act, ChangeSet(act=act, changes=[inserted]),
                           tmp_path / "a.docx")
    paragraphs = _styled(result.path)

    assert ("OJ Point", "(aj) ‘hazardous waste’ means hazardous waste;") in paragraphs
    assert ("OJ Point", "(ak) ‘municipal waste’ means municipal waste;") in paragraphs


def test_replaced_point_carries_its_sub_points_at_their_own_indent(tmp_path):
    act = make_act()
    article = act.article("1")
    point = article.add(Node(Kind.POINT, key="e", text="‘installation’ means a unit."))
    instr = instruction(op=Op.REPLACE_NODE,
                        payload="(e) ‘installation’ means either of the following: "
                                "(i) a stationary technical unit; "
                                "(ii) a CO2 transport infrastructure;",
                        payload_layout=[
                            "     ‘(e) ‘installation’ means either of the following:",
                            "     (i) a stationary technical unit;",
                            "     (ii) a CO2 transport infrastructure;’;",
                        ])
    changeset = ChangeSet(act=act, changes=build_changes(
        Resolution(instr, node=point, status="resolved")
    ))
    result = emit_document(act, changeset, tmp_path / "a.docx")
    styles = {style for style, _ in _styled(result.path)}
    assert "OJ Subpoint" in styles


def test_inserted_text_without_an_article_keeps_its_anchors_level(tmp_path):
    """A point added to a list of points is still a point, not body text."""
    act = make_act()
    article = act.article("1")
    point = article.by_key(Kind.PARAGRAPH, "1").add(
        Node(Kind.POINT, key="a", text="an existing point")
    )
    instr = instruction(op=Op.INSERT_NODE, raw="the following point (b) is inserted:")
    inserted = change(ChangeKind.INSERT, None, instr=instr, anchor=point,
                      new="(b) a wholly new point")
    result = emit_document(act, ChangeSet(act=act, changes=[inserted]),
                           tmp_path / "a.docx")
    assert ("OJ Point", "(b) a wholly new point") in _styled(result.path)


def test_payload_naming_a_different_article_is_not_split(tmp_path):
    """"Article 5 shall apply…" opening an inserted paragraph is a cross-reference,
    not a heading. The split is keyed on the article the instruction names."""
    act = make_act()
    instr = instruction(op=Op.INSERT_NODE,
                        raw="the following Article 1a is inserted:")
    inserted = change(ChangeKind.INSERT, act.root, instr=instr,
                      new="Article 5 shall apply mutatis mutandis.")
    result = emit_document(act, ChangeSet(act=act, changes=[inserted]),
                           tmp_path / "a.docx")
    paragraphs = _styled(result.path)
    assert ("OJ Text", "Article 5 shall apply mutatis mutandis.") in paragraphs
    assert not any(style == "OJ Article" and text.startswith("Article 5")
                   for style, text in paragraphs)


def test_front_matter_names_the_version(tmp_path):
    act = make_act()
    result = emit_document(act, ChangeSet(act=act), tmp_path / "a.docx")
    assert "TESTACT" in texts(load(result.path))
    assert "2024-03-01" in texts(load(result.path))


def test_version_warning_appears_in_the_document(tmp_path):
    """A doubt about which base text this is has to be visible to whoever opens the
    document, not only to whoever reads the report."""
    act = make_act()
    result = emit_document(act, ChangeSet(act=act), tmp_path / "a.docx",
                           version_warning="applies from 2024-03-01 but was not "
                                           "published until 2024-04-02")
    body = texts(load(result.path))
    assert "Version warning" in body
    assert "not published until 2024-04-02" in body


def test_each_version_warning_gets_its_own_paragraph(tmp_path):
    act = make_act()
    plain = emit_document(act, ChangeSet(act=act), tmp_path / "plain.docx")
    both = emit_document(act, ChangeSet(act=act), tmp_path / "both.docx",
                         version_warning="not yet published\nno English text")
    assert both.paragraphs == plain.paragraphs + 2


def test_no_version_warning_adds_nothing(tmp_path):
    act = make_act()
    result = emit_document(act, ChangeSet(act=act), tmp_path / "a.docx",
                           version_warning=None)
    assert "Version warning" not in texts(load(result.path))


# ------------------------------------------------------- numbered paragraphs
#
# EUR-Lex keeps a paragraph's number outside its words, so a numbered paragraph is a
# node with a key and no text of its own. Whatever carries its first words has to
# print the number, or the act loses its numbering entirely.


def numbered_article() -> tuple[Act, Node]:
    act = Act(celex="TESTACT", title="Test Directive")
    article = act.root.add(Node(Kind.ARTICLE, key="10", title="Auctioning"))
    first = article.add(Node(Kind.PARAGRAPH, key="1"))
    first.add(Node(Kind.SUBPARAGRAPH, text="From 2019 onwards, Member States shall auction."))
    first.add(Node(Kind.SUBPARAGRAPH, text="From 2021 onwards, without prejudice."))
    second = article.add(Node(Kind.PARAGRAPH, key="1a"))
    second.add(Node(Kind.SUBPARAGRAPH, text="Where the volume of allowances is lower."))
    return act, article


def test_a_numbered_paragraph_prints_its_number(tmp_path):
    act, _ = numbered_article()
    result = emit_document(act, ChangeSet(act=act), tmp_path / "a.docx")
    assert [t for s, t in _styled(result.path) if s == "OJ Text"] == [
        "1. From 2019 onwards, Member States shall auction.",
        "From 2021 onwards, without prejudice.",   # a later subparagraph, unnumbered
        "1a. Where the volume of allowances is lower.",
    ]


def test_a_replacement_restating_the_number_does_not_print_it_twice(tmp_path):
    """Payloads are drafted restating the number — "1. …". Printed against a number
    the act keeps outside its text, it would come out as "1. 1. …"."""
    act, article = numbered_article()
    paragraph = article.by_key(Kind.PARAGRAPH, "1")
    instr = instruction(op=Op.REPLACE_NODE,
                        payload="1. From 2019 onwards, Member States shall auction.")
    changeset = ChangeSet(act=act, changes=build_changes(
        Resolution(instr, node=paragraph, status="resolved")
    ))
    result = emit_document(act, changeset, tmp_path / "a.docx")

    doc = load(result.path)
    assert "1. 1." not in texts(doc)
    # The number is unchanged, so it is not struck and re-inserted either.
    assert "1." not in deleted_texts(doc)


def test_a_deleted_paragraph_takes_its_number_with_it(tmp_path):
    """Or accepting the deletion leaves a bare "1." standing over nothing."""
    act, article = numbered_article()
    paragraph = article.by_key(Kind.PARAGRAPH, "1")
    changeset = ChangeSet(act=act, changes=build_changes(
        Resolution(instruction(op=Op.DELETE_NODE), node=paragraph, status="resolved")
    ))
    result = emit_document(act, changeset, tmp_path / "a.docx")

    doc = load(result.path)
    assert "1. From 2019 onwards" in deleted_texts(doc)
    assert "1. From 2019 onwards" not in texts(doc)


def test_a_phrase_edit_leaves_the_number_alone(tmp_path):
    """An edit inside the words never touches the number in front of them."""
    act, article = numbered_article()
    paragraph = article.by_key(Kind.PARAGRAPH, "1")
    instr = instruction(op=Op.REPLACE_TEXT, find_text="2019", replace_text="2026")
    changeset = ChangeSet(act=act, changes=build_changes(
        Resolution(instr, node=paragraph, status="resolved")
    ))
    result = emit_document(act, changeset, tmp_path / "a.docx")

    doc = load(result.path)
    assert "2026" in texts(doc)
    assert "2019" in deleted_texts(doc)
    assert "1." not in deleted_texts(doc)
    assert any(s == "OJ Text" and t.startswith("1. From")
               for s, t in _styled(result.path))


# ------------------------------------------------------------------- placement


def numbered_act() -> Act:
    act = Act(celex="TESTACT", title="Test Directive")
    for key in ("28", "29", "29a", "30"):
        act.root.add(Node(Kind.ARTICLE, key=key, title=f"Heading {key}"))
    return act


def article_sequence(path) -> list[str]:
    return [
        text.removeprefix("Article ")
        for style, text in _styled(path) if style == "OJ Article"
    ]


def test_a_new_article_is_printed_where_its_number_belongs(tmp_path):
    """An unplaced insertion used to pile up at the end of the document, so a new
    Article 29b printed after Article 30 and everything else inserted before it."""
    act = numbered_act()
    instr = instruction(op=Op.INSERT_NODE, raw="the following Article 29b is inserted:")
    inserted = change(ChangeKind.INSERT, None, instr=instr,
                      anchor=act.article("29a"),
                      new="Article 29b Member States shall report annually.")
    result = emit_document(act, ChangeSet(act=act, changes=[inserted]),
                           tmp_path / "a.docx")
    assert article_sequence(result.path) == ["28", "29", "29a", "29b", "30"]


def test_two_new_articles_sharing_a_place_keep_their_order(tmp_path):
    """3gaa and 3gab are both inserted after 3ga — the second by an instruction
    naming the first, which is not in the act yet. They print in the order given."""
    act = numbered_act()
    changes = [
        change(ChangeKind.INSERT, None, anchor=act.article("29a"),
               instr=instruction(id=f"1.{n}", op=Op.INSERT_NODE,
                                 raw=f"the following Article {key} is inserted:"),
               new=f"Article {key} Some new obligation.")
        for n, key in enumerate(("29b", "29c"))
    ]
    result = emit_document(act, ChangeSet(act=act, changes=changes),
                           tmp_path / "a.docx")
    assert article_sequence(result.path) == ["28", "29", "29a", "29b", "29c", "30"]


def test_an_insertion_lands_after_the_provision_it_is_anchored_to(tmp_path):
    """"the following paragraphs are inserted after paragraph 1" must not land past
    paragraph 2, which is where the end of the article is."""
    act = Act(celex="TESTACT", title="Test Directive")
    article = act.root.add(Node(Kind.ARTICLE, key="1"))
    first = article.add(Node(Kind.PARAGRAPH, key="1", text="The first paragraph."))
    article.add(Node(Kind.PARAGRAPH, key="2", text="The second paragraph."))

    instr = instruction(op=Op.INSERT_NODE,
                        raw="the following paragraph is inserted after paragraph 1:")
    inserted = change(ChangeKind.INSERT, None, anchor=first, instr=instr,
                      new="1a. A paragraph between the two.")
    result = emit_document(act, ChangeSet(act=act, changes=[inserted]),
                           tmp_path / "a.docx")
    body = [text for style, text in _styled(result.path) if style == "OJ Text"]
    assert body == ["1. The first paragraph.",
                    "1a. A paragraph between the two.",
                    "2. The second paragraph."]


# ----------------------------------------------------- replacements, block by block


def replaced(act, node, payload, layout=None) -> ChangeSet:
    instr = instruction(op=Op.REPLACE_NODE, payload=payload,
                        payload_layout=layout or [])
    return ChangeSet(act=act, changes=build_changes(
        Resolution(instr, node=node, status="resolved")
    ))


def structured_article() -> tuple[Act, Node]:
    act = Act(celex="TESTACT", title="Test Directive")
    article = act.root.add(Node(Kind.ARTICLE, key="7", title="Reporting"))
    first = article.add(Node(Kind.PARAGRAPH, key="1",
                             text="Reports are due by 31 December 2030."))
    first.add(Node(Kind.POINT, key="a", text="emissions from aviation;"))
    first.add(Node(Kind.POINT, key="b", text="emissions from maritime transport;"))
    article.add(Node(Kind.PARAGRAPH, key="2",
                     text="The Commission shall publish the reports."))
    return act, article


def test_replaced_provision_is_written_block_by_block(tmp_path):
    act, article = structured_article()
    payload = ("1. Reports are due by 31 December 2035. (a) emissions from aviation; "
               "(b) emissions from maritime transport; "
               "2. The Commission shall publish the reports.")
    result = emit_document(act, replaced(act, article, payload), tmp_path / "a.docx")
    paragraphs = _styled(result.path)

    # One paragraph per provision, not one paragraph for the lot.
    assert any(style == "OJ Text" and text.startswith("1. Reports are due")
               for style, text in paragraphs)
    assert ("OJ Point", "(a) emissions from aviation;") in paragraphs
    assert ("OJ Text", "2. The Commission shall publish the reports.") in paragraphs


def test_a_block_that_survives_keeps_its_word_level_marks(tmp_path):
    """The point of aligning: a paragraph whose only change is a date must still
    read as one struck date and one inserted date."""
    act, article = structured_article()
    payload = ("1. Reports are due by 31 December 2035. (a) emissions from aviation; "
               "(b) emissions from maritime transport; "
               "2. The Commission shall publish the reports.")
    result = emit_document(act, replaced(act, article, payload), tmp_path / "a.docx")

    doc = load(result.path)
    assert "2035" in texts(doc)
    assert "2030" in deleted_texts(doc)
    # The unchanged paragraph carries no revision at all.
    survivor = next(p for p in Document(str(result.path)).paragraphs
                    if "publish the reports" in p._p.xml)
    assert not survivor._p.findall(f".//{W}ins")
    assert not survivor._p.findall(f".//{W}del")


def test_a_block_present_on_one_side_only_takes_its_paragraph_mark(tmp_path):
    """Otherwise accepting the change leaves the paragraph break of a provision
    that is no longer there."""
    act, article = structured_article()
    payload = ("1. Reports are due by 31 December 2030. (a) emissions from aviation; "
               "(b) emissions from maritime transport; "
               "2. The Commission shall publish the reports. "
               "3. Member States shall verify them.")
    result = emit_document(act, replaced(act, article, payload), tmp_path / "a.docx")

    added = next(p for p in Document(str(result.path)).paragraphs
                 if "verify them" in p._p.xml)
    assert added._p.findall(f"{W}pPr/{W}rPr/{W}ins")


def test_a_replaced_article_prints_its_heading_once(tmp_path):
    """The payload restates the heading, and the act already carries one. Printing
    both would put "Article 7" on the page twice."""
    act, article = structured_article()
    payload = ("Article 7 Reporting and verification "
               "1. Reports are due by 31 December 2035. "
               "2. The Commission shall publish the reports.")
    layout = [
        "                              ‘Article 7",
        "                        Reporting and verification",
        "     1.     Reports are due by 31 December 2035.",
        "     2.     The Commission shall publish the reports.’;",
    ]
    result = emit_document(act, replaced(act, article, payload, layout),
                           tmp_path / "a.docx")
    paragraphs = _styled(result.path)

    assert [text for style, text in paragraphs if style == "OJ Article"] == ["Article 7"]
    # The title changed, and the change is tracked on its own line.
    doc = load(result.path)
    assert "Reporting and verification" in texts(doc)
    assert "Reporting" in deleted_texts(doc)


def test_block_rendering_never_drops_a_provision(tmp_path):
    """Every word of both sides has to reach the page — struck, inserted or plain."""
    act, article = structured_article()
    payload = ("1. Reports are due by 31 December 2035. (a) emissions from aviation; "
               "2. Member States shall verify them.")
    changeset = replaced(act, article, payload)
    result = emit_document(act, changeset, tmp_path / "a.docx")

    doc = load(result.path)
    on_page = re.findall(r"\w+", texts(doc) + " " + deleted_texts(doc))
    for source in (article.full_text, payload):
        for word in re.findall(r"\w+", source):
            assert word in on_page


# ------------------------------------------------------------ payload structure
#
# `payload` is normalised for comparison and so cannot be printed with its structure.
# It is recovered from the payload's own source lines, and — where those are not
# available — from the markers in the flat text.


def kinds(provisions) -> list[tuple[str, str | None]]:
    return [(p.kind.value, p.key) for p in provisions]


def test_layout_recovers_an_articles_own_shape():
    provisions = structure(" ".join(l.strip() for l in ETS_AS_A_SERVICE),
                           layout=ETS_AS_A_SERVICE)
    assert kinds(provisions) == [
        ("article", "25b"),
        ("paragraph", "1"), ("point", "a"), ("point", "b"),
        ("subparagraph", None),
        ("paragraph", "2"), ("point", "a"),
        ("paragraph", "3"),
    ]
    assert provisions[0].title == "ETS as a service"


def test_layout_splits_a_payload_carrying_several_articles():
    """"the following Articles are inserted:" is followed by one quoted block."""
    layout = ETS_AS_A_SERVICE[:-1] + [
        "     to supplement this Article with rules on the operation of ETS as a service.",
        "                                              Article 25c",
        "                                        Reporting obligations",
        "     1.      Member States shall report annually to the Commission.’;",
    ]
    provisions = structure(" ".join(l.strip() for l in layout), layout=layout)
    articles = [p for p in provisions if p.is_article]
    assert [(a.key, a.title) for a in articles] == [
        ("25b", "ETS as a service"), ("25c", "Reporting obligations")
    ]


def test_points_set_at_one_margin_are_still_points():
    """Article 1(2)(g) adds points (aj) to (ao) to Article 3, and the PDF sets the
    lot as one wrapped block at a single indent. With no indentation to read, the
    sequence is what says these are six points and not one paragraph."""
    layout = [
        "                 ‘(aj) ‘hazardous waste’ means hazardous waste as defined in",
        "                 Article 3, point (2) of Directive 2008/98/EC;",
        "                 (ak) ‘municipal waste’ means municipal waste as defined in",
        "                 Article 3, point (2b) of that Directive;",
        "                 (al) ‘synthetic fuels’ means renewable fuels of non-biological",
        "                 origin;’;",
    ]
    provisions = structure(" ".join(l.strip() for l in layout), layout=layout)
    assert kinds(provisions) == [("point", "aj"), ("point", "ak"), ("point", "al")]


def test_sub_points_at_one_margin_still_nest():
    """Article 1(2)(a) replaces point (e) with a point carrying two sub-points, all
    set at the same indent. "(i)" is no successor of "(e)", but it opens a sequence
    of its own — so it belongs a level down."""
    layout = [
        "           ‘(e) ‘installation’ means either of the following:",
        "           (i) a stationary technical unit where one or more activities are",
        "           carried out;",
        "           (ii) a CO2 transport infrastructure as defined in Article 3(29);’;",
    ]
    provisions = structure(" ".join(l.strip() for l in layout), layout=layout)
    assert kinds(provisions) == [
        ("point", "e"), ("subpoint", "i"), ("subpoint", "ii")
    ]


def test_text_after_a_list_of_points_is_not_part_of_the_last_one():
    """Article 3c(6) lists points (a) to (e) and then closes with a subparagraph of
    the paragraph — "Fuels shall be eligible …". Its points wrap at column 11, so a
    line back at column 5, where the markers sit, has left the list."""
    layout = [
        "     ‘6.    The allowances allocated under this paragraph shall cover:",
        "     (a)   50 % of the remaining price differential between the use of fossil",
        "           kerosene and advanced biofuels;",
        "     (b)   60 % of the remaining price differential between the use of fossil",
        "           kerosene and hydrogen from renewable energy sources;",
        "     Fuels shall be eligible for support referred to in this paragraph only if",
        "     they are produced in the EEA.’;",
    ]
    provisions = structure(" ".join(l.strip() for l in layout), layout=layout)
    assert kinds(provisions) == [
        ("paragraph", "6"), ("point", "a"), ("point", "b"), ("subparagraph", None)
    ]
    assert provisions[-1].text.startswith("Fuels shall be eligible")
    assert "Fuels shall" not in provisions[-2].text


def test_a_paragraph_break_with_nothing_else_to_show_it_is_honoured():
    """A subparagraph often begins with no number and at the same indent as the line
    above. Only the source's line spacing shows the break, which `a2t.extract.pdf`
    reads off poppler's layout analysis and passes here."""
    layout = [
        "     ‘Fuels shall be eligible for support referred to in this paragraph only",
        "     if they are produced in the EEA, or in a third country to which the",
        "     Union provides support pursuant to Article 25b.",
        "     The allocation of allowances under this paragraph may take into account",
        "     possible support from other schemes.’;",
    ]
    breaks = [True, False, False, True, False]
    provisions = structure(" ".join(l.strip() for l in layout), layout, breaks)

    assert kinds(provisions) == [("subparagraph", None), ("subparagraph", None)]
    assert provisions[1].text.startswith("The allocation of allowances")


def test_a_break_that_falls_mid_sentence_is_not_honoured():
    """Line spacing also shifts across a page break and around inline formatting, so
    a break is taken only where a sentence has ended — otherwise a wrapped line would
    become a provision starting mid-clause."""
    layout = [
        "     ‘(c) 100 % of the remaining price differential at airports located in",
        "         an outermost region, where",
        "         the physical delivery of the eligible aviation fuel took place;’;",
    ]
    breaks = [False, False, True]
    provisions = structure(" ".join(l.strip() for l in layout), layout, breaks)

    assert kinds(provisions) == [("point", "c")]
    assert provisions[0].text.endswith("took place;")


def test_points_wrapping_flush_keep_their_own_wrapped_lines():
    """Where a payload sets a point's continuation flush with its marker there is no
    outdent to read, and an unmarked line has to stay part of the point it follows —
    guessing a boundary would move words from one provision to another."""
    layout = [
        "       ‘(aj) ‘hazardous waste’ means hazardous waste as defined in",
        "       Article 3, point (2) of Directive 2008/98/EC;",
        "       (ak) ‘municipal waste’ means municipal waste;’;",
    ]
    provisions = structure(" ".join(l.strip() for l in layout), layout=layout)
    assert kinds(provisions) == [("point", "aj"), ("point", "ak")]
    assert provisions[0].text.endswith("Directive 2008/98/EC;")


def test_i_after_h_is_a_letter_not_a_numeral():
    """The reading that continues the open list wins, or every lettered list would
    fall a level deeper when it reached (i)."""
    layout = [
        "           ‘(g) the seventh point;",
        "           (h) the eighth point;",
        "           (i) the ninth point;",
        "           (j) the tenth point;’;",
    ]
    provisions = structure(" ".join(l.strip() for l in layout), layout=layout)
    assert kinds(provisions) == [
        ("point", "g"), ("point", "h"), ("point", "i"), ("point", "j")
    ]


def test_an_outdented_block_does_not_relevel_the_payload():
    """The body's left margin is where it starts, not the shallowest line in it.
    Article 1(4)(a) of COM(2026) 616 closes with a subparagraph set further out than
    the paragraph it belongs to; measuring from that would make the paragraph's own
    "1." look like a point of something."""
    layout = [
        "                 ‘1.     In respect of ships, the following applies:",
        "                       (a)   half of the emissions released during voyages;",
        "           However, this shall not apply to offshore operations.’;",
    ]
    provisions = structure(" ".join(l.strip() for l in layout), layout=layout)
    assert kinds(provisions) == [
        ("paragraph", "1"), ("point", "a"), ("subparagraph", None)
    ]


def test_title_wrapping_the_full_column_is_still_a_title():
    """Titles are centred, so a long one clears the body indent by a single space.
    Article 3gaa of COM(2026) 616 final wraps onto two lines, the first of them at
    column 6 against a body at column 5."""
    layout = [
        "                                             ‘Article 3gaa",
        "      Reservation and allocation of allowances for the use of sustainable maritime fuels and",
        "                                 zero-emission propulsion technologies",
        "     1.      Notwithstanding Article 10(1), a maximum of 110 million allowances shall be",
        "     reserved for the use of sustainable maritime fuels.’;",
    ]
    provisions = structure(" ".join(l.strip() for l in layout), layout=layout)
    assert provisions[0].title == (
        "Reservation and allocation of allowances for the use of sustainable maritime "
        "fuels and zero-emission propulsion technologies"
    )
    assert kinds(provisions) == [("article", "3gaa"), ("paragraph", "1")]


def test_splitting_a_payload_loses_none_of_it():
    """A block that quietly swallowed or duplicated words would put the wrong text
    into the act — worse than the blob it replaced."""
    provisions = structure(" ".join(l.strip() for l in ETS_AS_A_SERVICE),
                           layout=ETS_AS_A_SERVICE)
    rebuilt = " ".join(
        f"Article {p.key} {p.title or ''}" if p.is_article else f"{p.key or ''} {p.text}"
        for p in provisions
    )
    words = lambda s: re.findall(r"\w+", s)
    assert words(rebuilt) == words(" ".join(l.strip() for l in ETS_AS_A_SERVICE))


def test_flat_payload_falls_back_to_its_markers():
    """Without the source lines the markers still give paragraphs and points."""
    provisions = structure(
        "Article 1a Reporting duties 1. Member States shall report annually. "
        "2. The report shall cover: (a) emissions; (b) removals.",
        named="1a",
    )
    assert kinds(provisions) == [
        ("article", "1a"), ("paragraph", "1"), ("paragraph", "2"),
        ("point", "a"), ("point", "b"),
    ]
    assert provisions[0].title == "Reporting duties"


@pytest.mark.parametrize("payload", [
    # A marker that opens no sequence.
    "Emissions shall be reported in accordance with Article 3, point (2) of "
    "Regulation (EU) 2018/1999 and with (b) of that point.",
    # A marker shaped like the first of a sequence, but mid-sentence.
    "The operator shall report emissions covering (a) combustion of fuels and "
    "any other activity listed in Annex I.",
    # A figure that reads as a numbered paragraph.
    "The factor shall be reduced by 1. Member States shall publish the result.",
])
def test_flat_marker_shaped_text_does_not_split_a_payload(payload):
    """Legislation is dense with text shaped like a marker. A split has to be earned
    by continuing a sequence *and* by following the end of a sentence, or a citation
    would break a provision in two."""
    assert len(structure(payload)) == 1


def test_a_payload_with_no_structure_is_returned_whole():
    provisions = structure("The competent authority shall publish the decision.")
    assert len(provisions) == 1
    assert provisions[0].text == "The competent authority shall publish the decision."


def test_empty_payload_yields_nothing():
    assert structure("") == []


# ----------------------------------------------------------------------- writer


def test_spans_render_without_re_diffing():
    doc = TrackedDocx()
    edit = diff("alpha beta gamma", "alpha delta gamma")
    block = doc.from_spans(edit.spans)
    assert len(block.runs) == len(edit.spans)


def test_revision_ids_are_unique_across_a_document(tmp_path):
    act = make_act()
    paragraphs = act.article("1").of_kind(Kind.PARAGRAPH)
    changeset = ChangeSet(act=act, changes=[
        change(ChangeKind.REPLACE, p, p.text, f"New {i}.")
        for i, p in enumerate(paragraphs)
    ])
    result = emit_document(act, changeset, tmp_path / "a.docx")
    doc = load(result.path)
    ids = [e.get(f"{W}id") for e in list(doc.iter(f"{W}ins")) + list(doc.iter(f"{W}del"))]
    assert len(ids) == len(set(ids))


# ------------------------------------------------------------------------ report


def make_report(act: Act, changeset: ChangeSet) -> RunReport:
    block = TargetBlock(celex="TESTACT", citation="Test Directive", root_scope=None)
    return RunReport(
        source=FsPath("proposal.pdf"), proposal_date="2026-07-17",
        outcomes=[ActOutcome(block=block, celex="TESTACT", version="TESTACT-20240301",
                             title="Test Directive", resolution=ResolutionReport(),
                             changeset=changeset)],
    )


def test_report_lists_every_change():
    act = make_act()
    node = act.article("1").by_key(Kind.PARAGRAPH, "1")
    changeset = ChangeSet(act=act, changes=[
        change(ChangeKind.REPLACE, node, node.text, "New text.")
    ])
    rendered = make_report(act, changeset).render()
    assert "`1.1`" in rendered
    assert "Article 1 / paragraph 1" in rendered
    assert "2026-07-17" in rendered


def test_report_has_a_review_section():
    act = make_act()
    node = act.article("1").by_key(Kind.PARAGRAPH, "1")
    instr = instruction(confidence=0.4)
    instr.note("possible numbering error in the proposal")
    changeset = ChangeSet(act=act, changes=[
        change(ChangeKind.REPLACE, node, node.text, "New.", instr=instr)
    ])
    rendered = make_report(act, changeset).render()
    assert "## Needs human review" in rendered
    assert "possible numbering error" in rendered


def test_report_says_so_when_nothing_is_flagged():
    act = make_act()
    node = act.article("1").by_key(Kind.PARAGRAPH, "1")
    changeset = ChangeSet(act=act, changes=[
        change(ChangeKind.REPLACE, node, node.text, "New.")
    ])
    assert "Nothing flagged." in make_report(act, changeset).render()


def test_report_records_unapplied_instructions():
    act = make_act()
    changeset = ChangeSet(act=act, changes=[
        Change(ChangeKind.SKIPPED, instruction(), note="unresolved target")
    ])
    rendered = make_report(act, changeset).render()
    assert "### Not applied" in rendered
    assert "unresolved target" in rendered
