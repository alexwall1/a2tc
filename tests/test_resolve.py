"""Phase 5: resolving instruction targets to nodes of the base act."""

from pathlib import Path as FsPath

import pytest

from a2t.detect.targets import detect_targets
from a2t.extract.deboilerplate import operative_part
from a2t.extract.pdf import extract_lines
from a2t.model.assemble import assemble
from a2t.model.instruction import Op
from a2t.model.path import Path, Step
from a2t.model.tree import Kind
from a2t.parse.rules import classify_all
from a2t.parse.segment import segment
from a2t.resolve.resolver import (
    PLAUSIBLE,
    _is_redundant,
    parse_enumeration,
    parse_phrase,
    resolve_block,
    targets_title,
)

from _samples import cached

ROOT = FsPath(__file__).resolve().parents[1]
PDF = ROOT / "amendment_proposal_ets.pdf"
CACHE = {
    "32003L0087": cached("02003L0087-20240301"),
    "32015D1814": cached("02015D1814-20240101"),
}

# Skipping lives on the fixtures that read the sample, not on the module: the tests
# below that build their own input need no sample and must run on a fresh clone.
MISSING = "run `a2t retrieve amendment_proposal_ets.pdf --cache out/cache` first"


@pytest.fixture(scope="module")
def reports():
    if not PDF.is_file() or not all(
            (p / "works.parquet").is_file() for p in CACHE.values()):
        pytest.skip(MISSING)
    import polars as pl

    lines = extract_lines(PDF)
    operative = operative_part(lines).lines
    blocks = segment(operative, detect_targets(operative, lines).targets)
    classify_all(blocks)

    out = {}
    for block in blocks:
        works = pl.read_parquet(CACHE[block.celex] / "works.parquet").row(0, named=True)
        act = assemble(works["full_text_html"], block.celex, works["title"])
        out[block.celex] = (block, act, resolve_block(block, act))
    return out


@pytest.fixture(scope="module")
def directive(reports):
    return reports["32003L0087"]


@pytest.fixture(scope="module")
def resolutions(reports):
    return {
        r.instruction.id: r
        for _, _, report in reports.values()
        for r in report.results
    }


# --------------------------------------------------------------- phrase parsing


@pytest.mark.parametrize(
    "phrase, steps",
    [
        ("Article 10a", [Step(Kind.ARTICLE, key="10a")]),
        ("paragraph 1a", [Step(Kind.PARAGRAPH, key="1a")]),
        ("point (e)", [Step(Kind.POINT, key="e")]),
        ("point (ag)", [Step(Kind.POINT, key="ag")]),
        ("the second subparagraph", [Step(Kind.SUBPARAGRAPH, index=2)]),
        ("the penultimate sentence", [Step(Kind.SENTENCE, index=-2)]),
        ("the fourth subparagraph", [Step(Kind.SUBPARAGRAPH, index=4)]),
        ("paragraph 1, second sentence",
         [Step(Kind.PARAGRAPH, key="1"), Step(Kind.SENTENCE, index=2)]),
        ("the title of Chapter III", [Step(Kind.CHAPTER, key="iii")]),
    ],
)
def test_parse_phrase(phrase, steps):
    assert parse_phrase(phrase) == steps


def test_unparseable_phrase_yields_nothing():
    assert parse_phrase("something unlike an address") == []
    assert parse_phrase(None) == []


def test_targets_title_detection():
    assert targets_title("the title of Chapter III")
    assert not targets_title("Chapter III")


def test_redundant_scope_detection():
    context = Path((Step(Kind.ARTICLE, key="9"),))
    assert _is_redundant(context, [Step(Kind.ARTICLE, key="9")])
    assert not _is_redundant(context, [Step(Kind.ARTICLE, key="10")])


# -------------------------------------------------------------------- coverage


