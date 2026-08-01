"""Phase 7: building the change set and the word-level diff."""

from pathlib import Path as FsPath

import pytest

from a2t.apply.changeset import (
    Change,
    ChangeKind,
    _substitute,
    build_changes,
    build_changeset,
)
from a2t.apply.diff import Span, align, diff, summarise
from a2t.detect.targets import detect_targets
from a2t.extract.deboilerplate import operative_part
from a2t.extract.pdf import extract_lines
from a2t.model.assemble import assemble
from a2t.model.instruction import Instruction, Op
from a2t.model.payload import Provision, from_node, structure
from a2t.model.tree import Act, Kind, Node
from a2t.parse.rules import classify_all
from a2t.parse.segment import segment
from a2t.resolve.resolver import Resolution, resolve_block

ROOT = FsPath(__file__).resolve().parents[1]
PDF = ROOT / "amendment_proposal_ets.pdf"
CACHE = {
    "32003L0087": ROOT / "out" / "cache" / "02003L0087-20240301",
    "32015D1814": ROOT / "out" / "cache" / "02015D1814-20240101",
}


# ------------------------------------------------------------------- diff engine


def test_identical_text_is_unchanged():
    edit = diff("the same words", "the same words")
    assert edit.mode == "unchanged"
    assert edit.changed_spans == []


def test_single_word_change_stays_word_level():
    """The acceptance criterion: one date changing must not rewrite the paragraph."""
    old = ("Until 31 December 2030, a share of allowances shall be attributed to "
           "Member States with a ratio of shipping companies that would have been "
           "under their responsibility.")
    new = old.replace("2030", "2038")
    edit = diff(old, new)

    assert edit.mode == "word"
    assert edit.deleted_text.strip() == "2030,"
    assert edit.inserted_text.strip() == "2038,"
    assert len(edit.changed_spans) == 2


def test_dissimilar_texts_fall_back_to_block():
    edit = diff("The quantity of allowances shall decrease annually.",
                "Aircraft operators shall surrender units for offsetting purposes.")
    assert edit.mode == "block"
    assert [s.op for s in edit.spans] == ["delete", "insert"]


def test_empty_old_is_a_pure_insertion():
    edit = diff("", "a wholly new provision")
    assert edit.deleted_text == ""
    assert edit.inserted_text == "a wholly new provision"


def test_empty_new_is_a_pure_deletion():
    edit = diff("a repealed provision", "")
    assert edit.inserted_text == ""
    assert edit.deleted_text == "a repealed provision"


def test_spans_reconstruct_both_texts_exactly():
    """Nothing may be invented or lost in the diff."""
    old = "Member States shall ensure that operators surrender allowances annually."
    new = "Member States shall ensure that aircraft operators surrender allowances."
    edit = diff(old, new)
    assert edit.old_text == old
    assert edit.new_text == new


def test_threshold_is_configurable():
    old, new = "alpha beta gamma delta", "alpha beta gamma epsilon"
    assert diff(old, new, block_threshold=0.0).mode == "word"
    assert diff(old, new, block_threshold=1.0).mode == "block"


def test_summarise_reports_word_counts():
    assert "-1 +1 words" in summarise(diff("a b c", "a d c"))


# ---------------------------------------------------------------- block alignment
#
# A provision replaced in full is not one edit but many: some of its paragraphs
# survive with a word changed, some are rewritten, some go, some are new.


def blocks(*specs) -> list[Provision]:
    return [Provision(kind, key, text) for kind, key, text in specs]


def test_surviving_blocks_are_matched_and_diffed_word_by_word():
    old = blocks((Kind.PARAGRAPH, "1", "Reports are due by 31 December 2030."),
                 (Kind.PARAGRAPH, "2", "The Commission shall publish them."))
    new = blocks((Kind.PARAGRAPH, "1", "Reports are due by 31 December 2035."),
                 (Kind.PARAGRAPH, "2", "The Commission shall publish them."))
    pairs = align(old, new)

    assert len(pairs) == 2
    assert all(p.old is not None and p.new is not None for p in pairs)
    assert pairs[0].edit.mode == "word"
    assert pairs[0].edit.deleted_text.strip() == "2030."
    assert pairs[1].edit.mode == "unchanged"


