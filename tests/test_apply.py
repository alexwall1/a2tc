"""Phase 7: building the change set and the word-level diff."""

from pathlib import Path as FsPath

import pytest

from a2t.apply.changeset import (
    Change,
    ChangeKind,
    _opening,
    _substitute,
    build_changes,
    build_changeset,
    split_articles_payload,
    split_enumerated_payload,
    split_leading,
)
from a2t.apply.diff import Span, align, diff, summarise
from a2t.detect.targets import detect_targets
from a2t.extract.deboilerplate import operative_part
from a2t.extract.pdf import extract_lines
from a2t.model.assemble import assemble
from a2t.model.instruction import Instruction, Op
from a2t.model.payload import Provision, from_node, opening_number, structure
from a2t.model.path import Path as NodePath
from a2t.model.path import Step, resolve
from a2t.model.tree import Act, Kind, Node
from a2t.parse.rules import classify_all
from a2t.parse.segment import segment
from a2t.resolve.resolver import Resolution, resolve_block

from _samples import cached

ROOT = FsPath(__file__).resolve().parents[1]
PDF = ROOT / "amendment_proposal_ets.pdf"
CACHE = {
    "32003L0087": cached("02003L0087-20240301"),
    "32015D1814": cached("02015D1814-20240101"),
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


# ------------------------------------------------------- splitting a shared payload


def test_payload_split_between_two_paragraphs():
    """The case the splitter is for: each new paragraph opens with its own number."""
    payload = "5. The first new paragraph. 5a. The second new paragraph."
    assert split_enumerated_payload(payload, ["5", "5a"]) == [
        "5. The first new paragraph.",
        "5a. The second new paragraph.",
    ]


def test_payload_split_refused_when_a_number_is_missing():
    assert split_enumerated_payload("5. Only one paragraph here.", ["5", "5a"]) is None


def test_payload_split_refused_when_the_numbers_run_backwards():
    payload = "5a. The later one first. 5. And then the earlier."
    assert split_enumerated_payload(payload, ["5", "5a"]) is None


def test_payload_split_refused_when_text_precedes_the_first_number():
    """Text above the first marker belongs to no segment, so cutting there loses it.

    This is what "Articles 2 and 3 are replaced by the following:" looks like when the
    numbers are matched instead of the headings: "2." and "3." are found, but they are
    the new articles' *paragraphs*, and everything above them goes silently.
    """
    payload = ("Article 2 1. The first paragraph, which is lost. "
               "2. The second. 3. The third.")
    assert split_enumerated_payload(payload, ["2", "3"]) is None


# --------------------------------------------------- a sentence added to a provision


def sentence_act() -> Act:
    """Article 1 with an unnumbered block, and a paragraph whose text is in blocks."""
    act = Act(celex="TEST", title="t")
    one = act.root.add(Node(Kind.ARTICLE, key="1"))
    one.add(Node(Kind.SUBPARAGRAPH, text="The first block. It has two sentences."))
    two = act.root.add(Node(Kind.ARTICLE, key="2"))
    paragraph = two.add(Node(Kind.PARAGRAPH, key="2"))
    paragraph.add(Node(Kind.SUBPARAGRAPH, text="The opening block."))
    paragraph.add(Node(Kind.SUBPARAGRAPH, text="The closing block."))
    return act


def test_an_added_sentence_extends_the_provision_it_is_added_to():
    """COM(2025) 524: written as an insertion it became a paragraph of its own."""
    act = sentence_act()
    node = act.article("1").of_kind(Kind.SUBPARAGRAPH)[0]
    instr = instruction(Op.APPEND_NODE, target_text="sentence",
                        payload="A third sentence.")
    change = build_changes(Resolution(instr, node=node, status="resolved"))[0]

    assert change.kind is ChangeKind.TEXT_EDIT
    assert change.node is node
    assert change.new_text == "The first block. It has two sentences. A third sentence."


def test_an_added_sentence_lands_in_the_block_that_ends_the_provision():
    """A numbered paragraph has no words of its own; its last block ends it."""
    act = sentence_act()
    paragraph = act.article("2").by_key(Kind.PARAGRAPH, "2")
    instr = instruction(Op.APPEND_NODE, target_text="sentence",
                        payload="An added sentence.")
    change = build_changes(Resolution(instr, node=paragraph, status="resolved"))[0]

    assert change.node is paragraph.of_kind(Kind.SUBPARAGRAPH)[1]
    assert change.new_text == "The closing block. An added sentence."


def test_a_sentence_added_in_the_beginning_opens_the_provision():
    act = sentence_act()
    paragraph = act.article("2").by_key(Kind.PARAGRAPH, "2")
    instr = instruction(Op.APPEND_NODE, target_text="sentence",
                        payload="An opening sentence.", anchor_where="before",
                        anchor_text="paragraph 2")
    change = build_changes(Resolution(instr, node=paragraph, status="resolved"))[0]

    assert change.node is paragraph.of_kind(Kind.SUBPARAGRAPH)[0]
    assert change.new_text == "An opening sentence. The opening block."


def test_a_sentence_added_after_another_goes_beside_it_not_at_the_end():
    """'inserted after the first sentence' — the block has a second one after it."""
    act = sentence_act()
    block = act.article("1").of_kind(Kind.SUBPARAGRAPH)[0]
    first = resolve(NodePath((Step(Kind.SENTENCE, index=1),)), block)
    instr = instruction(Op.INSERT_NODE, target_text="sentence",
                        payload="An inserted sentence.", anchor_where="after",
                        anchor_text="the first sentence")
    change = build_changes(Resolution(instr, node=first, status="resolved"))[0]

    assert change.kind is ChangeKind.TEXT_EDIT
    assert change.node is block
    assert change.new_text == (
        "The first block. An inserted sentence. It has two sentences."
    )


def test_provisions_added_are_still_blocks_of_their_own():
    act = sentence_act()
    node = act.article("1")
    instr = instruction(Op.APPEND_NODE, target_text="points",
                        payload="(a) a new point.")
    change = build_changes(Resolution(instr, node=node, status="resolved"))[0]

    assert change.kind is ChangeKind.INSERT
    assert change.anchor is node


def test_leading_split_leaves_the_provisions_the_payload_never_reaches():
    """Three paragraphs replaced by two: the third has no replacement text."""
    payload = "3. The new third paragraph. 4. The new fourth paragraph."
    result = split_leading(payload, ["3", "4", "5"], _opening)
    assert result is not None
    segments, dropped = result
    assert segments == ["3. The new third paragraph.", "4. The new fourth paragraph."]
    assert dropped == ["5"]


def test_leading_split_refuses_a_number_skipped_in_the_middle():
    """3 and 5 but no 4 is numbering that has moved, not a provision deleted."""
    payload = "3. The new third paragraph. 5. The new fifth paragraph."
    assert split_leading(payload, ["3", "4", "5"], _opening) is None


def test_a_provision_with_no_replacement_text_is_deleted():
    """COM(2025) 524, Article 1(2): Article 4(3), (4) and (5) replaced by two.

    Paragraph 5 is replaced by nothing, which is a deletion — not something to hand
    back to the reader as unhandled.
    """
    act = Act(celex="TEST", title="t")
    article = act.root.add(Node(Kind.ARTICLE, key="4"))
    for key in ("3", "4", "5"):
        paragraph = article.add(Node(Kind.PARAGRAPH, key=key))
        paragraph.add(Node(Kind.SUBPARAGRAPH, text=f"The old paragraph {key}."))
    nodes = article.of_kind(Kind.PARAGRAPH)

    instr = instruction(
        Op.REPLACE_NODE,
        payload="3. The new third paragraph. 4. The new fourth paragraph.",
    )
    changes = build_changes(
        Resolution(instr, nodes=nodes, status="resolved", enumerated=True)
    )

    assert [c.kind for c in changes] == [
        ChangeKind.REPLACE, ChangeKind.REPLACE, ChangeKind.DELETE
    ]
    assert changes[0].new_text == "3. The new third paragraph."
    assert changes[1].new_text == "4. The new fourth paragraph."
    assert changes[2].node is nodes[2]
    assert changes[2].new_text == ""
    assert "5. The old paragraph 5." in changes[2].old_text
    assert "replaced by nothing" in changes[2].note


def test_undivided_payload_is_applied_once_not_to_every_target():
    """COM(2025) 524 replaces Article 4(3), (4) and (5) with two paragraphs.

    Nothing in that payload opens with "5.", so it cannot be cut three ways — and the
    undivided block used to be applied to each target in turn, writing the same 605
    words into all three provisions.
    """
    act = make_act()
    nodes = act.article("3").of_kind(Kind.POINT)
    payload = "‘new entrant’ means a new thing, and operators are not spoken of."
    instr = instruction(Op.REPLACE_NODE, payload=payload)
    changes = build_changes(
        Resolution(instr, nodes=nodes, status="resolved", enumerated=True)
    )

    assert [c.kind for c in changes] == [ChangeKind.REPLACE, ChangeKind.SKIPPED]
    assert changes[0].node is nodes[0]
    assert changes[0].new_text == payload
    # The second provision keeps its own text: which words are its replacement is
    # exactly what could not be established.
    assert changes[1].node is nodes[1]
    assert changes[1].new_text == ""
    assert nodes[1].citation not in changes[0].new_text
    assert all(c.needs_review for c in changes)


def test_articles_split_at_their_headings():
    payload = ("Article 2 No quota systems shall apply. "
               "Article 3 1. In order to benefit, the undertaking shall record.")
    assert split_articles_payload(payload, ["2", "3"]) == [
        "Article 2 No quota systems shall apply.",
        "Article 3 1. In order to benefit, the undertaking shall record.",
    ]


def test_articles_split_refused_when_a_heading_is_missing():
    payload = "Article 2 No quota systems shall apply. 3. A paragraph, not an article."
    assert split_articles_payload(payload, ["2", "3"]) is None


def test_articles_split_is_not_fooled_by_a_cross_reference():
    """"as specified in Article 3" is a reference, and it precedes the real heading."""
    payload = ("Article 2 The rules as specified in Article 3 shall apply. "
               "Article 3 The rules referred to.")
    assert split_articles_payload(payload, ["2", "3"]) == [
        "Article 2 The rules as specified in Article 3 shall apply.",
        "Article 3 The rules referred to.",
    ]


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


def test_a_skip_keeps_the_provision_the_resolver_reached():
    """So the reader is told there, in a comment, rather than at the end."""
    act = make_act()
    change = build_changes(
        Resolution(instruction(Op.UNKNOWN), node=act.article("3"),
                   status="unresolved", reason="no rule matched this instruction")
    )[0]
    assert change.kind is ChangeKind.SKIPPED
    assert change.node is act.article("3")


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


def test_a_replacement_read_block_by_block_keeps_the_paragraph_number():
    """The number the act keeps outside a paragraph's words goes on the first block
    it is printed as — the payload's own source lines cannot carry it."""
    act = Act(celex="TEST", title="t")
    paragraph = act.root.add(Node(Kind.ARTICLE, key="9")).add(
        Node(Kind.PARAGRAPH, key="2")
    )
    paragraph.add(Node(Kind.SUBPARAGRAPH, text="Member States shall report yearly."))
    paragraph.add(Node(Kind.SUBPARAGRAPH, text="The Commission shall publish them."))

    instr = instruction(
        Op.REPLACE_NODE,
        payload="Member States shall report every second year. "
                "The Commission shall publish them.",
        payload_layout=["    Member States shall report every second year.",
                        "    The Commission shall publish them."],
        payload_breaks=[True, True],
    )
    change = build_changes(Resolution(instr, node=paragraph, status="resolved"))[0]

    assert change.old_text.startswith("2. ")
    assert change.new_text.startswith("2. Member States shall report every second")
    assert change.blocks[0].new.text.startswith("2. ")
    assert change.blocks[1].new.text.startswith("The Commission")


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
def test_a_run_replaced_as_a_whole_leaves_nothing_of_it_standing(changes):
    """Art. 1(14)(a): the first and second subparagraphs of Article 10(1).

    One payload, two provisions and no numbers to divide it by: it stands where the
    run begins and what it replaced goes with it — not one copy in each, and not the
    second left holding text the amendment repeals.
    """
    replaced, deleted = changes["1.14.a"]
    assert replaced.kind is ChangeKind.REPLACE
    assert replaced.node.citation.endswith("paragraph 1 / subparagraph 1")
    assert replaced.new_text.startswith("1. At least 57 %")
    assert deleted.kind is ChangeKind.DELETE
    assert deleted.node.citation.endswith("paragraph 1 / subparagraph 2")
    assert deleted.old_text.startswith("From 2021 onwards")
    assert deleted.node.parent.of_kind(Kind.SUBPARAGRAPH)[2].text.startswith("2 %")


@pytestmark_corpus
def test_new_provisions_are_pure_insertions(corpus):
    inserts = [c for cs in corpus.values() for c in cs.renderable
               if c.kind is ChangeKind.INSERT]
    assert inserts
    assert all(c.old_text == "" and c.edit.deleted_text == "" for c in inserts)


@pytestmark_corpus
def test_replacement_text_is_the_payload_verbatim(corpus):
    """The document must never contain text the pipeline composed.

    The one thing a replacement carries beyond its payload is the number of the
    paragraph it replaces, which the act keeps outside the words on both sides of
    the change and which the payload usually does not restate.
    """
    for changeset in corpus.values():
        for change in changeset.renderable:
            if change.kind is ChangeKind.REPLACE:
                payload = change.instruction.payload
                number = opening_number(change.node)
                assert change.new_text in (payload, number + payload)


@pytestmark_corpus
def test_a_replaced_paragraph_keeps_its_number(corpus):
    """Art. 1(7)(c): paragraph 4 of Article 3gg, replaced by text that omits "4.".

    Printed as the payload stands, the paragraph would come out unnumbered between
    paragraphs 3 and 5 — the number struck and nothing put back.
    """
    change = next(c for cs in corpus.values() for c in cs.renderable
                  if c.instruction.id == "1.7.c")
    assert change.old_text.startswith("4. ")
    assert change.new_text.startswith("4. No later than 30 September 2035,")
    assert "4." not in change.edit.deleted_text


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


def test_articles_split_is_not_fooled_by_a_reference_opening_a_sentence():
    """"In accordance with Article 3, ..." follows a full stop like a heading does."""
    payload = ("Article 2 The rules shall apply. In accordance with Article 3, "
               "Member States shall report. Article 3 The rules referred to.")
    assert split_articles_payload(payload, ["2", "3"]) == [
        "Article 2 The rules shall apply. In accordance with Article 3, "
        "Member States shall report.",
        "Article 3 The rules referred to.",
    ]
