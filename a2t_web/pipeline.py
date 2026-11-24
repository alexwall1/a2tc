"""Command construction for the `a2t convert` subprocess and log tailing.

This module is the test seam for the worker: tests monkeypatch
`a2t_web.worker.pipeline.build_command` to fake pipeline subprocesses.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_HEADING_RE = re.compile(r"^## (.+)$")
_DOCUMENT_RE = re.compile(r"^- Document: `([^`]+)`")


def build_command(
    pdf: Path, outdir: Path, cache: Path, author: str = "A2T (web)"
) -> list[str]:
    """argv for `python -m a2t.cli convert ...` in the same interpreter."""
    return [
        sys.executable,
        "-m",
        "a2t.cli",
        "convert",
        str(pdf),
        "-o",
        str(outdir),
        "--cache",
        str(cache),
        "--author",
        author,
        "--llm",
    ]


def artifact_titles(report_path: Path) -> dict[str, str]:
    """Map each ``- Document: `...``` artifact to its ``## <citation>`` section.

    A finished ``report.md`` lists every converted act under its own ``##``
    heading, with a ``- Document: `<name>_tracked.docx` -- ...`` line in the
    body. Only ``.docx`` names are returned. A missing or unreadable report
    yields an empty dict.
    """
    try:
        text = report_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    titles: dict[str, str] = {}
    heading: str | None = None
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            heading = match.group(1)
            continue
        match = _DOCUMENT_RE.match(line)
        if match and heading and match.group(1).endswith(".docx"):
            titles[match.group(1)] = heading
    return titles


def tail(path: Path, n: int = 200) -> str:
    """Last `n` lines of `path`; empty string when the file is missing."""
    if n <= 0:
        return ""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size == 0:
                return ""
            chunks: list[bytes] = []
            pos = size
            newlines = 0
            block = 4096
            while pos > 0 and newlines <= n:
                start = max(0, pos - block)
                handle.seek(start)
                chunk = handle.read(pos - start)
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
                pos = start
            data = b"".join(reversed(chunks))
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    ends_with_newline = data.endswith(b"\n")
    if ends_with_newline:
        lines.pop()
    selected = lines[-n:]
    if ends_with_newline:
        return "\n".join(selected) + "\n"
    return "\n".join(selected)