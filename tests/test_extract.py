"""Phase 1 acceptance tests, against the real COM(2026) 616 final PDF."""

import re
from pathlib import Path

import pytest

from a2t.extract.deboilerplate import (
    PAGE_FOOTER,
    PAYLOAD_RULE,
    find_operative_part,
    operative_part,
    strip_furniture,
)
from a2t.extract.pdf import Line, extract_lines, to_lines

PDF = Path(__file__).resolve().parents[1] / "amendment_proposal_ets.pdf"

pytestmark = pytest.mark.skipif(not PDF.is_file(), reason="sample PDF not present")


@pytest.fixture(scope="module")
def lines():
    return extract_lines(PDF)


@pytest.fixture(scope="module")
def cleaned(lines):
    return operative_part(lines)


# --------------------------------------------------------------------- extraction


def test_page_tracking_uses_form_feeds():
    text = "a\nb\n\fc\nd"
    got = to_lines(text)
    assert [(ln.text, ln.page) for ln in got] == [
        ("a", 1), ("b", 1), ("c", 2), ("d", 2)
    ]


def test_line_numbers_stay_aligned_with_grep():
    """splitlines() also splits on \\f, which would desynchronise line numbers."""
    text = "a\n\fb\nc"
    assert [ln.lineno for ln in to_lines(text)] == [1, 2, 3]
    assert len(text.splitlines()) == 4  # the trap we are avoiding


def test_extracts_expected_shape(lines):
    assert len(lines) == 7098
    assert lines[-1].page == 142


# ------------------------------------------------------------------ operative part


def test_operative_part_boundaries(lines):
    start, end = find_operative_part(lines)
    assert lines[start].lineno == 3130
    assert lines[end].lineno == 5563
    assert "HAVE ADOPTED THIS DIRECTIVE" in lines[start].text
    assert lines[end].stripped.startswith("Done at Brussels")


def test_operative_part_excludes_memorandum_and_financial_statement(cleaned):
    body = "\n".join(ln.text for ln in cleaned.lines)
    assert "EXPLANATORY MEMORANDUM" not in body
    assert "LEGISLATIVE FINANCIAL STATEMENT" not in body
    assert "Estimated impact on revenue" not in body
    # but it does contain the real payload
    assert "Directive 2003/87/EC is amended as follows:" in body
    assert "Article 1 of Decision (EU) 2015/1814 is amended as follows:" in body


def test_missing_enacting_formula_raises():
    with pytest.raises(ValueError, match="enacting formula"):
        find_operative_part([Line("nothing here", 1, 1)])


def test_missing_closing_formula_raises():
    with pytest.raises(ValueError, match="closing formula"):
        find_operative_part([Line("HAVE ADOPTED THIS DIRECTIVE:", 1, 1)])


# ------------------------------------------------------------------- deboilerplate


def test_no_page_furniture_remains(cleaned):
    assert not [ln for ln in cleaned.lines if PAGE_FOOTER.fullmatch(ln.text)]


def test_removes_every_page_footer(cleaned):
    assert cleaned.reasons()["page footer"] == 49


def test_payload_footnotes_survive(cleaned):
    """Footnotes inside quoted replacement text are part of the amendment."""
    kept = [ln for ln in cleaned.lines if PAYLOAD_RULE.match(ln.text)]
    assert len(kept) == 11

    body = "\n".join(ln.text for ln in cleaned.lines)
    # the footnote body attached to the new Article 3, point (e)
    assert "Regulation (EU) 2024/1735 of the European Parliament" in body


def test_document_footnote_block_removed(cleaned):
    dropped = cleaned.removed_for("document footnote")
    assert [ln.lineno for ln in dropped] == list(range(3154, 3163))
    assert dropped[0].stripped == "34"


def test_document_footnotes_remain_available_for_celex_resolution(cleaned):
    """The ELI URIs identifying amended acts live in footnotes, not operative text."""
    footnotes = "\n".join(ln.text for ln in cleaned.removed_for("document footnote"))
    assert "data.europa.eu/eli/dec/2015/1814" in footnotes


def test_payload_rule_page_is_left_alone():
    """A page carrying a payload rule must not have its markers stripped."""
    page = [
        Line("     ‘(e) 'installation' means either of the following:", 1, 1),
        Line("     (i) a stationary technical unit;", 1, 2),
        Line("     ___________", 1, 3),
        Line("     1", 1, 4),
        Line("            Regulation (EU) 2024/1735 ...", 1, 5),
    ]
    result = strip_furniture(page)
    assert result.removed == []
    assert len(result.lines) == 5


def test_provenance_is_preserved(cleaned):
    """Every surviving line still points back into the source PDF."""
    for ln in cleaned.lines:
        assert ln.lineno >= 3130
        assert ln.page >= 58
    assert all(
        a.lineno < b.lineno for a, b in zip(cleaned.lines, cleaned.lines[1:])
    )


def test_blank_runs_collapsed(cleaned):
    assert not any(
        a.blank and b.blank for a, b in zip(cleaned.lines, cleaned.lines[1:])
    )


# --------------------------------------------------------------- corpus invariants


def test_all_amendment_anchors_survive(cleaned):
    """The instruction anchors Phase 4 depends on must all still be present."""
    body = "\n".join(ln.text for ln in cleaned.lines)
    assert len(re.findall(r"is amended as follows:", body)) == 25
    assert "the following Article 3gaa is inserted:" in body
    assert "point (p) is deleted;" in body
    assert "in the title of Chapter III the word ‘stationary’ is deleted;" in body
