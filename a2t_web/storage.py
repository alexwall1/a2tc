"""Disk layout and upload handling for the web app.

Uploads are written immediately to ``uploads/<job_id>.pdf``; the original filename
is kept only as display metadata. Job artifacts live in ``jobs/<job_id>/`` and are
listed by scanning the directory, never from a cached list.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024

CHUNK_SIZE = 64 * 1024
MAGIC_SCAN_BYTES = 1024
PDF_MAGIC = b"%PDF-"


class UploadTooLarge(ValueError):
    pass


def safe_basename(name: str) -> str:
    """Display name for an uploaded file: no path components, no control chars."""
    cleaned = name.replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(ch for ch in cleaned if ch.isprintable() and ch not in "\x00\r\n\t")


def looks_like_pdf(path: Path) -> bool:
    with path.open("rb") as handle:
        head = handle.read(MAGIC_SCAN_BYTES)
    return PDF_MAGIC in head


def save_upload(file_storage, pdf_path: Path, max_bytes: int) -> int:
    """Stream `file_storage` (Flask-style, exposing `.stream`) to `pdf_path`.

    Aborts with `UploadTooLarge` once the byte count exceeds `max_bytes`; the
    partially written file is left for the caller to clean up.
    """
    total = 0
    with pdf_path.open("wb") as out:
        while True:
            chunk = file_storage.stream.read(CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise UploadTooLarge(f"upload exceeds {max_bytes} bytes")
            out.write(chunk)
    return total


def ensure_data_dirs(data_dir: Path) -> None:
    (data_dir / "uploads").mkdir(parents=True, exist_ok=True)
    (data_dir / "jobs").mkdir(parents=True, exist_ok=True)


def list_artifacts(job_dir: Path) -> list[Path]:
    """Everything a finished job produced: tracked-changes docx files and the report."""
    if not job_dir.is_dir():
        return []
    artifacts: list[Path] = sorted(job_dir.glob("*.docx"))
    report = job_dir / "report.md"
    if report.is_file():
        artifacts.append(report)
    return artifacts


def resolve_artifact(job_dir: Path, name: str) -> Path | None:
    """A download path inside `job_dir`, or None for missing or escaping names."""
    base = job_dir.resolve()
    candidate = (base / name).resolve()
    if not candidate.is_relative_to(base):
        return None
    if not candidate.is_file():
        return None
    return candidate