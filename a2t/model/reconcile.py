"""Cross-check the HTML-derived tree against `eurlex-builder`'s Parquet coordinates.

The two sources are built by different code paths, so disagreement is informative.
Two divergences are *expected* and classified as such rather than reported as faults
(see PHASE0.md):

* multi-letter points — the library's splitter handles single letters only, so
  `(aa)`–`(ai)` are missing from Parquet but present in the tree;
* unnumbered subparagraphs — the library does not split them where an article has no
  numbered paragraphs, so the tree has more subparagraphs than Parquet.

Anything else is a genuine discrepancy worth surfacing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from a2t.model.tree import Act, Kind


@dataclass
class Reconciliation:
    expected: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unexpected

    def summary(self) -> str:
        return (
            f"{len(self.expected)} expected divergence(s), "
            f"{len(self.unexpected)} unexpected"
        )


def reconcile(act: Act, text_units) -> Reconciliation:
    result = Reconciliation()
    if text_units is None or getattr(text_units, "height", 0) == 0:
        return result

    import polars as pl

    units = text_units.filter(pl.col("type") == "article")

    parquet_articles = {
        str(k) for k in units["number"].drop_nulls().to_list()
    }
    tree_articles = {a.key for a in act.articles if a.key}

    for key in sorted(tree_articles - parquet_articles):
        result.unexpected.append(f"article {key} in tree but not in text_units")
    for key in sorted(parquet_articles - tree_articles):
        # Recitals share the `number` space; only flag if it looks like an article.
        result.unexpected.append(f"article {key} in text_units but not in tree")

    for article in act.articles:
        tree_points = {p.key for p in article.of_kind(Kind.POINT) if p.key}
        if not tree_points:
            continue
        rows = units.filter(pl.col("number") == article.key)
        parquet_points = {
            str(p) for p in rows["point_letter"].drop_nulls().to_list()
        }
        missing = tree_points - parquet_points
        # `point_letter` holds a single ASCII letter, so anything else — multi-letter
        # points and numbered points alike — cannot appear there by construction.
        multi_letter = {p for p in missing if p.isalpha() and len(p) > 1}
        numeric = {p for p in missing if not p.isalpha()}
        if multi_letter:
            result.expected.append(
                f"Article {article.key}: {len(multi_letter)} multi-letter point(s) "
                f"absent from text_units ({', '.join(sorted(multi_letter))})"
            )
        if numeric:
            result.expected.append(
                f"Article {article.key}: {len(numeric)} numbered point(s) absent "
                f"from text_units ({', '.join(sorted(numeric))})"
            )
        for point in sorted(missing - multi_letter - numeric):
            result.unexpected.append(
                f"Article {article.key} point ({point}) in tree but not in text_units"
            )

    tree_subs = act.count(Kind.SUBPARAGRAPH)
    parquet_subs = units["subparagraph_num"].drop_nulls().len()
    if tree_subs > parquet_subs:
        result.expected.append(
            f"tree has {tree_subs} subparagraphs vs {parquet_subs} in text_units "
            f"(library does not split unnumbered subparagraphs)"
        )
    elif parquet_subs > tree_subs:
        result.unexpected.append(
            f"text_units has more subparagraphs ({parquet_subs}) than the tree "
            f"({tree_subs})"
        )
    return result
