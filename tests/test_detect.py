"""Phase 2 acceptance tests: target detection and CELEX resolution."""

from pathlib import Path

import pytest

from a2t.detect.celex import (
    Citation,
    eli_index,
    iter_citations,
    parse_citation,
    parse_eli,
)
from a2t.detect.targets import ANCHOR, detect_targets
from a2t.extract.deboilerplate import operative_part
from a2t.extract.pdf import Line, extract_lines

PDF = Path(__file__).resolve().parents[1] / "amendment_proposal_ets.pdf"

# Skipping lives on the fixtures that read the sample, not on the module: the tests
# below that build their own input need no sample and must run on a fresh clone.
MISSING = "sample PDF not present"


@pytest.fixture(scope="module")
def lines():
    if not PDF.is_file():
        pytest.skip(MISSING)
    return extract_lines(PDF)


@pytest.fixture(scope="module")
def detection(lines):
    return detect_targets(operative_part(lines).lines, lines)


# ------------------------------------------------------------------ citation grammar


@pytest.mark.parametrize(
    "text, year, number, celex",
    [
        # year-first: the modern and the pre-2015 directive style
        ("Directive 2003/87/EC", 2003, 87, "32003L0087"),
        ("Decision (EU) 2015/1814", 2015, 1814, "32015D1814"),
        ("Regulation (EU) 2015/757", 2015, 757, "32015R0757"),
        ("Regulation (EU) 2023/1805", 2023, 1805, "32023R1805"),
        # number-first: the "No" forms reverse the order
        ("Regulation (EC) No 219/2009", 2009, 219, "32009R0219"),
        ("Decision No 1359/2013/EU", 2013, 1359, "32013D1359"),
        # two-digit years
        ("Directive 96/61/EC", 1996, 61, "31996L0061"),
        ("Directive 85/337/EEC", 1985, 337, "31985L0337"),
        # qualifiers and prefixes must not break parsing
        ("Council Directive 96/61/EC", 1996, 61, "31996L0061"),
        ("Commission Delegated Regulation (EU) 2019/331", 2019, 331, "32019R0331"),
        ("Regulation (EU, Euratom) 2018/1046", 2018, 1046, "32018R1046"),
    ],
)
def test_citation_grammar(text, year, number, celex):
    got = parse_citation(text)
    assert got is not None, text
    assert (got.year, got.number, got.celex) == (year, number, celex)


def test_consolidated_celex_is_sector_zero():
    assert parse_citation("Directive 2003/87/EC").consolidated_celex == "02003L0087"


def test_non_citations_rejected():
    for text in ["Article 3", "Article 10a(1a)", "paragraph 6", "Annex I", ""]:
        assert parse_citation(text) is None, text


def test_iter_citations_dedupes_and_orders():
    text = ("amending Directive 2003/87/EC, Decision (EU) 2015/1814, "
            "Regulation (EU) 2015/757 and Regulation (EU) 2023/1805")
    assert [c.celex for c in iter_citations(text)] == [
        "32003L0087", "32015D1814", "32015R0757", "32023R1805"
    ]


# --------------------------------------------------------------------------- ELI


@pytest.mark.parametrize(
    "uri, celex",
    [
        ("http://data.europa.eu/eli/dir/2003/87/oj", "32003L0087"),
        ("http://data.europa.eu/eli/dec/2015/1814/oj", "32015D1814"),
        ("http://data.europa.eu/eli/reg/2024/1735/oj", "32024R1735"),
        ("http://data.europa.eu/eli/reg_del/2019/331/oj", "32019R0331"),
    ],
)
def test_eli_to_celex(uri, celex):
    assert parse_eli(uri) == celex


def test_eli_ignores_non_act_types():
    assert parse_eli("http://data.europa.eu/eli/agree_interinstit/2016/512/oj") is None


def test_eli_index_finds_both_targets(lines):
    index = eli_index(lines)
    assert "32003L0087" in index
    assert "32015D1814" in index


# ------------------------------------------------------------------------ detection


def test_detects_exactly_two_targets(detection):
    assert len(detection.targets) == 2


