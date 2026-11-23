"""Phase 4: instruction segmentation and rule-based classification.

Asserted against COM(2026) 616 final. Every operation in PLAN.md §3.4 and every source
defect in §3.5 has a case here.
"""

import re
from pathlib import Path

import pytest

from a2t.detect.targets import detect_targets
from a2t.extract.deboilerplate import operative_part
from a2t.extract.pdf import Line, extract_lines
from a2t.model.instruction import Instruction, Op
from a2t.parse.normalise import normalise, unify_quotes
from a2t.parse.rules import classify, classify_all
from a2t.parse.segment import (
    _alpha_successor,
    _roman_successor,
    marker_kind,
    segment,
)

PDF = Path(__file__).resolve().parents[1] / "amendment_proposal_ets.pdf"

# Skipping lives on the fixtures that read the sample, not on the module: the tests
# below that build their own input need no sample and must run on a fresh clone.
MISSING = "sample PDF not present"


@pytest.fixture(scope="module")
def parsed():
    if not PDF.is_file():
        pytest.skip(MISSING)
    lines = extract_lines(PDF)
    operative = operative_part(lines).lines
    blocks = segment(operative, detect_targets(operative, lines).targets)
    classify_all(blocks)
    return blocks, lines


@pytest.fixture(scope="module")
def by_id(parsed):
    blocks, _ = parsed
    return {i.id: i for block in blocks for i in block.walk()}


def make(raw: str) -> Instruction:
    return classify(Instruction(id="t", marker="1", level=1, raw=raw, source_line=1))


# ------------------------------------------------------------------ segmentation


def test_top_level_instruction_counts(parsed):
    """Article 1 carries 35 numbered points, Article 2 carries 3."""
    blocks, _ = parsed
    assert [len(b.instructions) for b in blocks] == [35, 3]


def test_top_level_markers_are_contiguous(parsed):
    blocks, _ = parsed
    assert [i.marker for i in blocks[0].instructions] == [str(n) for n in range(1, 36)]
    assert [i.marker for i in blocks[1].instructions] == ["1", "2", "3"]


def test_everything_is_classified(by_id):
    unknown = [i for i in by_id.values() if i.op is Op.UNKNOWN]
    assert unknown == []


def test_nesting_depth_and_ids(by_id):
    assert by_id["1.15.b.i"].level == 3
    assert by_id["1.15.b"].level == 2
    assert by_id["1.15"].level == 1
    assert by_id["1.15.b.i"] in by_id["1.15.b"].children


def test_markers_inside_payloads_are_not_instructions(by_id):
    """(2)(a)'s replacement text opens with '(i) a stationary technical unit ...'."""
    assert "1.2.a.i" not in by_id
    assert by_id["1.2.a"].payload.startswith("(e)")
    assert "(i) a stationary technical unit" in by_id["1.2.a"].payload


def test_wrapped_payload_line_is_not_an_instruction(by_id):
    """A payload line beginning '(b) resulting from ...' must not borrow the
    operation verb from the real instruction below it (source line 5334)."""
    assert by_id["1.31.b"].raw.startswith("paragraph 2 is replaced")
    assert by_id["1.31.b"].source_line == 5335


def test_block_boundary_excludes_next_target_heading(by_id):
    """The 'Article 2 / Amendments to ...' heading sits above the next anchor."""
    assert by_id["1.35"].raw == (
        "Annexes I to V are amended as set out in the Annexes to this Directive;"
    )


@pytest.mark.parametrize(
    "marker, kind",
    [("1", "num"), ("35", "num"), ("a", "alpha"), ("ab", "alpha"),
     ("ii", "roman"), ("iv", "roman"), ("i", "ambiguous")],
)
def test_marker_kinds(marker, kind):
    assert marker_kind(marker) == kind


@pytest.mark.parametrize(
    "marker, nxt", [("a", "b"), ("h", "i"), ("z", "aa"), ("aa", "ab")]
)
def test_alpha_successor(marker, nxt):
    assert _alpha_successor(marker) == nxt


def test_roman_successor():
    assert _roman_successor("i") == "ii"
    assert _roman_successor("iii") == "iv"


# -------------------------------------------------------------------- operations


def test_every_documented_operation_occurs(by_id):
    seen = {i.op for i in by_id.values()}
    for op in [Op.CONTAINER, Op.REPLACE_NODE, Op.DELETE_NODE, Op.INSERT_NODE,
               Op.APPEND_NODE, Op.REPLACE_TEXT, Op.DELETE_TEXT, Op.EXTERNAL]:
        assert op in seen, op


