"""Parsing helpers in `a2t_web.pipeline`: report titles and command building."""

from pathlib import Path

from a2t_web.pipeline import artifact_titles, build_command

REPORT = """# Conversion report — com.pdf
Proposal date: **2026-03-01**

## Acts amended
| Act | CELEX (version) | Scope | Identified via |
|---|---|---|---|
| Directive 2003/87/EC | `02003L0087-20240301` | whole act | citation |

## Directive 2003/87/EC
- Base text: `02003L0087-20240301` — Directive 2003/87/EC of the European Parliament and of the Council
- Instructions: 4 (2 top-level)
- Resolution: 4 resolved, 0 dropped, 0 skipped
- Changes: 3 inserted, 1 deleted
- Document: `02003L0087-20240301_tracked.docx` — 3 inserted, 1 deleted

## Decision (EU) 2015/1814
- Base text: `02015D1814-20240301` — Decision (EU) 2015/1814
- Instructions: 1
- Resolution: resolved
- Changes: 2 inserted
- Document: `02015D1814-20240301_tracked.docx` — 2 inserted

## Needs human review
Nothing flagged.
"""


def test_artifact_titles_maps_docx_to_citation(tmp_path: Path):
    report = tmp_path / "report.md"
    report.write_text(REPORT)
    assert artifact_titles(report) == {
        "02003L0087-20240301_tracked.docx": "Directive 2003/87/EC",
        "02015D1814-20240301_tracked.docx": "Decision (EU) 2015/1814",
    }


def test_artifact_titles_ignores_tables_and_non_sections(tmp_path):
    report = tmp_path / "report.md"
    report.write_text(REPORT)
    titles = artifact_titles(report)
    # the `## Acts amended` table line carries no `- Document:` marker
    assert "2003L0087-20240301" not in titles
    # only .docx artifacts are returned
    assert all(name.endswith(".docx") for name in titles)


def test_artifact_titles_missing_file_returns_empty(tmp_path):
    assert artifact_titles(tmp_path / "nope.md") == {}


def test_artifact_titles_empty_file_returns_empty(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("")
    assert artifact_titles(report) == {}


def test_artifact_titles_skips_non_docx_document_lines(tmp_path):
    # `- Document:` lines for non-docx artifacts (the report itself, stray
    # files) must not populate the map, even under a citation heading.
    text = (
        REPORT
        + "## Directive (EU) 2019/2121\n"
        + "- Document: `02019L2121-20200301_tracked.docx` — 1 inserted\n"
        + "- Document: `notes.md` — scratch notes\n"
    )
    report = tmp_path / "report.md"
    report.write_text(text)
    titles = artifact_titles(report)
    assert titles["02019L2121-20200301_tracked.docx"] == "Directive (EU) 2019/2121"
    assert "notes.md" not in titles
    assert "report.md" not in titles


def test_build_command_uses_llm(tmp_path):
    command = build_command(
        tmp_path / "p.pdf", tmp_path / "out", tmp_path / "cache"
    )
    assert command[0] != ""
    assert command[:3] == [command[0], "-m", "a2t.cli"]
    assert "--llm" in command
    assert "--no-llm" not in command