def test_target_celex_and_scope(detection):
    first, second = detection.targets
    assert (first.celex, first.consolidated_celex) == ("32003L0087", "02003L0087")
    assert first.root_scope is None          # whole act
    assert first.amending_article == "1"

    assert (second.celex, second.consolidated_celex) == ("32015D1814", "02015D1814")
    assert second.root_scope == "1"          # only Article 1 of the Decision
    assert second.amending_article == "2"


def test_both_targets_resolved_via_eli(detection):
    assert [t.celex_source for t in detection.targets] == ["eli", "eli"]


def test_targets_have_headers(detection):
    assert all(t.header_line is not None for t in detection.targets)


def test_instruction_containers_are_not_targets(lines):
    """25 lines say 'is amended as follows' but only 2 are amendment targets."""
    op = operative_part(lines).lines
    anchors = [ln for ln in op if ANCHOR.match(ln.stripped)]
    assert len(anchors) == 25
    assert len(detect_targets(op, lines).targets) == 2


def test_cross_references_are_not_targets(detection):
    """The proposal cites 51 acts; cross-references must not be picked up."""
    celexes = {t.celex for t in detection.targets}
    for cited in ["32008L0098", "32024R1735", "32021R1119", "32024R2803"]:
        assert cited not in celexes


# --------------------------------------------------------------------- discrepancies


def test_financial_statement_discrepancy_reported(detection):
    """FS §1.1 claims four amended acts; the operative part amends two."""
    claimed = [d for d in detection.discrepancies if d.kind == "claimed-not-amended"]
    assert len(claimed) == 1
    assert "2015/757" in claimed[0].message
    assert "2023/1805" in claimed[0].message


def test_no_spurious_discrepancies(detection):
    """Footnotes cite other acts' titles; those must not read as self-description."""
    assert len(detection.discrepancies) == 1


def test_cover_title_agrees_and_is_silent(detection):
    """The cover title names exactly the two amended acts, so it raises nothing."""
    assert not [d for d in detection.discrepancies if d.line and d.line < 100]


# -------------------------------------------------------------------------- anchors


def test_anchor_requires_citation_in_subject_position():
    """This is the whole discriminator between a target and an instruction."""
    target = "Directive 2003/87/EC is amended as follows:"
    container = "Article 3 is amended as follows:"
    assert ANCHOR.match(target) and ANCHOR.match(container)
    assert parse_citation(ANCHOR.match(target).group("act")) is not None
    assert parse_citation(ANCHOR.match(container).group("act")) is None


def test_scoped_anchor_extracts_root_scope():
    match = ANCHOR.match("Article 1 of Decision (EU) 2015/1814 is amended as follows:")
    assert match.group("scope") == "1"
    assert parse_citation(match.group("act")).celex == "32015D1814"


def test_detection_without_all_lines_still_works(lines):
    """ELI resolution degrades to citation grammar, detection still succeeds."""
    op = operative_part(lines).lines
    result = detect_targets(op)
    assert len(result.targets) == 2
    assert [t.celex for t in result.targets] == ["32003L0087", "32015D1814"]


# ------------------------------------------------- documents that amend only annexes


def _operative(*texts):
    from a2t.extract.pdf import Line

    return [Line(t, 1, i + 1) for i, t in enumerate(texts)]


@pytest.mark.parametrize(
    "sentence, act",
    [
        # Wordings seen in adopted acts amending Directives 2012/27, 2003/87 and
        # Regulations 2019/631 and 2017/745.
        ("Annex VIII to Directive 2012/27/EU is replaced by the text in Annex I to "
         "this Directive.", "Directive 2012/27/EU"),
        ("In Directive 2003/87/EC, in Annex I, the second paragraph of the entry "
         "‘Aviation’ is amended as follows:", "Directive 2003/87/EC"),
        ("Annexes I and III to Regulation (EU) 2019/631 are amended in accordance with "
         "the Annex to this Regulation.", "Regulation (EU) 2019/631"),
        ("In Part C of Annex VI to Regulation (EU) 2017/745 the following sections "
         "are added:", "Regulation (EU) 2017/745"),
    ],
)
def test_a_document_amending_only_an_annex_says_so(sentence, act):
    found = detect_targets(_operative("Article 1", sentence))
    assert found.targets == []
    assert found.annexes == [act]
    assert "annexes are out of scope" in found.explain_empty()
    assert act in found.explain_empty()


