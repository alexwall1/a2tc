"""Sanity gate: reject non-proposals early, reusing the CLI's detection blocks.

No real PDFs and no network: every building block is monkeypatched on the
`a2t_web.sanity` module.
"""

from datetime import date

import pytest

import a2t_web.sanity as sanity
from a2t.detect.celex import Citation
from a2t.detect.targets import Detection, Target
from a2t.extract.deboilerplate import CleanResult
from a2t.extract.pdf import ExtractionError, Line


def make_lines():
    return [
        Line("Brussels, 17.7.2026", 1, 1),
        Line("HAVE ADOPTED THIS DIRECTIVE:", 1, 2),
        Line("Directive 2003/87/EC is amended as follows:", 1, 3),
    ]


def make_target():
    return Target(
        citation=Citation("Directive 2003/87/EC", "Directive", 2003, 87),
        celex="32003L0087",
        celex_source="citation",
        root_scope=None,
        amending_article="1",
        anchor_line=3,
    )


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "com.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    return path


@pytest.fixture
def lines():
    return make_lines()


@pytest.fixture
def pass_detection():
    return Detection(targets=[make_target()])


@pytest.fixture
def stub_gate(monkeypatch, lines, pass_detection):
    """Wire every building block so the gate would accept."""
    monkeypatch.setattr(sanity, "pdftotext_available", lambda: True)
    monkeypatch.setattr(sanity, "extract_lines", lambda _pdf: lines)
    monkeypatch.setattr(sanity, "operative_part", lambda _lines: CleanResult(lines))
    monkeypatch.setattr(sanity, "detect_targets", lambda *_args: pass_detection)
    monkeypatch.setattr(sanity, "find_proposal_date", lambda _lines: date(2026, 7, 17))


def test_accepts_when_targets_and_proposal_date_present(stub_gate, pdf, pass_detection):
    assert sanity.sanity_check(pdf) is pass_detection


def test_rejects_when_pdftotext_unavailable(monkeypatch, pdf):
    monkeypatch.setattr(sanity, "pdftotext_available", lambda: False)
    with pytest.raises(sanity.SanityError, match="pdftotext"):
        sanity.sanity_check(pdf)


def test_rejects_when_extraction_fails(stub_gate, monkeypatch, pdf):
    def raise_extraction(_pdf):
        raise ExtractionError("pdftotext failed: boom")

    monkeypatch.setattr(sanity, "extract_lines", raise_extraction)
    with pytest.raises(sanity.SanityError, match="does not look like") as excinfo:
        sanity.sanity_check(pdf)
    assert "boom" in str(excinfo.value)


def test_rejects_when_operative_part_fails(stub_gate, monkeypatch, pdf):
    def raise_no_formula(_lines):
        raise ValueError("no enacting formula found — is this an amending act?")

    monkeypatch.setattr(sanity, "operative_part", raise_no_formula)
    with pytest.raises(sanity.SanityError, match="does not look like") as excinfo:
        sanity.sanity_check(pdf)
    assert "no enacting formula found" in str(excinfo.value)


def test_rejects_when_no_targets(stub_gate, monkeypatch, pdf):
    monkeypatch.setattr(sanity, "detect_targets", lambda *_args: Detection())
    with pytest.raises(sanity.SanityError, match="no amendment targets"):
        sanity.sanity_check(pdf)


def test_rejects_when_no_proposal_date(stub_gate, monkeypatch, pdf):
    monkeypatch.setattr(sanity, "find_proposal_date", lambda _lines: None)
    with pytest.raises(sanity.SanityError, match="proposal date"):
        sanity.sanity_check(pdf)