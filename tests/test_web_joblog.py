"""Pure unit tests for the durable job-log format/IO module (a2t_web.joblog)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from a2t_web.joblog import (
    append_log,
    create_record,
    format_row,
    parse_line,
    read_entries,
    terminal_record,
    _escape,
    _unescape,
)


def make_job(**overrides):
    values = {
        "id": "j1",
        "pdf_name": "com.pdf",
        "status": "queued",
        "error": None,
        "created_at": 1750000000.0,
        "started_at": None,
        "finished_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# ------------------------------------------------------------------ escaping


@pytest.mark.parametrize("text", ["plain", "with slash \\ here", "tab\there"])
def test_escape_round_trip(text):
    assert _unescape(_escape(text)) == text


def test_escape_handles_newlines_and_carriage_returns():
    original = "line1\nline2\r\nmixed\tand\\slashes"
    assert _unescape(_escape(original)) == original


def test_escape_maps_each_special_character():
    assert _escape("\\") == "\\\\"
    assert _escape("\t") == "\\t"
    assert _escape("\n") == "\\n"
    assert _escape("\r") == "\\r"


# -------------------------------------------------------------- row round-trips


def test_format_and_parse_create_record_roundtrip():
    job = make_job()
    record = create_record(job)
    line = format_row(record)
    parsed = parse_line(line)
    assert parsed is not None
    assert parsed["event"] == "create"
    assert parsed["job_id"] == "j1"
    assert parsed["pdf_name"] == "com.pdf"
    assert parsed["status"] == "queued"
    assert parsed["created_at"] == 1750000000.0
    assert parsed["started_at"] is None
    assert parsed["finished_at"] is None
    assert parsed["error"] == ""
    assert parsed["ts"] is not None


def test_format_and_parse_terminal_row_roundtrip():
    job = make_job(
        status="done",
        error=None,
        started_at=1750000001.0,
        finished_at=1750000002.0,
    )
    record = terminal_record(job)
    line = format_row(record)
    parsed = parse_line(line)
    assert parsed is not None
    assert parsed["event"] == "terminal"
    assert parsed["status"] == "done"
    assert parsed["started_at"] == 1750000001.0
    assert parsed["finished_at"] == 1750000002.0
    assert parsed["error"] == ""


def test_format_and_parse_error_with_special_characters_roundtrip():
    job = make_job(status="failed", error="line1\nline2\ttab\nboom\\tail")
    parsed = parse_line(format_row(terminal_record(job)))
    assert parsed["error"] == "line1\nline2\ttab\nboom\\tail"


# ---------------------------------------------------------------- malformed


@pytest.mark.parametrize("line", ["", "junk", "a\tb\tc", "\t" * 8])
def test_parse_line_malformed_returns_none(line):
    assert parse_line(line) is None


# ------------------------------------------------------------------- IO


def test_append_log_appends_and_creates_parent(tmp_path):
    path = tmp_path / "deep" / "jobs.log"
    append_log(path, create_record(make_job()))
    append_log(path, terminal_record(make_job(status="done")))
    text = path.read_text()
    assert text.count("\n") == 2
    assert len(read_entries(path)) == 1  # same job: last record wins


def test_read_entries_missing_file_returns_empty(tmp_path):
    assert read_entries(tmp_path / "nope.log") == []


def test_read_entries_newest_first_and_limit(tmp_path):
    path = tmp_path / "jobs.log"
    for i in range(3):
        job = make_job(id=f"job{i}")
        record = create_record(job)
        record["ts"] = 1750000000.0 + i
        append_log(path, record)
    entries = read_entries(path)
    assert [e["job_id"] for e in entries] == ["job2", "job1", "job0"]
    assert [e["job_id"] for e in read_entries(path, limit=2)] == ["job2", "job1"]


def test_read_entries_keeps_last_record_per_job(tmp_path):
    path = tmp_path / "jobs.log"
    append_log(path, create_record(make_job(id="j1", status="queued")))
    append_log(path, terminal_record(make_job(id="j1", status="done")))
    entries = read_entries(path)
    assert len(entries) == 1
    assert entries[0]["job_id"] == "j1"
    assert entries[0]["status"] == "done"
    assert entries[0]["event"] == "terminal"


def test_read_entries_skips_malformed_lines(tmp_path):
    path = tmp_path / "jobs.log"
    path.write_text("junk\n" + format_row(create_record(make_job(id="ok"))))
    entries = read_entries(path)
    assert [e["job_id"] for e in entries] == ["ok"]