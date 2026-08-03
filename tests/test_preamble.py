"""The amending act's own preamble: parsing it, and inserting it ahead of the articles.

Asserted against COM(2026) 616 final, with the held-out COM(2023) 702 final repeating
every structural claim — the two proposals differ in how poppler renders their footnote
references, which is exactly the thing most likely to be over-fitted.
"""

import zipfile
from pathlib import Path as FsPath

import pytest
from lxml import etree

from a2t.apply.changeset import ChangeSet
from a2t.emit.document import emit_document
from a2t.extract.pdf import Line, extract_lines, superscripts
from a2t.extract.preamble import (
    Para,
    Piece,
    Preamble,
    PreambleError,
    _split_recitals,
    find_preamble,
    preamble,
)
from a2t.model.tree import Act, Kind, Node

ROOT = FsPath(__file__).resolve().parents[1]
PDF = ROOT / "amendment_proposal_ets.pdf"
HELD_OUT = ROOT / "amending proposal intermodal.pdf"

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

pytestmark = pytest.mark.skipif(not PDF.is_file(), reason="sample PDF not present")


@pytest.fixture(scope="module")
def ets() -> Preamble:
    return preamble(extract_lines(PDF), PDF)


@pytest.fixture(scope="module")
def intermodal() -> Preamble:
    if not HELD_OUT.is_file():
        pytest.skip("held-out proposal not present")
    return preamble(extract_lines(HELD_OUT), HELD_OUT)


# ----------------------------------------------------------------------- bounds


def test_preamble_skips_the_cover_page():
    """A COM proposal prints its title block twice; the preamble's is the second."""
    lines = extract_lines(PDF)
    start, end = find_preamble(lines)
    assert lines[start].lineno == 1531  # not the cover block at line 10
    assert lines[end - 1].stripped == "HAVE ADOPTED THIS DIRECTIVE:"


def test_preamble_needs_an_enacting_formula():
    lines = [Line("Whereas:", 1, 1), Line("(1) Something.", 1, 2)]
    with pytest.raises(PreambleError, match="enacting formula"):
        find_preamble(lines)


def test_preamble_needs_an_institutions_line():
    lines = [Line("Whereas:", 1, 1), Line("HAVE ADOPTED THIS DIRECTIVE:", 1, 2)]
    with pytest.raises(PreambleError, match="institutions line"):
        find_preamble(lines)


# ------------------------------------------------------------------- title block


def test_title_block(ets):
    assert ets.procedure == "2026/0212 (COD)"
    titles = [para.text for para in ets.title]
    assert titles[0] == "Proposal for a"
    assert titles[1] == "DIRECTIVE OF THE EUROPEAN PARLIAMENT AND OF THE COUNCIL"
    assert titles[2].startswith("amending Directive 2003/87/EC and Decision (EU) 2015/1814")
    assert titles[-1] == "(Text with EEA relevance)"


def test_institutions_and_enacting_formula(ets):
    assert ets.institutions == (
        "THE EUROPEAN PARLIAMENT AND THE COUNCIL OF THE EUROPEAN UNION,"
    )
    assert ets.whereas == "Whereas:"
    assert ets.enacting == "HAVE ADOPTED THIS DIRECTIVE:"


def test_citations_are_one_paragraph_each(ets):
    texts = [para.text for para in ets.citations]
    assert len(texts) == 6
    # The first citation wraps across two source lines and must come back as one.
    assert texts[0] == (
        "Having regard to the Treaty on the Functioning of the European Union, "
        "and in particular Article 192(1) thereof,"
    )
    assert texts[-1] == "Acting in accordance with the ordinary legislative procedure,"


# ----------------------------------------------------------------------- recitals


def test_recitals_are_complete_and_in_order(ets):
    assert len(ets.recitals) == 98
    assert [r.number for r in ets.recitals] == [str(n) for n in range(1, 99)]
    assert ets.recitals[0].text.startswith("The Paris Agreement")
    assert ets.recitals[-1].text.startswith(
        "Directive 2003/87/EC and Decision (EU) 2015/1814"
    )
    assert not ets.notes


