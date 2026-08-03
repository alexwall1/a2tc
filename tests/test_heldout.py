"""Phase 9: the held-out proposal.

COM(2023) 702 final (intermodal transport) was never used while building the pipeline.
Every case here is a construction the ETS proposal does not contain, so these tests
guard against the pipeline being over-fitted to a single document.
"""

from pathlib import Path as FsPath

import pytest

from a2t.apply.changeset import ChangeKind, build_changeset
from a2t.detect.celex import parse_citation
from a2t.detect.targets import detect_targets, find_proposal_date
from a2t.extract.deboilerplate import operative_part
from a2t.extract.pdf import extract_lines
from a2t.model.assemble import assemble
from a2t.model.instruction import Op
from a2t.model.tree import Kind
from a2t.parse.rules import classify_all
from a2t.parse.segment import segment
from a2t.resolve.resolver import resolve_block
from a2t.retrieval.base import title_matches

ROOT = FsPath(__file__).resolve().parents[1]
PDF = ROOT / "amending proposal intermodal.pdf"
CACHE = ROOT / "out" / "cache" / "01992L0106-20130701"

pytestmark = pytest.mark.skipif(not PDF.is_file(), reason="held-out PDF not present")


@pytest.fixture(scope="module")
def lines():
    return extract_lines(PDF)


@pytest.fixture(scope="module")
def detection(lines):
    return detect_targets(operative_part(lines).lines, lines)


@pytest.fixture(scope="module")
def blocks(lines, detection):
    result = segment(operative_part(lines).lines, detection.targets)
    classify_all(result)
    return result


# ------------------------------------------------------------------- detection


def test_detects_both_targets(detection):
    assert [t.celex for t in detection.targets] == ["31992L0106", "32020R1056"]
    assert all(t.celex_source == "eli" for t in detection.targets)


def test_two_digit_year_act_resolves(detection):
    """Directive 92/106/EEC — the year is 1992, not 92."""
    citation = detection.targets[0].citation
    assert (citation.year, citation.number) == (1992, 106)
    assert citation.consolidated_celex == "01992L0106"


def test_proposal_date_read_from_cover(lines):
    assert find_proposal_date(lines).isoformat() == "2023-11-07"


def test_title_with_two_as_regards_clauses_is_understood(detection):
    """The cover title pairs each act with its own "as regards" clause, so a
    non-greedy match would see only the first act and report a false discrepancy."""
    assert detection.discrepancies == []


def test_two_digit_year_title_verification():
    """The act's own title reads "92/106/EEC", never "1992/106"."""
    citation = parse_citation("Directive 92/106/EEC")
    assert title_matches(
        citation,
        "Council Directive 92/106/EEC of 7 December 1992 on the establishment of "
        "common rules for certain types of combined transport of goods",
    )
    assert not title_matches(citation, "Council Directive 96/61/EC concerning …")


# ---------------------------------------------------------------- segmentation


def test_instruction_counts(blocks):
    assert [len(b.instructions) for b in blocks] == [11, 1]


def test_everything_classifies(blocks):
    assert [i for b in blocks for i in b.walk() if i.op is Op.UNKNOWN] == []


def test_unnumbered_single_instruction_is_found(blocks):
    """Article 2 of the proposal carries one instruction with no "(1)" marker."""
    instruction = blocks[1].instructions[0]
    assert instruction.op is Op.INSERT_NODE
    assert instruction.raw.startswith("In Article 9(1), the following point (l)")
    assert any("carries no number" in n for n in instruction.notes)
    assert instruction.payload


def test_enumerated_insert_is_classified(blocks):
    """"the following Articles 1a, 1b and 1c are inserted"."""
    instruction = next(i for i in blocks[0].walk() if i.id == "1.3")
    assert instruction.op is Op.INSERT_NODE
    assert instruction.target_text == "Articles 1a, 1b and 1c"


def test_annex_addition_is_external(blocks):
    """"the text set out in the Annex to this Directive is added as the Annex"."""
    instruction = next(i for i in blocks[0].walk() if i.id == "1.11")
    assert instruction.op is Op.EXTERNAL


def test_plural_scope_is_stripped(blocks):
    """"In Article 6, paragraphs 1 and 2 are replaced" — the noun is plural."""
    instruction = next(i for i in blocks[0].walk() if i.id == "1.7")
    assert instruction.scope_text == "Article 6"
    assert instruction.target_text == "paragraphs 1 and 2"


def test_act_title_replacement_is_parsed(blocks):
    instruction = blocks[0].instructions[0]
    assert instruction.op is Op.REPLACE_NODE
    assert instruction.target_text == "the title"


# ------------------------------------------------------- resolution and changes


needs_cache = pytest.mark.skipif(
    not (CACHE / "works.parquet").is_file(),
    reason="run `a2t convert 'amending proposal intermodal.pdf' --cache out/cache`",
)