@pytest.mark.parametrize(
    "ident, op, target",
    [
        ("1.2", Op.CONTAINER, "Article 3"),
        ("1.3.a", Op.REPLACE_NODE, "paragraph 6"),
        ("1.2.d", Op.DELETE_NODE, "point (p)"),
        ("1.5", Op.INSERT_NODE, "Article 3gaa"),
        ("1.15.b.i", Op.REPLACE_NODE, "the second subparagraph"),
    ],
)
def test_real_instruction_classification(by_id, ident, op, target):
    instruction = by_id[ident]
    assert instruction.op is op
    assert instruction.target_text == target


def test_inline_token_replacement(by_id):
    """Art. 1(8): in Article 4, 'Article 27' is replaced by 'Articles 27 and 27a'."""
    instruction = by_id["1.8"]
    assert instruction.op is Op.REPLACE_TEXT
    assert instruction.scope_text == "Article 4"
    assert instruction.find_text == "Article 27"
    assert instruction.replace_text == "Articles 27 and 27a"
    assert instruction.payload is None


def test_phrase_deletion(by_id):
    """Art. 1(2)(f): in point (ag), the words '...' are deleted."""
    instruction = by_id["1.2.f"]
    assert instruction.op is Op.DELETE_TEXT
    assert instruction.scope_text == "point (ag)"
    assert instruction.find_text == "for the purposes of Chapter IVa of this Directive"


def test_chapter_title_edit(by_id):
    """Art. 1(10): in the title of Chapter III the word 'stationary' is deleted."""
    instruction = by_id["1.10"]
    assert instruction.op is Op.DELETE_TEXT
    assert instruction.find_text == "stationary"
    assert "title of Chapter III" in (instruction.scope_text or "")


def test_annex_amendment_marked_external(by_id):
    assert by_id["1.35"].op is Op.EXTERNAL
    assert "out of scope" in " ".join(by_id["1.35"].notes)


def test_positional_append_records_anchor(by_id):
    """'the following subparagraph is added after the second subparagraph'."""
    instruction = by_id["1.15.b.ii"]
    assert instruction.op is Op.APPEND_NODE
    assert any("after the second subparagraph" in n for n in instruction.notes)


def test_a_sentence_added_to_a_provision_takes_it_as_the_position():
    """'added to point (b)' names the provision the sentence joins, at its end."""
    instruction = make("the following sentence is added to point (b):")
    assert instruction.adds_a_sentence
    assert instruction.anchor_where == "after"
    assert instruction.anchor_text == "point (b)"


def test_provisions_added_to_a_provision_do_not():
    """'the following points are added to paragraph 3' says where they live, which is
    a different question, so it is left to the ordinary reading."""
    instruction = make("the following points are added to paragraph 3:")
    assert not instruction.adds_a_sentence
    assert instruction.anchor_where is None
    assert any("insert to paragraph 3" in n for n in instruction.notes)


# ---------------------------------------------------------------- source defects


def test_stray_preposition_repaired_in_both_places(by_id):
    """§3.5: 'in paragraph 2 is amended as follows' (L4142) and 'in Article 30 ...'
    (L5406) are both ungrammatical in the source."""
    for ident, target in [("1.15.c", "paragraph 2"), ("1.33", "Article 30")]:
        instruction = by_id[ident]
        assert instruction.op is Op.CONTAINER
        assert instruction.target_text == target
        assert any("malformed" in n for n in instruction.notes)


def test_container_terminated_with_semicolon(by_id):
    """Art. 1(33) ends 'is amended as follows;' — a ';' where ':' belongs."""
    assert by_id["1.33"].op is Op.CONTAINER


def test_double_quoted_date_replacement(by_id):
    """Art. 1(4)(c)(i) uses “ ” while every other instruction uses ‘ ’."""
    instruction = by_id["1.4.c.i"]
    assert instruction.op is Op.REPLACE_TEXT
    assert instruction.find_text == "31 December 2030"
    assert instruction.replace_text == "31 December 2038"


def test_list_valued_scope_survives(by_id):
    """Art. 1(22)(b) scopes to four paragraphs at once, comma-separated."""
    instruction = by_id["1.22.b"]
    assert instruction.op is Op.REPLACE_TEXT
    assert instruction.scope_text == "paragraphs 3-e, 3-d, 3-c, and 3-b"
    assert instruction.find_text == "31 December 2030"