def test_a_recital_wishing_an_annex_amended_is_not_an_amendment():
    found = detect_targets(_operative(
        "(8) Annexes I and III to Regulation (EU) 2019/631 should therefore be "
        "amended accordingly."))
    assert found.annexes == []
    assert found.explain_empty() == "no amendment targets found in the operative part"


def test_annexes_are_not_searched_for_when_an_act_is_amended():
    found = detect_targets(_operative(
        "Article 1", "Directive 2003/87/EC is amended as follows:",
        "(1) in Article 3, point (h) is replaced by the following:",
        "Annex I to Directive 2003/87/EC is replaced by the following:"))
    assert [t.celex for t in found.targets] == ["32003L0087"]
    assert found.annexes == []


def test_a_linking_word_before_the_act_does_not_hide_the_anchor():
    """Regulation (EC) No 219/2009 introduces its amendments of No 1257/96 this way."""
    found = detect_targets(_operative(
        "Article 1", "Accordingly, Regulation (EC) No 1257/96 is hereby amended as "
        "follows:", "1. in Article 13, the fourth paragraph shall be replaced by the "
        "following:"))
    assert [t.celex for t in found.targets] == ["31996R1257"]


def test_a_quoted_sentence_with_a_linking_word_is_still_not_an_anchor():
    found = detect_targets(_operative(
        "Article 1", "Accordingly, Article 3 is amended as follows:"))
    assert found.targets == []


@pytest.mark.parametrize(
    "text, celex",
    [
        ("Regulation (EEC) No 2913/92", "31992R2913"),
        ("Regulation (EC) No 1257/96", "31996R1257"),
        ("Regulation (EC) No 219/2009", "32009R0219"),
    ],
)
def test_a_two_digit_year_after_the_number_is_the_20th_century(text, celex):
    assert parse_citation(text).celex == celex


# --------------------------- an article that is itself one instruction naming the act


def _blocks(*texts):
    from a2t.parse.rules import classify_all
    from a2t.parse.segment import segment

    operative = _operative(*texts)
    blocks = segment(operative, detect_targets(operative).targets)
    classify_all(blocks)
    return blocks


@pytest.mark.parametrize(
    "sentence, celex, op, raw, target",
    [
        # Each wording is taken from an adopted act amending the Directive named.
        ("Article 97 of Directive 2006/112/EC is replaced by the following:",
         "32006L0112", "replace", "Article 97 is replaced by the following:",
         "Article 97"),
        ("In Article 12 of Directive 2003/87/EC, the following paragraphs are added:",
         "32003L0087", "append",
         "In Article 12, the following paragraphs are added:", "paragraphs"),
        ("The following Article is inserted in Directive 2006/112/EC:",
         "32006L0112", "insert", "The following Article is inserted:", "Article"),
        ("In Directive 2006/112/EC, the following Articles are inserted:",
         "32006L0112", "insert", "the following Articles are inserted:", "Articles"),
    ],
)
def test_an_article_that_is_one_instruction_naming_the_act(sentence, celex, op, raw, target):
    (block,) = _blocks("Article 1", sentence, "‘Article 129a", "Text of the provision.’")
    (instruction,) = block.instructions

    assert block.celex == celex
    assert instruction.op.value == op
    assert instruction.raw == raw
    assert instruction.target_text == target
    # Whatever follows is the payload, verbatim, not part of the instruction.
    assert "Text of the provision." in instruction.payload


def test_a_heading_between_the_article_and_its_instruction_is_allowed():
    found = detect_targets(_operative(
        "Article 1", "Amendment to Regulation (EC) No 80/2009",
        "Article 12 of Regulation (EC) No 80/2009 is replaced by the following:"))
    assert [t.celex for t in found.targets] == ["32009R0080"]
    assert found.targets[0].inline