@pytest.fixture(scope="module")
def directive(blocks):
    import polars as pl

    works = pl.read_parquet(CACHE / "works.parquet").row(0, named=True)
    act = assemble(works["full_text_html"], "01992L0106-20130701", works["title"])
    report = resolve_block(blocks[0], act)
    return act, report, build_changeset(report, act)


@needs_cache
def test_everything_resolves(directive):
    _, report, _ = directive
    assert report.unresolved == []
    assert report.coverage == 1.0


@needs_cache
def test_act_title_resolves_to_the_root(directive):
    act, report, _ = directive
    result = next(r for r in report.results if r.instruction.id == "1.1")
    assert result.targets_title
    assert result.node is act.root
    assert act.root.title == act.title


@needs_cache
def test_act_title_becomes_a_title_edit(directive):
    _, _, changeset = directive
    change = next(c for c in changeset.renderable if c.instruction.id == "1.1")
    assert change.kind is ChangeKind.TITLE_EDIT
    assert "support framework for intermodal transport" in change.new_text
    assert "common rules" in change.old_text


@needs_cache
def test_plural_scope_resolves_to_both_paragraphs(directive):
    _, report, _ = directive
    result = next(r for r in report.results if r.instruction.id == "1.7")
    assert result.enumerated
    assert [n.key for n in result.targets] == ["1", "2"]
    assert all("Article 6" in n.citation for n in result.targets)


@needs_cache
def test_repealed_article_is_a_deletion(directive):
    _, _, changeset = directive
    change = next(c for c in changeset.renderable if c.instruction.id == "1.2")
    assert change.kind is ChangeKind.DELETE
    assert change.node.kind is Kind.ARTICLE
    assert change.node.key == "1"


@needs_cache
def test_all_changes_are_placed_in_the_document(directive, tmp_path):
    from a2t.emit.document import emit_document

    act, _, changeset = directive
    result = emit_document(act, changeset, tmp_path / "out.docx")
    assert result.unrendered == []


@needs_cache
def test_repealed_article_heading_is_struck(directive, tmp_path):
    """Otherwise accepting the change leaves "Article 1" over nothing."""
    import zipfile

    from lxml import etree

    from a2t.emit.document import emit_document

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    act, _, changeset = directive
    result = emit_document(act, changeset, tmp_path / "out.docx")
    doc = etree.fromstring(zipfile.ZipFile(result.path).read("word/document.xml"))
    struck = " ".join(t.text or "" for t in doc.iter(f"{W}delText"))
    assert "Article 1" in struck


# ------------------------------------------------- replacing several whole articles


@needs_cache
def test_articles_replaced_together_keep_all_their_text(directive):
    """Art. 1(4): "Articles 2 and 3 are replaced by the following:".

    Cut on the provision numbers, as a payload of several new *paragraphs* is, the
    markers found are the new Article 3's paragraphs 2. and 3. — so Article 2 took
    Article 3's second paragraph, Article 3 took its third, and the new Article 2
    entirely, plus Article 3's first paragraph, were dropped without a word.
    """
    _, _, changeset = directive
    changes = [c for c in changeset.renderable if c.instruction.id == "1.4"]
    assert [c.node.key for c in changes] == ["2", "3"]

    two, three = changes
    assert two.new_text.startswith("Article 2")
    assert "No quota systems and systems of authorisations" in two.new_text
    # Article 3's paragraphs belong to Article 3.
    assert "transport information" not in two.new_text

    assert three.new_text.startswith("Article 3")
    assert "In order to benefit from the support framework" in three.new_text
    assert "2. The transport information referred to in paragraph 1" in three.new_text
    assert "3. Transport information recorded pursuant to paragraph 2" in three.new_text

    # Between them the two segments account for the whole payload.
    payload = changes[0].instruction.payload
    assert len(two.new_text) + len(three.new_text) >= len(payload) - 2


@needs_cache
def test_an_article_replaced_in_full_states_its_heading_once(directive):
    """The segment opens with "Article 3"; the act's own heading must not print too."""
    _, _, changeset = directive
    three = next(c for c in changeset.renderable
                 if c.instruction.id == "1.4" and c.node.key == "3")
    assert any(pair.block.is_article for pair in three.blocks)
    assert three.blocks[0].block.key == "3"


@needs_cache
def test_the_footnote_of_a_replaced_article_survives_the_split(directive):
    """The marker sits in the new Article 3's first paragraph — the one that was lost."""
    _, _, changeset = directive
    three = next(c for c in changeset.renderable
                 if c.instruction.id == "1.4" and c.node.key == "3")
    assert "*" in three.new_text
    assert three.instruction.footnotes
    assert "Regulation (EU) 1056/2020" in three.instruction.footnotes[0]