def test_redundant_rescope_is_preserved_for_phase_5(by_id):
    """Art. 1(12)(b) says 'in Article 9, ...' while already inside Article 9."""
    instruction = by_id["1.12.b"]
    assert instruction.scope_text == "Article 9"
    assert instruction.op is Op.APPEND_NODE


def test_editorial_note_stripped():
    """§3.5: '[N.B.: ...]' asides appear in the operative text."""
    instruction = make(
        "paragraph 3 [N.B.: date may need to be changed] is replaced by the following:"
    )
    assert "N.B." not in instruction.raw
    assert any("N.B" in n for n in instruction.notes)


def test_unify_quotes_maps_all_families():
    assert unify_quotes('“x” and "y"') == "‘x’ and ‘y‘"


def test_normalise_collapses_whitespace():
    instruction = Instruction(id="t", marker="1", level=1,
                              raw="point   (p)    is  deleted;", source_line=1)
    normalise(instruction)
    assert instruction.raw == "point (p) is deleted;"


# ------------------------------------------------------------------- payloads


def test_payload_is_captured_for_replacements(by_id):
    instruction = by_id["1.15.b.i"]
    assert instruction.payload.startswith("By way of derogation from the first")
    assert instruction.payload_lines == (4112, 4123)


def test_payload_absent_for_self_contained_instructions(by_id):
    for ident in ["1.2.d", "1.8", "1.10", "1.2.f"]:
        assert by_id[ident].payload is None, ident


def test_payload_words_come_verbatim_from_the_source(parsed, by_id):
    """The payload must be the source's words, never the parser's."""
    _, lines = parsed
    instruction = by_id["1.15.b.i"]
    start, end = instruction.payload_lines
    source = " ".join(
        ln.stripped for ln in lines if start <= ln.lineno <= end
    )
    normalised = re.sub(r"\s+", " ", source).strip()
    # The payload is the source verbatim, minus only the enclosing quotes and the
    # instruction's own terminator — the sentence's final "." is part of the text.
    assert instruction.payload in normalised
    assert instruction.payload.endswith("shall apply.")

    stripped = normalised.strip("‘").rstrip(";").rstrip("’")
    assert instruction.payload.split() == stripped.split()


def test_payload_footnotes_split_off(by_id):
    """A '___________' rule inside a payload introduces its footnotes."""
    instruction = by_id["1.2.a"]
    assert instruction.footnotes
    assert "Regulation (EU) 2024/1735" in instruction.footnotes[0]
    assert "___________" not in instruction.payload


def test_every_payload_marker_has_a_footnote(by_id):
    """A marker in the text and no note behind it prints an asterisk pointing at air."""
    for instruction in by_id.values():
        markers = re.findall(r"\*+", instruction.payload or "")
        assert len(markers) == len(instruction.footnotes), instruction.id


def test_a_second_payload_footnote_is_its_own_note(by_id):
    """Art. 1(3)(a) carries "*" and "**"; joined, the two read as one source."""
    instruction = by_id["1.3.a"]
    assert len(instruction.footnotes) == 2
    assert instruction.footnotes[0].startswith("*")
    assert "Regulation (EU) 2023/2405" in instruction.footnotes[0]
    assert instruction.footnotes[1].startswith("**")
    assert "Directive (EU) 2018/2001" in instruction.footnotes[1]


def test_footnotes_reusing_one_marker_stay_separate(by_id):
    """Art. 1(2)(g) marks both of its notes "*" — only the line each opens tells them
    apart."""
    instruction = by_id["1.2.g"]
    assert len(instruction.footnotes) == 2
    assert "Directive 2008/98/EC" in instruction.footnotes[0]
    assert "Directive (EU) 2024/1788" in instruction.footnotes[1]


def test_a_payload_is_not_cut_above_its_own_footnotes(by_id):
    """Art. 1(2)(g) closes its quote, then rules off and cites its sources.

    Cutting at that first close took the whole footnote block with it.
    """
    instruction = by_id["1.2.g"]
    assert instruction.footnotes
    assert "‘offshore worksite’" in instruction.payload  # the text still ends there
    # The notes are the payload's, not the legislation's.
    assert "repealing certain Directives" not in instruction.payload
    assert "repealing certain Directives" in instruction.footnotes[0]