def test_a_block_matching_nothing_stands_alone():
    old = blocks((Kind.PARAGRAPH, "1", "The old rule on reporting."))
    new = blocks((Kind.PARAGRAPH, "1", "The old rule on reporting."),
                 (Kind.PARAGRAPH, "2", "An entirely unrelated new obligation."))
    pairs = align(old, new)

    assert [(p.old is None, p.new is None) for p in pairs] == [(False, False),
                                                               (True, False)]
    assert pairs[1].edit.spans == [Span("insert", "2. An entirely unrelated new "
                                                 "obligation.")]


def test_a_renumbered_block_still_matches_its_predecessor():
    """A provision keeps its number across a rewrite more reliably than its words,
    so paragraph 3 pairs with paragraph 3 however little of it survives."""
    old = blocks((Kind.PARAGRAPH, "3", "Member States shall notify the Commission."))
    new = blocks((Kind.PARAGRAPH, "3", "Operators shall keep records for ten years."))
    pairs = align(old, new)

    assert len(pairs) == 1
    assert pairs[0].old is not None and pairs[0].new is not None


def test_alignment_keeps_document_order():
    old = blocks((Kind.PARAGRAPH, "1", "First."), (Kind.PARAGRAPH, "2", "Second."),
                 (Kind.PARAGRAPH, "3", "Third."))
    new = blocks((Kind.PARAGRAPH, "1", "First."), (Kind.PARAGRAPH, "3", "Third."))
    pairs = align(old, new)
    assert [p.block.key for p in pairs] == ["1", "2", "3"]
    assert pairs[1].new is None


def test_markers_are_part_of_what_is_diffed():
    """Renumbering has to show as a revision, not happen silently between two
    paragraphs that read as unchanged."""
    old = blocks((Kind.POINT, "a", "the same words exactly."))
    new = blocks((Kind.POINT, "b", "the same words exactly."))
    pairs = align(old, new)
    assert pairs[0].edit.deleted_text.strip() == "(a)"
    assert pairs[0].edit.inserted_text.strip() == "(b)"


def test_alignment_loses_no_text():
    old = blocks((Kind.PARAGRAPH, "1", "One."), (Kind.SUBPARAGRAPH, None, "Two."))
    new = blocks((Kind.PARAGRAPH, "1", "One, amended."),
                 (Kind.POINT, "a", "Three."))
    pairs = align(old, new)
    assert [p.edit.old_text for p in pairs if p.old] == ["1. One.", "Two."]
    assert [p.edit.new_text for p in pairs if p.new] == ["1. One, amended.",
                                                         "(a) Three."]


def test_single_block_replacement_is_not_aligned():
    """One paragraph against one paragraph has nothing to align, and the
    whole-provision diff already reads correctly."""
    act = make_act()
    node = act.article("3").by_key(Kind.POINT, "h")
    instr = instruction(Op.REPLACE_NODE, payload="‘new entrant’ means a new thing.")
    change = build_changes(Resolution(instr, node=node, status="resolved"))[0]
    assert change.blocks == []


def test_replacement_matching_nothing_keeps_the_whole_diff():
    """A provision whose subparagraphs the act records but whose replacement text
    the PDF gives as one undivided run: no old block resembles the single new one
    closely enough to pair with it, yet the two sides taken whole differ by a word.

    Aligning that would print the provision as a wholesale deletion followed by a
    wholesale re-insertion, burying the one word that changed. The whole-provision
    diff the change set already holds is the better reading, so it is kept."""
    parts = [f"Subparagraph {n} of this paragraph imposes its own obligation."
             for n in range(1, 8)]
    act = Act(celex="TEST", title="t")
    paragraph = act.root.add(Node(Kind.ARTICLE, key="9")).add(
        Node(Kind.PARAGRAPH, key="1")
    )
    for text in parts:
        paragraph.add(Node(Kind.SUBPARAGRAPH, text=text))

    instr = instruction(Op.REPLACE_NODE,
                        payload=" ".join(parts).replace("obligation", "duty", 1))
    change = build_changes(Resolution(instr, node=paragraph, status="resolved"))[0]

    assert change.edit.mode == "word"          # one word apart, taken whole
    assert align(from_node(paragraph), structure(change.new_text)) != []
    assert all(p.old is None or p.new is None  # …but nothing pairs block to block
               for p in align(from_node(paragraph), structure(change.new_text)))
    assert change.blocks == []