def test_rule_engine_coverage(reports):
    """113 of 114 resolve by rules alone; the other one is correctly refused.

    It was previously "resolved" by falling back to the enclosing scope, which is
    the silent-mistarget failure mode — see the test below.
    """
    total = sum(len(r.applicable) for _, _, r in reports.values())
    resolved = sum(len(r.resolved) for _, _, r in reports.values())
    assert total == 114
    assert resolved == 113


def test_unreadable_target_is_refused_not_widened(resolutions):
    """An unparseable target must never fall back to its container.

    `1.33.a` reads "in paragraph 7, first subparagraph, the sole footnote is deleted".
    Falling back to the scope would delete the whole subparagraph rather than its
    footnote — a destructive edit that looks like a clean resolution. It is handed to
    the LLM tier instead.
    """
    result = resolutions["1.33.a"]
    assert not result.ok
    assert "could not be parsed" in result.reason
    assert "refusing to fall back" in result.reason


def test_a_run_addressed_by_ordinal_resolves_to_every_member(resolutions):
    """`1.14.a` reads "in paragraph 1, the first and second subparagraphs are

    replaced by the following". Both are addressed, and neither the phrase nor the
    payload names a number to divide them by.
    """
    result = resolutions["1.14.a"]
    assert result.ok and result.enumerated
    assert [n.citation.split(" / ")[-1] for n in result.targets] == [
        "subparagraph 1", "subparagraph 2",
    ]


def test_annex_amendment_is_not_applicable(resolutions):
    assert resolutions["1.35"].status == "not-applicable"


# ------------------------------------------------------------ scope accumulation


def test_nested_scope_accumulates(resolutions):
    """(15) Article 10a -> (b) paragraph 1a -> (i) the second subparagraph."""
    node = resolutions["1.15.b.i"].node
    assert node.citation.endswith("Article 10a / paragraph 1a / subparagraph 2")


def test_container_scope_applies_to_children(resolutions):
    assert resolutions["1.2.b"].node.citation.endswith("Article 3 / point (h)")


def test_scope_prefix_is_applied(resolutions):
    """Art. 1(8): 'in Article 4, ...' resolves to Article 4."""
    assert resolutions["1.8"].node.citation.endswith("Article 4")


def test_redundant_rescope_does_not_double_apply(resolutions):
    """Art. 1(12)(b) says 'in Article 9' while already inside Article 9."""
    result = resolutions["1.12.b"]
    assert result.ok
    assert result.node.citation.endswith("Article 9")
    assert any("redundant scope" in n for n in result.instruction.notes)


def test_article_step_is_absolute(resolutions):
    """An Article scope restarts from the act root rather than nesting."""
    assert resolutions["1.1"].node is not None
    assert "Article 2" in resolutions["1.1"].node.citation


# ------------------------------------------------------------------- addressing


def test_ordinal_counted_across_subtree(resolutions):
    """'in Article 2, the first subparagraph' lives under paragraph 1."""
    result = resolutions["1.1"]
    assert result.ok
    assert result.node.kind is Kind.SUBPARAGRAPH


def test_chapter_title_edit_resolves(resolutions):
    """Art. 1(10): in the title of Chapter III the word 'stationary' is deleted."""
    result = resolutions["1.10"]
    assert result.ok
    assert result.node.kind is Kind.CHAPTER
    assert result.node.title == "STATIONARY INSTALLATIONS"


def test_new_article_is_placed_by_its_number(resolutions):
    """'the following Article 3gaa is inserted' names no position, so the number it
    is given supplies one: an act is never renumbered, and 3gaa follows 3ga."""
    result = resolutions["1.5"]
    assert result.ok
    assert result.instruction.op is Op.INSERT_NODE
    assert result.node.kind is Kind.ARTICLE
    assert result.node.key == "3ga"


def test_position_naming_a_provision_the_proposal_itself_inserts(resolutions):
    """'after Article 3gaa, the following Article 3gab is inserted' — but 3gaa is
    not in the base act, the instruction before this one inserts it. So this one goes
    where that insertion goes: both land after 3ga, in the order given."""
    result = resolutions["1.6"]
    assert result.ok
    assert result.node.key == "3ga"
    assert any("this proposal inserts there" in note
               for note in result.instruction.notes)