def test_a_note_between_two_inserted_articles_ends_at_the_next_one(by_id):
    """Art. 1(17) inserts Articles 10cb to 10ce and cites a source in 10cb(6).

    The note is printed at the foot of 10cb, so its rule falls in the middle of the
    payload. Reading everything below that rule as the note swallowed Articles 10cc,
    10cd and 10ce into a single 14 000-character footnote.
    """
    instruction = by_id["1.17"]
    assert len(instruction.footnotes) == 1
    assert instruction.footnotes[0].endswith("innovation activities.]")
    assert "Industrial Decarbonisation Bank" not in instruction.footnotes[0]
    for article in ("Article 10cb", "Article 10cc", "Article 10cd", "Article 10ce"):
        assert article in instruction.payload
    assert instruction.payload.endswith("Regulation (EU, Euratom) 2024/2509.")


def test_payload_opened_with_an_apostrophe_is_still_quoted(by_id):
    """Art. 1(7)(c) and (d) open their payload with ' and close it with ’."""
    for ident in ["1.7.c", "1.7.d"]:
        assert by_id[ident].payload.startswith("No later than"), ident


def test_payload_runs_past_a_quote_that_closes_too_early(by_id):
    """Art. 1(7)(e): "the following paragraph 6 and 7 are added:".

    The quote closes after paragraph 6; paragraph 7 stands below it unquoted and
    carries the close that was meant to come after both. Cutting at the first close
    would drop paragraph 7 — 88 words of new law — without a trace.
    """
    instruction = by_id["1.7.e"]
    assert instruction.payload.startswith("6. By 1 January 2032,")
    assert "7. No later than 30 September 2033," in instruction.payload
    assert instruction.payload.endswith("submit a legislative proposal to that effect.")
    # The close it ran past is gone, not left standing mid-payload.
    assert "’" not in instruction.payload
    assert any("that close are read as part of the payload" in note
               for note in instruction.notes)


def test_payload_stops_at_the_close_that_does_end_it(by_id):
    """The repair above must not let a payload run on into what follows it."""
    assert by_id["1.7.a"].payload.endswith("including double payment.")
    assert by_id["1.9"].payload.endswith("successively granted for the first time "
                                         "and withdrawn.")


def test_inserted_article_payload_is_whole_article(by_id):
    instruction = by_id["1.5"]
    assert instruction.op is Op.INSERT_NODE
    assert instruction.payload.startswith("Article 3gaa")
    assert len(instruction.payload) > 5000


# ------------------------------------------------------------- synthetic grammar


@pytest.mark.parametrize(
    "raw, op",
    [
        ("Article 3 is amended as follows:", Op.CONTAINER),
        ("paragraph 6 is replaced by the following:", Op.REPLACE_NODE),
        ("point (p) is deleted;", Op.DELETE_NODE),
        ("the following Article 3gaa is inserted:", Op.INSERT_NODE),
        ("the following points are added:", Op.APPEND_NODE),
        ("the penultimate sentence is replaced by the following:", Op.REPLACE_NODE),
        ("‘Article 27’ is replaced by ‘Articles 27 and 27a’.", Op.REPLACE_TEXT),
        ("the words ‘for the purposes’ are deleted;", Op.DELETE_TEXT),
    ],
)
def test_grammar_table(raw, op):
    assert make(raw).op is op


def test_unmatched_instruction_is_flagged_not_guessed():
    instruction = make("something entirely unlike an amendment")
    assert instruction.op is Op.UNKNOWN
    assert instruction.confidence == 0.0
    assert instruction.notes


@pytest.mark.parametrize(
    "raw, scope, find",
    [
        # Quoted words that contain commas must not end the scope early. Wordings from
        # adopted acts amending Regulation 2019/631 and Directive 2014/65.
        ("in point (a), the reference to ‘Annex II to Directive 2007/46/EC’ is "
         "replaced by ‘Article 4(1)’;", "point (a)", "Annex II to Directive 2007/46/EC"),
        ("in Article 93(1), the date ‘3 July 2016’ is replaced by ‘3 July 2017’;",
         "Article 93(1)", "3 July 2016"),
        ("in paragraph 2, the reference to ‘Article 7(8), Article 10(8), the fourth "
         "subparagraph of Article 11(1)’ is replaced by ‘Article 7(8)’;",
         "paragraph 2", "Article 7(8), Article 10(8), the fourth subparagraph of "
                        "Article 11(1)"),
        # A list-valued scope with no quotes still reaches its last comma.
        ("in paragraphs 3-e, 3-d, 3-c, and 3-b, the date ‘2030’ is replaced by ‘2035’;",
         "paragraphs 3-e, 3-d, 3-c, and 3-b", "2030"),
    ],
)
def test_a_scope_stops_before_the_words_it_quotes(raw, scope, find):
    instruction = make(raw)
    assert instruction.scope_text == scope
    assert instruction.find_text == find