# -------------------------------------------------------------------- substitution


def test_substitution_is_case_insensitive():
    text, count = _substitute("STATIONARY INSTALLATIONS", "stationary", "")
    assert (text, count) == ("INSTALLATIONS", 1)


def test_deletion_does_not_leave_a_double_space():
    text, _ = _substitute("‘x’ for the purposes of Chapter IVa means y",
                          "for the purposes of Chapter IVa ", "")
    assert "  " not in text
    assert text == "‘x’ means y"


def test_substitution_counts_every_occurrence():
    _, count = _substitute("2030 and 2030 again", "2030", "2035")
    assert count == 2


def test_substitution_reports_absence():
    _, count = _substitute("nothing here", "2030", "2035")
    assert count == 0


# ------------------------------------------------------------------- change kinds


def make_act() -> Act:
    act = Act(celex="TEST", title="t")
    article = act.root.add(Node(Kind.ARTICLE, key="3"))
    article.add(Node(Kind.POINT, key="h", text="‘new entrant’ means an installation."))
    article.add(Node(Kind.POINT, key="p", text="‘operator’ means any person."))
    return act


def instruction(op: Op, **kwargs) -> Instruction:
    base = dict(id="t", marker="a", level=2, raw="", source_line=1, op=op)
    base.update(kwargs)
    return Instruction(**base)


def test_replace_records_old_and_new():
    act = make_act()
    node = act.article("3").by_key(Kind.POINT, "h")
    instr = instruction(Op.REPLACE_NODE, payload="‘new entrant’ means a new thing.")
    change = build_changes(Resolution(instr, node=node, status="resolved"))[0]

    assert change.kind is ChangeKind.REPLACE
    assert change.old_text == node.text
    assert change.new_text == instr.payload


def test_delete_produces_an_empty_new_text():
    act = make_act()
    node = act.article("3").by_key(Kind.POINT, "p")
    change = build_changes(
        Resolution(instruction(Op.DELETE_NODE), node=node, status="resolved")
    )[0]
    assert change.kind is ChangeKind.DELETE
    assert change.new_text == ""
    assert change.old_text == node.text


def test_insert_attaches_to_an_anchor():
    act = make_act()
    instr = instruction(Op.INSERT_NODE, payload="Article 3gaa …")
    change = build_changes(
        Resolution(instr, node=act.root, status="resolved")
    )[0]
    assert change.kind is ChangeKind.INSERT
    assert change.anchor is act.root
    assert change.old_text == ""


def test_unresolved_instruction_is_skipped_not_guessed():
    change = build_changes(
        Resolution(instruction(Op.REPLACE_NODE), status="unresolved", reason="nope")
    )[0]
    assert change.kind is ChangeKind.SKIPPED
    assert change.note == "nope"


def test_container_is_skipped_with_a_benign_note():
    act = make_act()
    change = build_changes(
        Resolution(instruction(Op.CONTAINER), node=act.article("3"), status="resolved")
    )[0]
    assert change.kind is ChangeKind.SKIPPED
    assert "children" in change.note


def test_text_edit_absent_phrase_is_skipped():
    act = make_act()
    node = act.article("3").by_key(Kind.POINT, "h")
    instr = instruction(Op.REPLACE_TEXT, find_text="nonexistent", replace_text="x")
    change = build_changes(Resolution(instr, node=node, status="resolved"))[0]
    assert change.kind is ChangeKind.SKIPPED
    assert "not present" in change.note


def test_enumerated_resolution_yields_one_change_per_target():
    act = make_act()
    nodes = act.article("3").of_kind(Kind.POINT)
    instr = instruction(Op.DELETE_NODE)
    changes = build_changes(
        Resolution(instr, nodes=nodes, status="resolved", enumerated=True)
    )
    assert len(changes) == 2
    assert {c.node for c in changes} == set(nodes)


