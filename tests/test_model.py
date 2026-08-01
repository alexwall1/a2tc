"""Phase 3: tree assembly, addressing, and sentence derivation.

Facts asserted here are cross-checked against the official consolidated PDF
(`consolidated_ets.pdf`, 02003L0087 — EN — 01.03.2024 — 016.001).
"""

from pathlib import Path

import pytest

from a2t.model.assemble import assemble, clean_text
from a2t.model.path import Path as NodePath
from a2t.model.path import Step, parse_ordinal, resolve
from a2t.model.reconcile import reconcile
from a2t.model.subdivide import split_sentences
from a2t.model.tree import Act, Kind, Node

CACHE = Path(__file__).resolve().parents[1] / "out" / "cache" / "02003L0087-20240301"

pytestmark = pytest.mark.skipif(
    not (CACHE / "works.parquet").is_file(),
    reason="run `a2t retrieve amendment_proposal_ets.pdf --cache out/cache` first",
)


@pytest.fixture(scope="module")
def retrieved():
    import polars as pl

    works = pl.read_parquet(CACHE / "works.parquet").row(0, named=True)
    units = pl.read_parquet(CACHE / "text_units.parquet")
    return works, units


@pytest.fixture(scope="module")
def act(retrieved):
    works, _ = retrieved
    return assemble(works["full_text_html"], "02003L0087-20240301", works["title"])


# ------------------------------------------------------------------- assembly


def test_article_count(act):
    """81 articles — the enacting-terms wrapper is not one of them."""
    assert len(act.articles) == 81


def test_no_duplicate_article_keys(act):
    keys = [a.key for a in act.articles]
    assert len(keys) == len(set(keys))


def test_article_keys_include_letter_suffixes(act):
    for key in ["3a", "3ga", "3gg", "10a", "10ca", "28b", "30m"]:
        assert act.article(key) is not None, key


def test_multi_letter_points_recovered(act):
    """The limitation that made 2 of 10 Phase 0 probes fail."""
    points = [p.key for p in act.article("3").of_kind(Kind.POINT)]
    assert len(points) == 34
    assert points[:3] == ["a", "b", "c"]
    assert points[-4:] == ["af", "ag", "ah", "ai"]


def test_unnumbered_subparagraphs_recovered(act):
    """Article 9 has no numbered paragraphs; its body is 4 subparagraphs."""
    subs = act.article("9").of_kind(Kind.SUBPARAGRAPH)
    assert len(subs) == 4
    assert subs[3].text.startswith("From 1 January 2026 and 1 January 2027")


def test_article_10a_paragraph_sequence(act):
    """Matches the official PDF exactly, including gaps left by earlier repeals."""
    keys = [p.key for p in act.article("10a").of_kind(Kind.PARAGRAPH)]
    assert keys == ["1", "1a", "2", "5", "5a", "5b", "6", "7", "8", "8a", "8b",
                    "9", "11", "19", "20", "21", "22"]


def test_paragraph_subparagraphs(act):
    """10a(1a) has five subparagraphs — the trailing p.norm siblings belong to it."""
    para = act.article("10a").by_key(Kind.PARAGRAPH, "1a")
    assert len(para.of_kind(Kind.SUBPARAGRAPH)) == 5


def test_article_titles_captured(act):
    assert act.article("9").title == "Union-wide quantity of allowances"
    assert act.article("3").title == "Definitions"


def test_consolidation_markers_stripped(act):
    text = act.article("1").full_text
    assert "►" not in text and "◄" not in text and "▼" not in text
    assert "Union-wide" in text or "Union" in text


def test_clean_text_removes_markers_without_losing_words():
    from lxml import html as LH

    el = LH.fromstring("<div>a <span>►M9 </span>Union<span> ◄</span> -wide system</div>")
    assert clean_text(el) == "a Union-wide system"


# ------------------------------------------------------------------ addressing


@pytest.mark.parametrize(
    "steps, expect_start",
    [
        # real targets from COM(2026) 616 final, Article 1
        ((Step(Kind.ARTICLE, "3"), Step(Kind.POINT, "h")), "‘new entrant’"),
        ((Step(Kind.ARTICLE, "3"), Step(Kind.POINT, "ag")), "‘release for consumption’"),
        ((Step(Kind.ARTICLE, "9"), Step(Kind.SUBPARAGRAPH, index=4)),
         "From 1 January 2026"),
        ((Step(Kind.ARTICLE, "10a"), Step(Kind.PARAGRAPH, "1a"),
          Step(Kind.SUBPARAGRAPH, index=2)), "By way of derogation"),
    ],
)
def test_resolves_real_amendment_targets(act, steps, expect_start):
    node = resolve(NodePath(steps), act)
    assert node is not None
    assert node.text.startswith(expect_start)


