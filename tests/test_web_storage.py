"""Storage helpers: uploads, artifact listing, and safe path resolution."""

import io
from pathlib import Path

import pytest

from a2t_web.storage import (
    UploadTooLarge,
    ensure_data_dirs,
    list_artifacts,
    looks_like_pdf,
    resolve_artifact,
    safe_basename,
    save_upload,
)


class FakeFileStorage:
    def __init__(self, data: bytes) -> None:
        self.stream = io.BytesIO(data)


def make_file(data: bytes) -> FakeFileStorage:
    return FakeFileStorage(data)


# ---------------------------------------------------------------- safe_basename


def test_safe_basename_strips_path_components():
    assert safe_basename("/etc/passwd") == "passwd"
    assert safe_basename("../../x.pdf") == "x.pdf"


def test_safe_basename_normalizes_backslashes():
    assert safe_basename("..\\..\\x.pdf") == "x.pdf"
    assert safe_basename("C:\\Users\\a\\com.pdf") == "com.pdf"


def test_safe_basename_drops_control_chars():
    assert safe_basename("a\x00b.pdf") == "ab.pdf"
    assert safe_basename("a\nb.pdf") == "ab.pdf"
    assert safe_basename("  name.pdf") == "  name.pdf"


# --------------------------------------------------------------- looks_like_pdf


def test_looks_like_pdf_true_for_magic_bytes(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.7\n1 0 obj")
    assert looks_like_pdf(pdf)


def test_looks_like_pdf_false_for_non_pdf(tmp_path):
    archive = tmp_path / "doc.zip"
    archive.write_bytes(b"PK\x03\x04...")
    assert not looks_like_pdf(archive)

    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    assert not looks_like_pdf(empty)


def test_looks_like_pdf_only_scans_first_1024_bytes(tmp_path):
    near = tmp_path / "near.pdf"
    near.write_bytes(b"x" * 1019 + b"%PDF-1.7")
    assert looks_like_pdf(near)

    straddling = tmp_path / "straddling.pdf"
    straddling.write_bytes(b"x" * 1020 + b"%PDF-1.7")
    assert not looks_like_pdf(straddling)

    far = tmp_path / "far.pdf"
    far.write_bytes(b"x" * 1024 + b"%PDF-1.7")
    assert not looks_like_pdf(far)


# ----------------------------------------------------------------- save_upload


def test_save_upload_writes_bytes(tmp_path):
    target = tmp_path / "out.pdf"
    count = save_upload(make_file(b"hello world"), target, max_bytes=1024)
    assert count == 11
    assert target.read_bytes() == b"hello world"


def test_save_upload_streams_more_than_one_chunk(tmp_path):
    data = b"a" * (64 * 1024 + 17)
    target = tmp_path / "big.pdf"
    count = save_upload(make_file(data), target, max_bytes=len(data))
    assert count == len(data)
    assert target.read_bytes() == data


def test_save_upload_accepts_exactly_max_bytes(tmp_path):
    data = b"x" * 1024
    target = tmp_path / "ok.pdf"
    count = save_upload(make_file(data), target, max_bytes=1024)
    assert count == 1024
    assert target.read_bytes() == data


def test_save_upload_raises_when_over_limit(tmp_path):
    target = tmp_path / "big.pdf"
    with pytest.raises(UploadTooLarge):
        save_upload(make_file(b"x" * 100), target, max_bytes=10)


def test_save_upload_too_large_is_a_value_error(tmp_path):
    with pytest.raises(ValueError):
        save_upload(make_file(b"x" * 100), tmp_path / "big.pdf", max_bytes=10)


# -------------------------------------------------------------- ensure_data_dirs


def test_ensure_data_dirs_creates_uploads_and_jobs(tmp_path):
    data_dir = tmp_path / "data"
    ensure_data_dirs(data_dir)
    assert (data_dir / "uploads").is_dir()
    assert (data_dir / "jobs").is_dir()


def test_ensure_data_dirs_is_idempotent(tmp_path):
    data_dir = tmp_path / "data"
    ensure_data_dirs(data_dir)
    ensure_data_dirs(data_dir)
    assert (data_dir / "uploads").is_dir()
    assert (data_dir / "jobs").is_dir()


# ---------------------------------------------------------------- list_artifacts


def test_list_artifacts_returns_docx_and_report(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "02003L0087-20240301_tracked.docx").write_bytes(b"x")
    (job_dir / "report.md").write_text("# report")
    (job_dir / "a2t.log").write_text("log")
    assert list_artifacts(job_dir) == [
        job_dir / "02003L0087-20240301_tracked.docx",
        job_dir / "report.md",
    ]


def test_list_artifacts_sorts_docx_names(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "zz.docx").write_bytes(b"x")
    (job_dir / "aa.docx").write_bytes(b"x")
    assert [p.name for p in list_artifacts(job_dir)] == ["aa.docx", "zz.docx"]


def test_list_artifacts_allows_report_without_docx(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "report.md").write_text("# report")
    assert list_artifacts(job_dir) == [job_dir / "report.md"]


def test_list_artifacts_empty_for_missing_or_empty_dir(tmp_path):
    assert list_artifacts(tmp_path / "nope") == []

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "a2t.log").write_text("log")
    assert list_artifacts(empty) == []


# ------------------------------------------------------------- resolve_artifact


def test_resolve_artifact_returns_resolved_file(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    report = job_dir / "report.md"
    report.write_text("# report")
    assert resolve_artifact(job_dir, "report.md") == report.resolve()


def test_resolve_artifact_none_for_missing_file(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    assert resolve_artifact(job_dir, "nope.md") is None


def test_resolve_artifact_blocks_parent_traversal(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    assert resolve_artifact(job_dir, "../secret.txt") is None
    assert resolve_artifact(job_dir, "..\\secret.txt") is None


def test_resolve_artifact_blocks_absolute_and_empty_names(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    assert resolve_artifact(job_dir, "/etc/passwd") is None
    assert resolve_artifact(job_dir, "") is None


def test_resolve_artifact_does_not_return_directories(tmp_path):
    job_dir = tmp_path / "job"
    (job_dir / "sub").mkdir(parents=True)
    assert resolve_artifact(job_dir, "sub") is None