def test_each_inline_article_gets_its_own_block():
    blocks = _blocks(
        "Article 14", "Amendment to Regulation (EU) 2017/745",
        "In Article 106 of Regulation (EU) 2017/745, paragraph 14 is replaced by the "
        "following:", "‘14. New text.’",
        "Article 15", "Amendment to Regulation (EU) 2022/123",
        "Article 3 of Regulation (EU) 2022/123 is deleted.")
    assert [b.celex for b in blocks] == ["32017R0745", "32022R0123"]
    assert "New text." in blocks[0].instructions[0].payload
    assert blocks[1].instructions[0].op.value == "delete"


def test_a_sentence_in_the_middle_of_a_payload_is_not_an_inline_instruction():
    found = detect_targets(_operative(
        "Article 1", "Directive 2003/87/EC is amended as follows:",
        "(1) Article 3 is replaced by the following:",
        "‘Article 3", "Article 5 of Directive 2008/98/EC is replaced by the following:’"))
    assert [t.celex for t in found.targets] == ["32003L0087"]
    assert not found.targets[0].inline


def test_an_inline_instruction_naming_an_annex_is_left_to_the_annex_report():
    found = detect_targets(_operative(
        "Article 1", "Annex I to Directive 2003/87/EC is replaced by the following:"))
    assert found.targets == []
    assert found.annexes == ["Directive 2003/87/EC"]


# ------------------------------------------- one act amended in more than one article


def test_an_act_amended_in_two_articles_is_one_block():
    """Each block used to write its own document, the second over the first."""
    blocks = _blocks(
        "Article 1", "Directive 2003/87/EC is amended as follows:",
        "(1) Article 5 is deleted;", "(2) Article 6 is deleted.",
        "Article 2", "Directive 2008/98/EC is amended as follows:",
        "(1) Article 3 is deleted.",
        "Article 3", "Directive 2003/87/EC is amended as follows:",
        "(1) Article 9 is deleted.",
        "Article 4", "This Directive shall enter into force on the twentieth day.")
    assert [b.celex for b in blocks] == ["32003L0087", "32008L0098"]
    first = blocks[0]
    assert [i.id for i in first.walk()] == ["1.1", "1.2", "3.1"]
    assert [i.target_text for i in first.walk()] == ["Article 5", "Article 6", "Article 9"]


def test_blocks_for_one_act_with_different_scopes_stay_apart():
    blocks = _blocks(
        "Article 1", "Directive 2003/87/EC is amended as follows:",
        "(1) Article 5 is deleted.",
        "Article 2", "Article 3 of Directive 2003/87/EC is amended as follows:",
        "(1) paragraph 2 is deleted.")
    assert [b.root_scope for b in blocks] == [None, "3"]


# --------------------------------------- what the proposal itself enacts, listed not lost


def _own(*texts):
    from a2t.parse.segment import own_provisions

    operative = _operative(*texts)
    return own_provisions(operative, detect_targets(operative).targets)


def test_the_proposals_own_articles_are_listed():
    assert _own(
        "Article 1", "Directive 2003/87/EC is amended as follows:",
        "(1) Article 5 is deleted.",
        "Article 2", "Transposition", "1. Member States shall adopt the measures.",
        "Article 3", "Entry into force", "This Directive shall enter into force.",
    ) == ["Article 2 — Transposition", "Article 3 — Entry into force"]


def test_nothing_is_listed_when_every_article_amends():
    assert _own("Article 1", "Directive 2003/87/EC is amended as follows:",
                "(1) Article 5 is deleted.") == []


def test_the_report_lists_the_proposals_own_provisions():
    from pathlib import Path

    from a2t.emit.report import RunReport

    text = RunReport(source=Path("p.pdf"), proposal_date="2026-07-17",
                     own_provisions=["Article 2 — Transposition"]).render()
    assert "## The proposal's own provisions" in text
    assert "- Article 2 — Transposition" in text
    assert "amend no act" in text