# --------------------------------------------------------------- the real corpus


pytestmark_corpus = pytest.mark.skipif(
    not PDF.is_file() or not all((p / "works.parquet").is_file() for p in CACHE.values()),
    reason="run `a2t retrieve amendment_proposal_ets.pdf --cache out/cache` first",
)


@pytest.fixture(scope="module")
def corpus():
    import polars as pl

    lines = extract_lines(PDF)
    operative = operative_part(lines).lines
    blocks = segment(operative, detect_targets(operative, lines).targets)
    classify_all(blocks)

    out = {}
    for block in blocks:
        works = pl.read_parquet(CACHE[block.celex] / "works.parquet").row(0, named=True)
        act = assemble(works["full_text_html"], block.celex, works["title"])
        out[block.celex] = build_changeset(resolve_block(block, act), act)
    return out


@pytest.fixture(scope="module")
def changes(corpus):
    index: dict[str, list[Change]] = {}
    for changeset in corpus.values():
        for change in changeset.renderable:
            index.setdefault(change.instruction.id, []).append(change)
    return index


@pytestmark_corpus
def test_every_instruction_becomes_changes_or_is_reported(corpus):
    for changeset in corpus.values():
        for change in changeset.changes:
            assert change.kind is not ChangeKind.SKIPPED or change.note


@pytestmark_corpus
def test_date_change_in_four_paragraphs_is_word_level(changes):
    """Art. 1(22)(b) changes one date across four paragraphs."""
    group = changes["1.22.b"]
    assert len(group) == 4
    for change in group:
        assert change.edit.mode == "word"
        assert change.edit.deleted_text.strip() == "2030"
        assert change.edit.inserted_text.strip() == "2035"
        assert change.edit.ratio > 0.95


@pytestmark_corpus
def test_chapter_title_word_deletion(changes):
    """Art. 1(10): 'stationary' leaves the Chapter III heading."""
    change = changes["1.10"][0]
    assert change.kind is ChangeKind.TITLE_EDIT
    assert change.old_text == "STATIONARY INSTALLATIONS"
    assert change.new_text == "INSTALLATIONS"


@pytestmark_corpus
def test_inline_reference_replacement(changes):
    """Art. 1(8): 'Article 27' -> 'Articles 27 and 27a'."""
    change = changes["1.8"][0]
    assert change.kind is ChangeKind.TEXT_EDIT
    assert "Articles 27 and 27a" in change.new_text
    assert change.edit.mode == "word"


@pytestmark_corpus
def test_deleted_paragraphs_become_two_deletions(changes):
    """Art. 1(15)(j): paragraphs 8 and 8a are deleted."""
    group = changes["1.15.j"]
    assert len(group) == 2
    assert all(c.kind is ChangeKind.DELETE for c in group)
    assert all(c.new_text == "" for c in group)


@pytestmark_corpus
def test_new_provisions_are_pure_insertions(corpus):
    inserts = [c for cs in corpus.values() for c in cs.renderable
               if c.kind is ChangeKind.INSERT]
    assert inserts
    assert all(c.old_text == "" and c.edit.deleted_text == "" for c in inserts)


@pytestmark_corpus
def test_replacement_text_is_the_payload_verbatim(corpus):
    """The document must never contain text the pipeline composed."""
    for changeset in corpus.values():
        for change in changeset.renderable:
            if change.kind is ChangeKind.REPLACE:
                assert change.new_text == change.instruction.payload


@pytestmark_corpus
def test_no_change_introduces_double_spacing(corpus):
    for changeset in corpus.values():
        for change in changeset.renderable:
            assert "  " not in change.new_text


@pytestmark_corpus
def test_most_replacements_stay_word_level(corpus):
    """Block mode is the exception, not the norm."""
    replaces = [c for cs in corpus.values() for c in cs.renderable
                if c.kind is ChangeKind.REPLACE]
    word = [c for c in replaces if c.edit.mode == "word"]
    assert len(word) / len(replaces) > 0.75
