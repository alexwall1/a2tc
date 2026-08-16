"""Durable, append-only job log for the web app.

Format: TSV, one record per line, 9 fields, escaped so that tabs, newlines,
backslashes, and carriage returns in job metadata cannot break the row layout.
The registry appends records on job creation and on terminal transitions; the
admin route reads the file back.

The module deliberately does NOT import ``jobs.py``: every function here is
duck-typed on the ``Job`` dataclass so the format/IO half stays pure and
directly testable.
"""

from __future__ import annotations

import time
from pathlib import Path

_FIELDS = (
    "ts",
    "event",
    "job_id",
    "pdf_name",
    "status",
    "created_at",
    "started_at",
    "finished_at",
    "error",
)

_ESCAPE_MAP = {
    "\\": "\\\\",
    "\t": "\\t",
    "\n": "\\n",
    "\r": "\\r",
}

_UNESCAPE_MAP = {
    "\\\\": "\\",
    "\\t": "\t",
    "\\n": "\n",
    "\\r": "\r",
}


def _escape(s: str) -> str:
    return "".join(_ESCAPE_MAP.get(ch, ch) for ch in s)


def _unescape(s: str) -> str:
    """Undo ``_escape`` left to right: \\\\ → \\, \\t → tab, \\n → LF, \\r → CR."""
    out: list[str] = []
    i = 0
    while i < len(s):
        two = s[i : i + 2]
        if two in _UNESCAPE_MAP:
            out.append(_UNESCAPE_MAP[two])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def create_record(job) -> dict:
    """A record for a freshly created job (status is queued)."""
    return {
        "ts": time.time(),
        "event": "create",
        "job_id": job.id,
        "pdf_name": job.pdf_name,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "error": job.error,
    }


def terminal_record(job) -> dict:
    """A record for a terminal transition (DONE/FAILED/CANCELLED)."""
    return {
        "ts": time.time(),
        "event": "terminal",
        "job_id": job.id,
        "pdf_name": job.pdf_name,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "error": job.error,
    }


def _format_timestamp(value) -> str:
    return "" if value is None else str(float(value))


def format_row(record: dict) -> str:
    """Serialize a record dict into one escaped TSV line (with trailing newline)."""
    fields = [
        _format_timestamp(record["ts"]),
        str(record["event"]),
        str(record["job_id"]),
        _escape(str(record["pdf_name"])),
        _escape(str(record["status"])),
        _format_timestamp(record.get("created_at")),
        _format_timestamp(record.get("started_at")),
        _format_timestamp(record.get("finished_at")),
        _escape(str(record.get("error") or "")),
    ]
    return "\t".join(fields) + "\n"


def _as_float(value: str) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_line(line: str) -> dict | None:
    """Parse a single TSV line into a record dict, or None for malformed lines."""
    line = line.rstrip("\n")
    if not line:
        return None
    fields = line.split("\t")
    if len(fields) != len(_FIELDS):
        return None
    (
        ts,
        event,
        job_id,
        pdf_name,
        status,
        created_at,
        started_at,
        finished_at,
        error,
    ) = fields
    if not event or not job_id:
        return None
    return {
        "ts": _as_float(ts),
        "event": _unescape(event),
        "job_id": _unescape(job_id),
        "pdf_name": _unescape(pdf_name),
        "status": _unescape(status),
        "created_at": _as_float(created_at),
        "started_at": _as_float(started_at),
        "finished_at": _as_float(finished_at),
        "error": _unescape(error),
    }


def append_log(path: Path, record: dict) -> None:
    """Append one record to the TSV log, creating the parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(format_row(record))


def read_entries(path: Path, limit: int | None = None) -> list[dict]:
    """Latest state per job id, newest first, from the TSV log.

    The log is append-only, so later lines win for a job id: a finished job
    shows its terminal record, a never-finished job its create record. Returns
    [] for a missing or unreadable log.
    """
    if not path.is_file():
        return []
    latest: dict[str, dict] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    for line in text.splitlines():
        parsed = parse_line(line)
        if parsed is not None:
            latest[parsed["job_id"]] = parsed
    entries = sorted(latest.values(), key=lambda entry: entry["ts"] or 0.0, reverse=True)
    if limit is not None:
        entries = entries[:limit]
    return entries