def test_a_paragraph_follows_the_paragraphs_the_proposal_inserts(resolutions):
    """Art. 1(15)(e) adds paragraph 3f after paragraph 3e of Article 10a, which
    Art. 1(15)(d) inserts after paragraph 2. The base act stops at paragraph 2, so
    placing 3f by its container would drop it at the end of the article — after
    paragraph 22, twenty paragraphs past the run it belongs to."""
    result = resolutions["1.15.e"]
    assert result.ok
    assert result.node.citation.endswith("Article 10a / paragraph 2")
    assert result.node is resolutions["1.15.d"].node


def test_a_stated_position_is_not_second_guessed(resolutions):
    """Where the instruction says where its new article goes, the number is never
    consulted — and the reader is not told it was."""
    result = resolutions["1.22.a.ii"]
    assert result.ok
    assert not any("its number belongs" in note
                   for note in result.instruction.notes)


def test_a_new_paragraph_goes_after_the_one_it_names(resolutions):
    """'the following paragraphs are inserted after paragraph 3' used to land at the
    end of the article, past every paragraph that follows paragraph 3."""
    result = resolutions["1.22.a.ii"]
    assert result.ok
    assert result.node.kind is Kind.PARAGRAPH
    assert result.node.key == "3"


def test_text_replacement_verifies_the_phrase_exists(resolutions):
    """Art. 1(8) must actually find 'Article 27' inside Article 4."""
    result = resolutions["1.8"]
    assert result.ok
    assert "Article 27" in result.node.full_text


# ------------------------------------------------------------------ enumerations


@pytest.mark.parametrize(
    "phrase, kind, keys",
    [
        ("paragraphs 3-e, 3-d, 3-c, and 3-b", Kind.PARAGRAPH,
         ["3-e", "3-d", "3-c", "3-b"]),
        ("paragraphs 8 and 8a", Kind.PARAGRAPH, ["8", "8a"]),
        ("points (a), (b) and (c)", Kind.POINT, ["a", "b", "c"]),
    ],
)
def test_parse_enumeration(phrase, kind, keys):
    assert parse_enumeration(phrase) == (kind, [Step(kind, key=key) for key in keys])


@pytest.mark.parametrize(
    "phrase, kind, indices",
    [
        ("the first and second subparagraphs", Kind.SUBPARAGRAPH, [1, 2]),
        ("the second, third and fourth sentences", Kind.SENTENCE, [2, 3, 4]),
        ("the last two subparagraphs", None, None),  # a count, not an enumeration
        ("the following paragraphs", None, None),
    ],
)
def test_parse_ordinal_enumeration(phrase, kind, indices):
    expected = (
        None if kind is None
        else (kind, [Step(kind, index=index) for index in indices])
    )
    assert parse_enumeration(phrase) == expected


@pytest.mark.parametrize("phrase", ["paragraph 6", "Article 10a", "point (h)", None])
def test_single_targets_are_not_enumerations(phrase):
    assert parse_enumeration(phrase) is None


def test_hyphenated_paragraph_keys_exist(directive):
    """The ETS Directive numbers Article 12 paragraphs 3-a ... 3-e."""
    _, act, _ = directive
    keys = [p.key for p in act.article("12").of_kind(Kind.PARAGRAPH)]
    for key in ["3-a", "3-b", "3-c", "3-d", "3-e"]:
        assert key in keys


def test_enumerated_scope_applies_to_every_paragraph(resolutions):
    """Art. 1(22)(b) edits a date in four paragraphs at once."""
    result = resolutions["1.22.b"]
    assert result.ok and result.enumerated
    assert [n.key for n in result.targets] == ["3-e", "3-d", "3-c", "3-b"]
    # the phrase being replaced must be present in each of them
    assert all(result.instruction.find_text in n.full_text for n in result.targets)