def test_negative_index_addresses_from_the_end(act):
    para = act.article("10a").by_key(Kind.PARAGRAPH, "1a")
    last = resolve(NodePath((Step(Kind.SUBPARAGRAPH, index=-1),)), para)
    penultimate = resolve(NodePath((Step(Kind.SUBPARAGRAPH, index=-2),)), para)
    assert last is para.of_kind(Kind.SUBPARAGRAPH)[4]
    assert penultimate is para.of_kind(Kind.SUBPARAGRAPH)[3]


def test_unresolvable_path_returns_none(act):
    assert resolve(NodePath((Step(Kind.ARTICLE, "999"),)), act) is None
    assert resolve(
        NodePath((Step(Kind.ARTICLE, "3"), Step(Kind.POINT, "zz"))), act
    ) is None


def test_point_key_normalisation(act):
    """'(h)' and 'h' address the same point."""
    article = act.article("3")
    assert article.by_key(Kind.POINT, "(h)") is article.by_key(Kind.POINT, "h")


def test_citation_string(act):
    node = resolve(
        NodePath((Step(Kind.ARTICLE, "10a"), Step(Kind.PARAGRAPH, "1a"),
                  Step(Kind.SUBPARAGRAPH, index=2))), act
    )
    # Chapters are part of the tree, so they appear in the citation chain.
    assert node.citation == (
        "chapter III / Article 10a / paragraph 1a / subparagraph 2"
    )


@pytest.mark.parametrize(
    "word, value",
    [("first", 1), ("second", 2), ("fourth", 4), ("penultimate", -2),
     ("last", -1), ("final", -1), ("4th", 4), ("21st", 21)],
)
def test_parse_ordinal(word, value):
    assert parse_ordinal(word) == value


def test_parse_ordinal_rejects_nonsense():
    assert parse_ordinal("bananas") is None


# -------------------------------------------------------------------- sentences


def test_splits_plain_sentences():
    text = "The first sentence ends here. The second follows. And a third."
    assert len(split_sentences(text)) == 3


def test_does_not_split_on_abbreviations():
    text = "See Regulation (EC) No 219/2009 and p. 32 of the Annex. Then a new one."
    assert len(split_sentences(text)) == 2


def test_does_not_split_inside_unclosed_parenthesis():
    text = "The rate (as set out in Article 9. of that act) shall apply. Next."
    assert len(split_sentences(text)) == 2


def test_sentence_addressing_on_real_text(act):
    para = act.article("10b").by_key(Kind.PARAGRAPH, "1")
    second = resolve(NodePath((Step(Kind.SENTENCE, index=2),)), para)
    assert second is not None and second.text


def test_empty_text_yields_no_sentences():
    assert split_sentences("") == []
    assert split_sentences("   ") == []


# ---------------------------------------------------------------- reconciliation


def test_reconciliation_has_no_unexpected_divergences(act, retrieved):
    _, units = retrieved
    result = reconcile(act, units)
    assert result.unexpected == []


def test_reconciliation_classifies_known_gaps(act, retrieved):
    _, units = retrieved
    result = reconcile(act, units)
    joined = " ".join(result.expected)
    assert "multi-letter point" in joined
    assert "subparagraph" in joined


# ------------------------------------------------------------------ tree basics


def test_add_assigns_sibling_indices():
    root = Node(Kind.ARTICLE, key="1")
    for _ in range(3):
        root.add(Node(Kind.SUBPARAGRAPH))
    assert [c.index for c in root.of_kind(Kind.SUBPARAGRAPH)] == [1, 2, 3]


def test_by_index_is_one_based_and_bounded():
    root = Node(Kind.ARTICLE, key="1")
    for _ in range(2):
        root.add(Node(Kind.SUBPARAGRAPH))
    assert root.by_index(Kind.SUBPARAGRAPH, 1) is root.children[0]
    assert root.by_index(Kind.SUBPARAGRAPH, 3) is None
    assert root.by_index(Kind.SUBPARAGRAPH, -3) is None


def test_empty_act_is_safe():
    empty = Act(celex="x", title="y")
    assert empty.articles == []
    assert empty.article("1") is None
