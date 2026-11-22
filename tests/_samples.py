"""Where the tests find the acts they need.

`a2t retrieve` and `a2t convert` cache to ~/.cache/a2t/acts unless told otherwise, and
the tests used to look only under out/cache — so an act already retrieved by ordinary
use was invisible to them, and the suite skipped for want of a flag. Either place
counts; the project-local one wins, since that is the one a developer filled on purpose.
"""

from __future__ import annotations

from pathlib import Path

from a2t.retrieval.eurlex_builder import DEFAULT_CACHE

ROOT = Path(__file__).resolve().parents[1]


def cached(pinned: str) -> Path:
    """The cache directory of one pinned consolidation, e.g. 02003L0087-20240301."""
    local = ROOT / "out" / "cache" / pinned
    if (local / "works.parquet").is_file():
        return local
    default = DEFAULT_CACHE / pinned
    return default if (default / "works.parquet").is_file() else local