def test_held_out_recitals(intermodal):
    assert len(intermodal.recitals) == 36
    assert [r.number for r in intermodal.recitals] == [str(n) for n in range(1, 37)]
    assert intermodal.procedure == "2023/0396 (COD)"
    assert not intermodal.notes


def test_a_bracketed_number_out_of_sequence_does_not_open_a_recital():
    """Recitals quote figures in brackets; only a successor starts a new one."""
    lines = [
        Line("     (1)     The first recital, which refers to", 1, 1),
        Line("     (7)     of some other instrument entirely.", 1, 2),
        Line("     (2)     The second recital.", 1, 3),
    ]
    grouped = _split_recitals(lines)
    assert [number for number, _ in grouped] == ["1", "2"]
    assert len(grouped[0][1]) == 2  # the stray "(7)" stayed with recital 1


def test_recital_text_carries_no_footnote_apparatus(intermodal):
    """A footnote body wrapping back to the marker's indent must not join the recital.

    Footnote 9 of the held-out proposal does exactly that, and before the block
    detector allowed it the ISBN of a Commission handbook was read as legislation.
    """
    eighteen = next(r for r in intermodal.recitals if r.number == "18")
    assert "ISBN" not in eighteen.text
    assert "Handbook on the external costs" not in eighteen.text


# ---------------------------------------------------------------------- footnotes


def test_superscripts_are_read_from_the_geometry():
    """Every reference, in document order, with the word it follows."""
    marks = superscripts(PDF, 30, 59)
    assert [int(number) for _, number in marks] == list(range(1, len(marks) + 1))
    assert marks[0] == ("Committee", "1")
    assert marks[2] == ("Agreement", "3")


def test_a_figure_in_the_running_text_is_not_a_reference(ets):
    """"on 12 December 2015" and "1,5 °C" sit in recitals that also carry markers."""
    first = ets.recitals[0]
    superscripted = [p.text for p in first.pieces if p.superscript]
    assert superscripted == ["3"]
    assert "12 December 2015" in first.text
    second = ets.recitals[1]
    assert [p.text for p in second.pieces if p.superscript] == []
    assert "1,5 °C" in second.text


@pytest.mark.parametrize("name", ["ets", "intermodal"])
def test_every_reference_is_placed_and_has_its_text(name, request):
    front = request.getfixturevalue(name)
    placed = [
        piece.text
        for para in (*front.citations, *front.recitals)
        for piece in para.pieces
        if piece.superscript
    ]
    assert placed == [str(n) for n in range(1, len(placed) + 1)]
    assert {number for number, _ in front.footnotes} == set(placed)
    assert all(text for _, text in front.footnotes)


def test_footnotes_below_the_enacting_formula_are_still_collected(intermodal):
    """The last recitals' footnotes are printed under the formula, on the same page."""
    numbers = {number for number, _ in intermodal.footnotes}
    assert {"12", "13"} <= numbers


def test_a_reference_keeps_no_space_before_it(intermodal):
    """Poppler separates the marker on this document; the printed page does not."""
    first = intermodal.recitals[0]
    before = next(
        first.pieces[i - 1] for i, p in enumerate(first.pieces) if p.superscript
    )
    assert before.text.endswith("Council")


# ----------------------------------------------------------------------- emission


def make_act() -> Act:
    act = Act(celex="TESTACT", title="Test Directive", version_date="2024-03-01")
    article = act.root.add(Node(Kind.ARTICLE, key="1", title="Scope"))
    article.add(Node(Kind.PARAGRAPH, key="1", text="The original first paragraph."))
    return act


