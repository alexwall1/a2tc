"""Phase 8: emitting the tracked-changes document and the report."""

import zipfile
from pathlib import Path as FsPath

import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt
from lxml import etree

from a2t.apply.changeset import Change, ChangeKind, ChangeSet
from a2t.apply.diff import diff
from a2t.emit.document import DocumentEmitter, emit_document
from a2t.emit.report import ActOutcome, RunReport
from a2t.emit.tracked_docx import TrackedDocx
from a2t.model.instruction import Instruction, Op, TargetBlock
from a2t.model.tree import Act, Kind, Node
from a2t.resolve.resolver import ResolutionReport

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