def test_enumerated_target_deletes_every_paragraph(resolutions):
    """Art. 1(15)(j): 'paragraphs 8 and 8a are deleted'.

    Without enumeration handling the phrase fails to parse and the instruction
    collapses onto its container — deleting the whole of Article 10a.
    """
    result = resolutions["1.15.j"]
    assert result.ok and result.enumerated
    assert result.instruction.op is Op.DELETE_NODE
    assert [n.key for n in result.targets] == ["8", "8a"]
    assert all("Article 10a" in n.citation for n in result.targets)


def test_single_target_resolution_still_has_one_target(resolutions):
    result = resolutions["1.2.b"]
    assert not result.enumerated
    assert len(result.targets) == 1


# --------------------------------------------------------------- plausibility


def test_numbering_error_in_the_proposal_is_flagged(resolutions):
    """Art. 1(7)(b) says 'the second subparagraph' but supplies the third.

    The replacement restates the existing third subparagraph almost verbatim and
    refers to 'the report referred to in the second subparagraph', so it cannot be
    that subparagraph. Confirmed against the official consolidated PDF.
    """
    result = resolutions["1.7.b"]
    assert result.ok, "the instruction is still applied as written"
    assert result.needs_review
    assert result.plausibility < PLAUSIBLE
    assert "subparagraph 3" in result.doubt
    assert "applied as written" in result.doubt


def test_instruction_is_not_silently_retargeted(resolutions):
    """Applying the drafter's words is the contract; correcting them is not."""
    result = resolutions["1.7.b"]
    assert result.node.index == 2
    assert result.instruction.confidence <= 0.5


def test_plausible_replacements_are_not_flagged(resolutions):
    """A normal rewrite of the same provision must not be marked for review."""
    result = resolutions["1.2.b"]  # Article 3 point (h)
    assert result.ok and not result.needs_review
    assert result.plausibility > PLAUSIBLE


def test_review_list_is_small_and_specific(reports):
    doubtful = [r for _, _, report in reports.values() for r in report.doubtful]
    assert 0 < len(doubtful) <= 10
    assert all(r.doubt for r in doubtful)


# ------------------------------------------------------- operations nobody read


def _block_with_an_unread_instruction():
    from a2t.model.instruction import Instruction, TargetBlock
    from a2t.model.tree import Act, Node
    from a2t.parse.rules import classify_all

    act = Act(celex="TEST", title="t")
    article = act.root.add(Node(Kind.ARTICLE, key="3"))
    article.add(Node(Kind.POINT, key="h", text="‘new entrant’ means an installation."))

    container = Instruction(id="1", marker="1", level=1, source_line=1,
                            raw="Article 3 is amended as follows:")
    unread = Instruction(id="1.a", marker="a", level=2, source_line=2,
                         raw="point (h) is renumbered as point (i);")
    inner = Instruction(id="1.a.i", marker="i", level=3, source_line=3,
                        raw="point (h) is deleted;")
    unread.children.append(inner)
    container.children.append(unread)
    block = TargetBlock(celex="TEST", citation="Test act", root_scope=None,
                        instructions=[container])
    classify_all([block])
    return block, act, resolve_block(block, act)


def test_an_operation_no_rule_read_is_not_counted_as_resolved():
    """It has no target, so it used to land on the enclosing article and pass for a
    resolution — and the coverage figure with it — though nothing would be applied."""
    block, act, report = _block_with_an_unread_instruction()
    by_id = {r.instruction.id: r for r in report.results}

    assert by_id["1.a"].instruction.op is Op.UNKNOWN
    assert by_id["1.a"].status == "unresolved"
    assert by_id["1.a"].unread
    assert report.coverage < 1.0
    # Still reported at the provision it sits under, not at the end of the document.
    assert by_id["1.a"].node is act.article("3")