def small_preamble() -> Preamble:
    return Preamble(
        procedure="2026/0212 (COD)",
        title=(Para((Piece("Proposal for a"),)),
               Para((Piece("DIRECTIVE OF THE EUROPEAN PARLIAMENT"),))),
        institutions="THE EUROPEAN PARLIAMENT AND THE COUNCIL OF THE EUROPEAN UNION,",
        citations=(Para((Piece("Having regard to the Treaty,"),)),),
        recitals=(Para((Piece("The first reason"), Piece("1", True), Piece(".")),
                       number="1"),
                  Para((Piece("The second reason."),), number="2")),
        enacting="HAVE ADOPTED THIS DIRECTIVE:",
        footnotes=(("1", "OJ L 282, 19.10.2016, p. 4."),),
        source_lines=(1531, 3130),
    )


def emit(tmp_path, front) -> etree._Element:
    act = make_act()
    path = emit_document(act, ChangeSet(act), tmp_path / "out.docx",
                         preamble=front).path
    return etree.fromstring(zipfile.ZipFile(path).read("word/document.xml"))


def styles_in_order(doc) -> list[str]:
    """Style ids in document order. Word's ids drop the spaces: "OJ Title" -> OJTitle."""
    out = []
    for p in doc.iter(f"{W}p"):
        style = p.find(f"{W}pPr/{W}pStyle")
        out.append(style.get(f"{W}val") if style is not None else "Normal")
    return out


def test_no_preamble_by_default(tmp_path):
    doc = emit(tmp_path, None)
    text = " ".join(t.text or "" for t in doc.iter(f"{W}t"))
    assert "HAVE ADOPTED" not in text
    assert "Proposal for a" not in text


def test_preamble_order(tmp_path):
    doc = emit(tmp_path, small_preamble())
    order = styles_in_order(doc)
    # Base-act front matter, then the proposal's preamble, then the articles.
    assert order[:2] == ["OJTitle", "OJNote"]
    assert order[2:8] == [
        "OJProcedure", "OJProposalLead", "OJProposalTitle",
        "OJFormula", "OJFormula", "OJFormula",  # institutions, citation, Whereas:
    ]
    assert order[8:10] == ["OJRecital", "OJRecital"]
    assert order[10:12] == ["OJFootnote", "OJFootnote"]  # rule, then the note
    assert order[12] == "OJFormula"  # HAVE ADOPTED THIS DIRECTIVE:
    assert order[13] == "OJArticle"


def test_every_preamble_paragraph_is_an_insertion(tmp_path):
    doc = emit(tmp_path, small_preamble())
    paragraphs = list(doc.iter(f"{W}p"))
    # Everything from the procedure number to the enacting formula inclusive.
    for p in paragraphs[2:13]:
        assert p.findall(f"{W}ins"), etree.tostring(p)[:200]
        assert p.find(f"{W}pPr/{W}rPr/{W}ins") is not None
    assert not paragraphs[13].findall(f"{W}ins")  # the article is untouched


def test_recital_number_is_written_with_a_tab(tmp_path):
    doc = emit(tmp_path, small_preamble())
    recital = [
        p for p in doc.iter(f"{W}p")
        if (s := p.find(f"{W}pPr/{W}pStyle")) is not None
        and s.get(f"{W}val") == "OJRecital"
    ][0]
    assert " ".join(t.text or "" for t in recital.iter(f"{W}t")).startswith("(1)")
    assert recital.findall(f".//{W}tab")


def test_reference_is_superscript_and_inserted(tmp_path):
    doc = emit(tmp_path, small_preamble())
    marks = doc.findall(f".//{W}vertAlign")
    assert [m.get(f"{W}val") for m in marks] == ["superscript", "superscript"]
    for mark in marks:
        run = mark.getparent().getparent()
        assert run.getparent().tag == f"{W}ins"


def test_result_counts_the_preamble(tmp_path):
    act = make_act()
    result = emit_document(act, ChangeSet(act), tmp_path / "out.docx",
                           preamble=small_preamble())
    assert result.preamble_paragraphs == 11
    assert "of preamble" in result.summary()
