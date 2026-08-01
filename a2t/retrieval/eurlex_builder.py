"""`eurlex-builder`-backed base act provider.

The library is a batch ETL pipeline, not a request/response API: it takes a YAML
config, runs, and writes Parquet. This adapter owns that round-trip and caches by
pinned CELEX, so a re-run costs nothing and the test suite never touches the network.

`store_raw_html` is **required**, not optional: `full_text_html` is the primary
structural source (see PHASE0.md), because the library's own splitter drops
multi-letter points and unnumbered subparagraph boundaries.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import yaml

from a2t.retrieval.base import RetrievalError, RetrievedAct
from a2t.retrieval.versions import parse_pinned

DEFAULT_CACHE = Path.home() / ".cache" / "a2t" / "acts"


def build_config(celex: str, output_dir: Path) -> dict:
    return {
        "metadata": {
            "project_name": f"a2t base act {celex}",
            "description": "Base act retrieval for amendment conversion.",
        },
        "data": {"mode": "fixed", "celex_ids": [celex]},
        "processing": {
            "text_extraction": {
                "article_granularity": "point",
                "include_recitals": False,   # amendments target articles
                "include_articles": True,
                "include_annexes": False,    # annex amendments are out of scope
                "strip_boilerplate": True,
                "store_raw_html": True,      # required: primary structural source
            },
            "translation": {
                "translate_full_text": False,
                "translate_text_units": False,
            },
            "include_relations": False,
            "include_eurovoc": False,
        },
        "output": {"formats": ["parquet"], "output_directory": str(output_dir)},
    }


class EurlexBuilderProvider:
    """Fetches base acts, caching each pinned CELEX on disk."""

    def __init__(self, cache_dir: Path | str = DEFAULT_CACHE, refresh: bool = False):
        self.cache_dir = Path(cache_dir)
        self.refresh = refresh

    def _dir_for(self, celex: str) -> Path:
        return self.cache_dir / celex

    def is_cached(self, celex: str) -> bool:
        return (self._dir_for(celex) / "works.parquet").is_file()

    def fetch(self, celex: str) -> RetrievedAct:
        target = self._dir_for(celex)
        if self.refresh or not self.is_cached(celex):
            self._run_pipeline(celex, target)
        return self._load(celex, target)

    # ------------------------------------------------------------------ internals

    def _run_pipeline(self, celex: str, target: Path) -> None:
        import contextlib
        import io

        from eurlex_builder import Pipeline  # imported lazily: heavy and optional

        target.mkdir(parents=True, exist_ok=True)
        config_path = target / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(build_config(celex, target), sort_keys=False),
            encoding="utf-8",
        )
        # The pipeline reports its own progress; keep our output clean and surface
        # its log only if it fails.
        log = io.StringIO()
        try:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                Pipeline.from_config_file(str(config_path)).run()
        except Exception as exc:
            raise RetrievalError(
                f"retrieval failed for {celex}: {exc}\n{log.getvalue()[-800:]}"
            ) from exc

    def _load(self, celex: str, target: Path) -> RetrievedAct:
        works_path = target / "works.parquet"
        if not works_path.is_file():
            raise RetrievalError(f"no output produced for {celex}")

        works = pl.read_parquet(works_path)
        if works.height == 0:
            raise RetrievalError(f"no document retrieved for {celex}")

        row = works.row(0, named=True)
        html = row.get("full_text_html") or ""
        if not html.strip():
            raise RetrievalError(
                f"{celex} returned no HTML — the version may not exist "
                f"(only actual consolidation dates resolve)"
            )

        units_path = target / "text_units.parquet"
        units = pl.read_parquet(units_path) if units_path.is_file() else pl.DataFrame()

        pinned = parse_pinned(celex)
        return RetrievedAct(
            celex=celex,
            title=row.get("title") or "",
            full_text_html=html,
            text_units=units,
            version_date=pinned[1].isoformat() if pinned else None,
        )