def test_what_an_unread_instruction_contains_is_reported_not_lost():
    _, _, report = _block_with_an_unread_instruction()
    by_id = {r.instruction.id: r for r in report.results}

    assert by_id["1.a.i"].status == "unresolved"
    assert "1.a" in by_id["1.a.i"].reason


# ------------------------------------------- scopes written the way the Journal cites


@pytest.mark.parametrize(
    "phrase, steps",
    [
        # Wordings from adopted acts amending Directives 2003/87, 2010/75, 2014/65.
        ("Article 27(3)", [Step(Kind.ARTICLE, key="27"), Step(Kind.PARAGRAPH, key="3")]),
        ("Article 4(1)", [Step(Kind.ARTICLE, key="4"), Step(Kind.PARAGRAPH, key="1")]),
        ("Article 25(4)(a)", [Step(Kind.ARTICLE, key="25"), Step(Kind.PARAGRAPH, key="4"),
                              Step(Kind.POINT, key="a")]),
        ("point (a) of Article 25(4)",
         [Step(Kind.ARTICLE, key="25"), Step(Kind.PARAGRAPH, key="4"),
          Step(Kind.POINT, key="a")]),
        ("Article 9(1)(c)(ii)",
         [Step(Kind.ARTICLE, key="9"), Step(Kind.PARAGRAPH, key="1"),
          Step(Kind.POINT, key="c"), Step(Kind.SUBPOINT, key="ii")]),
        ("Article 2(1), second sentence",
         [Step(Kind.ARTICLE, key="2"), Step(Kind.PARAGRAPH, key="1"),
          Step(Kind.SENTENCE, index=2)]),
        ("point (a) of paragraph 2", [Step(Kind.PARAGRAPH, key="2"), Step(Kind.POINT, key="a")]),
    ],
)
def test_parse_compact_and_inverted_citations(phrase, steps):
    assert parse_phrase(phrase) == steps


def test_an_inverted_phrase_with_an_unreadable_side_is_refused():
    assert parse_phrase("something of Article 3") == []


def _act_with_two_articles():
    from a2t.model.tree import Act, Node

    act = Act(celex="TEST", title="t")
    first = act.root.add(Node(Kind.ARTICLE, key="1"))
    first.add(Node(Kind.SUBPARAGRAPH, text="This Directive lays down rules."))
    first.add(Node(Kind.SUBPARAGRAPH, text="It also provides for reductions."))
    article = act.root.add(Node(Kind.ARTICLE, key="27"))
    paragraph = article.add(Node(Kind.PARAGRAPH, key="3"))
    paragraph.add(Node(Kind.SUBPARAGRAPH, text="First words."))
    paragraph.add(Node(Kind.SUBPARAGRAPH, text="Second words."))
    return act


def _resolve_one(raw: str, act):
    from a2t.model.instruction import Instruction, TargetBlock
    from a2t.parse.rules import classify_all

    instruction = Instruction(id="1.1", marker="1", level=1, source_line=1, raw=raw)
    block = TargetBlock(celex="TEST", citation="Test", root_scope=None,
                        instructions=[instruction])
    classify_all([block])
    return instruction, resolve_block(block, act).results[0]


def test_a_compact_scope_lands_on_the_article_it_names_not_on_article_1():
    """It used to resolve to Article 1's second subparagraph and say nothing."""
    instruction, result = _resolve_one(
        "In Article 27(3), the second subparagraph is replaced by the following:",
        _act_with_two_articles())
    assert result.status == "resolved"
    assert result.node.text == "Second words."


def test_a_scope_that_cannot_be_read_is_unresolved_not_inherited():
    instruction, result = _resolve_one(
        "In the arrangements for the transitional period, the second subparagraph "
        "is replaced by the following:", _act_with_two_articles())
    assert result.status == "unresolved"
    assert "could not be read" in result.reason
    assert result.node is None


def test_what_sits_inside_an_unreadable_scope_is_refused_with_it():
    from a2t.model.instruction import Instruction, TargetBlock
    from a2t.parse.rules import classify_all

    container = Instruction(id="1", marker="1", level=1, source_line=1,
                            raw="In the arrangements for the transitional period, "
                                "Article 27 is amended as follows:")
    inner = Instruction(id="1.a", marker="a", level=2, source_line=2,
                        raw="paragraph 3 is deleted;")
    container.children.append(inner)
    block = TargetBlock(celex="TEST", citation="Test", root_scope=None,
                        instructions=[container])
    classify_all([block])
    report = resolve_block(block, _act_with_two_articles())
    assert [r.status for r in report.results] == ["unresolved", "unresolved"]


# ------------------------------------------ containers and scopes naming what is not there


def _container_block(container_raw, *child_raws):
    from a2t.model.instruction import Instruction, TargetBlock
    from a2t.parse.rules import classify_all

    container = Instruction(id="1", marker="1", level=1, source_line=1, raw=container_raw)
    for n, raw in enumerate(child_raws, 1):
        container.children.append(Instruction(id=f"1.{n}", marker=str(n), level=2,
                                              source_line=1 + n, raw=raw))
    block = TargetBlock(celex="TEST", citation="Test", root_scope=None,
                        instructions=[container])
    classify_all([block])
    return block


def test_a_container_naming_something_unreadable_refuses_what_is_inside_it():
    """Its children used to be resolved against the act root."""
    block = _container_block("The schedule of fees is amended as follows:",
                             "paragraph 3 is deleted;")
    report = resolve_block(block, _act_with_two_articles())
    assert [r.status for r in report.results] == ["unresolved", "unresolved"]
    assert all(r.node is None for r in report.results)
    assert "could not be read" in report.results[0].reason


def test_a_container_for_an_annex_is_not_applicable_with_everything_in_it():
    block = _container_block("Annex IV is amended as follows:",
                             "in row 4, the figure ‘3’ is replaced by ‘5’;",
                             "row 11 is deleted;")
    report = resolve_block(block, _act_with_two_articles())
    assert [r.status for r in report.results] == ["not-applicable"] * 3
    assert report.results[0].reason == "amends an annex — out of scope"
    assert report.coverage == 1.0  # nothing applicable was left unresolved


def test_a_scope_inside_an_annex_is_not_applicable():
    instruction, result = _resolve_one(
        "in point (k) of Annex I, the second paragraph is replaced by the following:",
        _act_with_two_articles())
    assert result.status == "not-applicable"


@pytest.mark.parametrize("scope, kind", [
    ("Title VIII, Chapter 2", "title"),
    ("Chapter VI, Section 3, Subsection II", "section"),
    ("the introductory part", "introductory part"),
])
def test_a_refusal_says_which_kind_of_provision_the_model_lacks(scope, kind):
    instruction, result = _resolve_one(
        f"in {scope}, the first subparagraph is replaced by the following:",
        _act_with_two_articles())
    assert result.status == "unresolved"
    assert "provision kind the model does not have" in result.reason
    assert kind in result.reason


def test_a_container_whose_target_is_readable_still_resolves_its_children():
    block = _container_block("Article 27 is amended as follows:", "paragraph 3 is deleted;")
    report = resolve_block(block, _act_with_two_articles())
    assert [r.status for r in report.results] == ["resolved", "resolved"]


@pytest.mark.parametrize("raw, said", [
    ("in Article 27, the following paragraph is inserted before paragraph 3a:",
     "'inserted before' is not supported"),
    ("the following title shall be inserted before Article 1:",
     "no title, section or subsection"),
    ("something entirely unlike an amendment", "no rule matched"),
])
def test_an_unread_instruction_says_why(raw, said):
    instruction, result = _resolve_one(raw, _act_with_two_articles())
    assert result.status == "unresolved"
    assert said in result.reason


def test_an_annex_by_reference_leaves_the_applicable_count():
    instruction, result = _resolve_one(
        "Annex V is amended in accordance with Annex III to this Regulation.",
        _act_with_two_articles())
    assert result.status == "not-applicable"
