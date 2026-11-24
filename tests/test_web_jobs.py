"""Job registry and worker lifecycle, driven by fake subprocess commands.

`a2t_web.worker.pipeline.build_command` is monkeypatched on the module to
return `[sys.executable, "-c", SCRIPT, "<outdir>"]`; the script receives the
job's outdir as its first argument.
"""

import os
import sys
import time
from pathlib import Path

import pytest

import a2t_web.worker as worker_module
from a2t_web.config import WebSettings
from a2t_web.db import Database
from a2t_web.jobs import CANCELLED, DONE, FAILED, QUEUED, RUNNING, Job, JobRegistry
from a2t_web.pipeline import tail

WRITE_REPORT_SCRIPT = (
    "import pathlib, sys;"
    "out = pathlib.Path(sys.argv[1]);"
    "out.mkdir(parents=True, exist_ok=True);"
    "(out / 'report.md').write_text('# report');"
    "(out / '02003L0087-20240301_tracked.docx').write_bytes(b'PK');"
)

# Exits 0 having written a report but no Word document: what an older `convert` did when
# every act failed to retrieve.
NO_DOCUMENT_SCRIPT = (
    "import pathlib, sys;"
    "out = pathlib.Path(sys.argv[1]);"
    "out.mkdir(parents=True, exist_ok=True);"
    "(out / 'report.md').write_text('# report');"
    "print('Directive 2003/87/EC: SKIPPED — retrieval failed');"
)

FAIL_SCRIPT = (
    "import sys;"
    "print('boom', file=sys.stderr);"
    "sys.exit(1);"
)

SLEEP_SCRIPT = (
    "import pathlib, sys, time;"
    "out = pathlib.Path(sys.argv[1]);"
    "out.mkdir(parents=True, exist_ok=True);"
    "time.sleep(60);"
)


def fake_build(script: str):
    def _build(pdf, outdir, cache, author="A2T (web)"):
        return [sys.executable, "-c", script, str(outdir)]

    return _build


def wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met before timeout")


@pytest.fixture
def settings(tmp_path):
    return WebSettings(
        data_dir=tmp_path,
        cache_dir=tmp_path / "cache",
        max_workers=1,
        job_timeout=3600,
        job_ttl_hours=24,
    )


@pytest.fixture
def registry():
    return JobRegistry()


def make_job(
    registry: JobRegistry,
    tmp_path: Path,
    job_id: str = "j1",
    pdf_name: str = "COM(2026) 1 final.pdf",
) -> Job:
    return registry.create(
        job_id, tmp_path / f"{job_id}.pdf", tmp_path / "jobs" / job_id, pdf_name
    )


def make_worker(registry: JobRegistry, settings: WebSettings) -> worker_module.Worker:
    return worker_module.Worker(registry, settings)


# ---------------------------------------------------------------- registry


def test_registry_create_and_get(registry, tmp_path):
    job = make_job(registry, tmp_path)
    assert registry.get(job.id) is job
    assert job.status == QUEUED
    assert job.error is None
    assert job.started_at is None
    assert job.finished_at is None
    assert job.cancelled is False


def test_registry_get_unknown_returns_none(registry):
    assert registry.get("nope") is None
    assert registry.state_snapshot("nope") is None
    assert registry.position("nope") is None
    assert registry.job_dir("nope") is None


def test_registry_state_snapshot_is_jsonable(registry, tmp_path):
    job = make_job(registry, tmp_path)
    snapshot = registry.state_snapshot(job.id)
    assert set(snapshot) == {
        "id",
        "status",
        "error",
        "pdf_name",
        "queue_position",
        "created_at",
        "started_at",
        "finished_at",
    }
    assert snapshot["id"] == job.id
    assert snapshot["status"] == QUEUED
    assert snapshot["pdf_name"] == "COM(2026) 1 final.pdf"
    assert isinstance(snapshot["created_at"], float)


def test_registry_transition_running_sets_started_at(registry, tmp_path):
    job = make_job(registry, tmp_path)
    registry.transition(job.id, status=RUNNING)
    assert job.status == RUNNING
    assert job.started_at is not None
    assert job.finished_at is None


def test_registry_transition_terminal_sets_finished_at(registry, tmp_path):
    job = make_job(registry, tmp_path)
    registry.transition(job.id, status=RUNNING)
    registry.transition(job.id, status=DONE)
    assert job.status == DONE
    assert job.started_at is not None
    assert job.finished_at is not None
    assert job.finished_at >= job.started_at


def test_registry_transition_sets_error(registry, tmp_path):
    job = make_job(registry, tmp_path)
    registry.transition(job.id, status=FAILED, error="something broke")
    assert job.status == FAILED
    assert job.error == "something broke"


def test_registry_transition_unknown_is_noop(registry):
    registry.transition("nope", status=RUNNING)
    assert registry.get("nope") is None


def test_registry_finalize_marks_terminal(registry, tmp_path):
    job = make_job(registry, tmp_path)
    assert registry.finalize(job.id, status=CANCELLED) == CANCELLED
    assert job.status == CANCELLED
    assert job.finished_at is not None


def test_registry_finalize_refuses_to_overwrite_terminal_state(registry, tmp_path):
    job = make_job(registry, tmp_path)
    registry.transition(job.id, status=RUNNING)
    registry.finalize(job.id, status=DONE)
    assert job.status == DONE
    assert registry.finalize(job.id, status=CANCELLED) == DONE
    assert job.status == DONE


def test_registry_finalize_sets_error(registry, tmp_path):
    job = make_job(registry, tmp_path)
    assert registry.finalize(job.id, status=FAILED, error="boom") == FAILED
    assert job.error == "boom"


def test_registry_queue_position_fifo(registry, tmp_path):
    job1 = make_job(registry, tmp_path, job_id="one")
    job2 = make_job(registry, tmp_path, job_id="two")
    assert registry.position(job1.id) == 0
    assert registry.position(job2.id) == 1
    assert registry.state_snapshot(job2.id)["queue_position"] == 1

    registry.transition(job1.id, status=RUNNING)
    assert registry.position(job1.id) is None
    assert registry.position(job2.id) == 1

    registry.transition(job1.id, status=DONE)
    assert registry.position(job2.id) == 0


def test_registry_job_dir(registry, tmp_path):
    job = make_job(registry, tmp_path)
    assert registry.job_dir(job.id) == tmp_path / "jobs" / job.id


# ------------------------------------------------------------ db persistence


def make_db_registry(tmp_path: Path, name: str = "a2t.db"):
    db = Database(tmp_path / name)
    db.init()
    return JobRegistry(db=db), db


def test_registry_create_persists_row(tmp_path):
    registry, db = make_db_registry(tmp_path)
    make_job(registry, tmp_path, job_id="abc", pdf_name="COM.pdf")
    rows = db.list_jobs()
    assert len(rows) == 1
    assert rows[0]["job_id"] == "abc"
    assert rows[0]["pdf_name"] == "COM.pdf"
    assert rows[0]["status"] == QUEUED
    assert rows[0]["finished_at"] is None


def test_registry_terminal_transition_persists(tmp_path):
    registry, db = make_db_registry(tmp_path)
    job = make_job(registry, tmp_path, job_id="abc")
    registry.transition(job.id, status=RUNNING)
    registry.transition(job.id, status=DONE)
    # RUNNING transitions are not persisted, but a terminal transition must
    # carry the job's started_at into the durable row so /admin can show it
    assert job.started_at is not None
    assert job.finished_at is not None
    rows = db.list_jobs()
    assert len(rows) == 1
    assert rows[0]["status"] == DONE
    assert rows[0]["started_at"] == job.started_at
    assert rows[0]["finished_at"] == job.finished_at


def test_registry_finalize_persists_error(tmp_path):
    registry, db = make_db_registry(tmp_path)
    job = make_job(registry, tmp_path, job_id="abc")
    registry.finalize(job.id, status=FAILED, error="boom")
    rows = db.list_jobs()
    assert len(rows) == 1
    assert rows[0]["status"] == FAILED
    assert rows[0]["error"] == "boom"


def test_registry_cancel_queued_job_persists_null_started_at(tmp_path):
    registry, db = make_db_registry(tmp_path)
    job = make_job(registry, tmp_path, job_id="abc")
    registry.finalize(job.id, status=CANCELLED)  # cancelled while still queued
    rows = db.list_jobs()
    assert len(rows) == 1
    assert rows[0]["status"] == CANCELLED
    assert rows[0]["started_at"] is None
    assert rows[0]["finished_at"] is not None


def test_registry_finalize_early_return_does_not_overwrite_done(tmp_path):
    registry, db = make_db_registry(tmp_path)
    job = make_job(registry, tmp_path, job_id="abc")
    registry.finalize(job.id, status=DONE)
    registry.finalize(job.id, status=CANCELLED)  # early return: no second write
    rows = db.list_jobs()
    assert len(rows) == 1
    assert rows[0]["status"] == DONE
    assert rows[0]["error"] is None


def test_registry_no_db_writes_nothing(registry, tmp_path):
    make_job(registry, tmp_path)
    assert not (tmp_path / "a2t.db").exists()
    assert db_registry_files(tmp_path) == []


def db_registry_files(tmp_path: Path) -> list[Path]:
    return sorted(
        p for p in tmp_path.iterdir() if p.name.startswith("a2t.db")
    )


def test_registry_db_error_with_newlines_roundtrips(tmp_path):
    registry, db = make_db_registry(tmp_path)
    job = make_job(registry, tmp_path, job_id="abc")
    registry.finalize(job.id, status=FAILED, error="line1\nline2\tboom")
    rows = db.list_jobs()
    assert rows[0]["error"] == "line1\nline2\tboom"


def test_registry_running_transition_not_persisted(tmp_path):
    registry, db = make_db_registry(tmp_path)
    job = make_job(registry, tmp_path, job_id="abc")
    registry.transition(job.id, status=RUNNING)
    rows = db.list_jobs()
    assert len(rows) == 1
    # only the create row exists; the row's status never flipped from queued
    assert rows[0]["status"] == QUEUED
    assert rows[0]["started_at"] is None


# ---------------------------------------------------------------- worker: happy


def test_worker_happy_path_ends_done(tmp_path, monkeypatch, settings):
    monkeypatch.setattr(
        worker_module.pipeline, "build_command", fake_build(WRITE_REPORT_SCRIPT)
    )
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job = make_job(registry, tmp_path)
        worker.submit(job)
        wait_until(lambda: registry.get(job.id).status == DONE)
        assert (tmp_path / "jobs" / job.id / "report.md").is_file()
        snapshot = registry.state_snapshot(job.id)
        assert snapshot["status"] == DONE
        assert snapshot["finished_at"] is not None
    finally:
        worker.shutdown()


# --------------------------------------------------------------- worker: failure


def test_worker_failure_exit_1_marks_failed(tmp_path, monkeypatch, settings):
    monkeypatch.setattr(worker_module.pipeline, "build_command", fake_build(FAIL_SCRIPT))
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job = make_job(registry, tmp_path)
        worker.submit(job)
        wait_until(lambda: registry.get(job.id).status == FAILED)
        failed = registry.get(job.id)
        assert failed.status == FAILED
        assert failed.error
        assert "boom" in failed.error
    finally:
        worker.shutdown()


def test_worker_error_not_empty_when_log_empty(tmp_path, monkeypatch, settings):
    empty_fail = "import sys; sys.exit(1);"
    monkeypatch.setattr(worker_module.pipeline, "build_command", fake_build(empty_fail))
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job = make_job(registry, tmp_path)
        worker.submit(job)
        wait_until(lambda: registry.get(job.id).status == FAILED)
        assert registry.get(job.id).error
    finally:
        worker.shutdown()


# --------------------------------------------------------------- worker: timeout


def test_worker_timeout_marks_failed(tmp_path, monkeypatch):
    settings = WebSettings(
        data_dir=tmp_path,
        cache_dir=tmp_path / "cache",
        max_workers=1,
        job_timeout=0.1,
        job_ttl_hours=24,
    )
    monkeypatch.setattr(worker_module.pipeline, "build_command", fake_build(SLEEP_SCRIPT))
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job = make_job(registry, tmp_path)
        worker.submit(job)
        wait_until(lambda: registry.get(job.id).status == FAILED)
        timed_out = registry.get(job.id)
        assert timed_out.status == FAILED
        assert "timed out" in timed_out.error
    finally:
        worker.shutdown()


# ---------------------------------------------------------------- worker: cancel


def test_worker_cancel_running_job(tmp_path, monkeypatch, settings):
    monkeypatch.setattr(worker_module.pipeline, "build_command", fake_build(SLEEP_SCRIPT))
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job = make_job(registry, tmp_path)
        worker.submit(job)
        wait_until(
            lambda: registry.get(job.id).status == RUNNING
            and registry.get(job.id).proc is not None
        )
        assert worker.cancel(job.id) is True
        wait_until(lambda: registry.get(job.id).status == CANCELLED)
    finally:
        worker.shutdown()


def test_worker_cancel_queued_job(tmp_path, monkeypatch, settings):
    monkeypatch.setattr(worker_module.pipeline, "build_command", fake_build(SLEEP_SCRIPT))
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job1 = make_job(registry, tmp_path, job_id="one")
        job2 = make_job(registry, tmp_path, job_id="two")
        worker.submit(job1)
        worker.submit(job2)
        wait_until(
            lambda: registry.get(job1.id).status == RUNNING
            and registry.get(job1.id).proc is not None
        )
        assert registry.get(job2.id).status == QUEUED
        assert worker.cancel(job2.id) is True
        assert registry.get(job2.id).cancelled is True
        assert registry.get(job2.id).status == CANCELLED
        assert worker.cancel(job1.id) is True
        wait_until(lambda: registry.get(job1.id).status == CANCELLED)
        # the cancelled queued job never runs the pipeline
        assert registry.get(job2.id).status == CANCELLED
        assert registry.get(job2.id).started_at is None
    finally:
        worker.shutdown()


def test_worker_cancel_immediately_after_submit(tmp_path, monkeypatch, settings):
    """Cancelling right after submit must not leave the pipeline running.

    Guards the race between the pre-spawn `was_cancelled` check and
    `set_proc`: a cancel landing in that window used to miss the subprocess,
    which then ran to completion (sleeping 60 s) while holding the single
    worker slot.
    """
    monkeypatch.setattr(worker_module.pipeline, "build_command", fake_build(SLEEP_SCRIPT))
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job = make_job(registry, tmp_path)
        worker.submit(job)
        assert worker.cancel(job.id) is True
        wait_until(lambda: registry.get(job.id).status == CANCELLED, timeout=2.0)
        time.sleep(0.2)
        assert registry.get(job.id).status == CANCELLED
        assert registry.get(job.id).proc is None
    finally:
        worker.shutdown()


def test_worker_cancel_finished_job_returns_false(tmp_path, monkeypatch, settings):
    monkeypatch.setattr(
        worker_module.pipeline, "build_command", fake_build(WRITE_REPORT_SCRIPT)
    )
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job = make_job(registry, tmp_path)
        worker.submit(job)
        wait_until(lambda: registry.get(job.id).status == DONE)
        assert worker.cancel(job.id) is False
    finally:
        worker.shutdown()


def test_worker_cancel_unknown_job_returns_false(settings):
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        assert worker.cancel("nope") is False
    finally:
        worker.shutdown()


# ------------------------------------------------------- worker: queue position


def test_worker_queue_position_while_first_runs(tmp_path, monkeypatch, settings):
    monkeypatch.setattr(worker_module.pipeline, "build_command", fake_build(SLEEP_SCRIPT))
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job1 = make_job(registry, tmp_path, job_id="one")
        job2 = make_job(registry, tmp_path, job_id="two")
        worker.submit(job1)
        worker.submit(job2)
        wait_until(lambda: registry.get(job1.id).status == RUNNING)
        assert registry.get(job2.id).status == QUEUED
        assert registry.position(job1.id) is None
        assert registry.position(job2.id) == 1
        assert registry.state_snapshot(job2.id)["queue_position"] == 1
    finally:
        worker.cancel(job1.id)
        worker.cancel(job2.id)
        worker.shutdown()


# ----------------------------------------------------------------- pipeline.tail


def test_tail_returns_last_lines(tmp_path):
    log = tmp_path / "a2t.log"
    log.write_text("a\nb\nc\n")
    assert tail(log) == "a\nb\nc\n"
    assert tail(log, 2) == "b\nc\n"


def test_tail_large_log_returns_only_last_n(tmp_path):
    log = tmp_path / "a2t.log"
    log.write_text("".join(f"line {i}\n" for i in range(1000)))
    assert tail(log, 5) == "line 995\nline 996\nline 997\nline 998\nline 999\n"
    assert tail(log, 200) == "".join(f"line {i}\n" for i in range(800, 1000))


def test_tail_preserves_final_line_without_newline(tmp_path):
    log = tmp_path / "a2t.log"
    log.write_text("a\nb\nc")
    assert tail(log, 2) == "b\nc"
    assert tail(log, 200) == "a\nb\nc"


def test_tail_zero_or_negative_returns_empty(tmp_path):
    log = tmp_path / "a2t.log"
    log.write_text("a\nb\n")
    assert tail(log, 0) == ""
    assert tail(log, -1) == ""


def test_tail_missing_file_returns_empty(tmp_path):
    assert tail(tmp_path / "nope.log") == ""


# ------------------------------------------------------------------------ sweep


def test_registry_sweep_removes_expired_job(tmp_path):
    registry = JobRegistry()
    job_dir = tmp_path / "jobs" / "expired"
    job_dir.mkdir(parents=True)
    job = registry.create("expired", tmp_path / "x.pdf", job_dir, "x.pdf")
    old = time.time() - 1000
    os.utime(job_dir, (old, old))
    removed = registry.sweep(time.time(), ttl=100)
    assert removed == ["expired"]
    assert registry.get("expired") is None
    assert not job_dir.exists()


def test_registry_sweep_removes_upload_file(tmp_path):
    registry = JobRegistry()
    job_dir = tmp_path / "jobs" / "expired"
    job_dir.mkdir(parents=True)
    pdf_path = tmp_path / "uploads" / "expired.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4")
    registry.create("expired", pdf_path, job_dir, "expired.pdf")
    old = time.time() - 1000
    os.utime(job_dir, (old, old))
    assert registry.sweep(time.time(), ttl=100) == ["expired"]
    assert not pdf_path.exists()


def test_registry_sweep_keeps_running_job(tmp_path):
    registry = JobRegistry()
    job_dir = tmp_path / "jobs" / "running"
    job_dir.mkdir(parents=True)
    job = registry.create("running", tmp_path / "x.pdf", job_dir, "x.pdf")
    registry.transition(job.id, status=RUNNING)
    old = time.time() - 1000
    os.utime(job_dir, (old, old))
    assert registry.sweep(time.time(), ttl=100) == []
    assert registry.get(job.id) is not None
    assert job_dir.exists()


def test_registry_sweep_keeps_fresh_job(tmp_path):
    registry = JobRegistry()
    job = make_job(registry, tmp_path)
    (tmp_path / "jobs" / job.id).mkdir(parents=True)
    assert registry.sweep(time.time(), ttl=100) == []
    assert registry.get(job.id) is not None


def test_worker_sweep_delegates_to_registry(tmp_path):
    short_ttl_settings = WebSettings(
        data_dir=tmp_path,
        cache_dir=tmp_path / "cache",
        max_workers=1,
        job_timeout=3600,
        job_ttl_hours=0.1,
    )
    registry = JobRegistry()
    worker = make_worker(registry, short_ttl_settings)
    try:
        job_dir = tmp_path / "jobs" / "old"
        job_dir.mkdir(parents=True)
        job = registry.create("old", tmp_path / "x.pdf", job_dir, "x.pdf")
        old = time.time() - 1000
        os.utime(job_dir, (old, old))
        registry.transition(job.id, status=DONE)
        worker.sweep()
        assert registry.get(job.id) is None
        assert not job_dir.exists()
    finally:
        worker.shutdown()

# ----------------------------------------------- worker: a document linked, not uploaded


PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\n%%EOF\n"


def make_link_job(registry, tmp_path, celex="52023PC0702"):
    return registry.create("l1", tmp_path / "uploads" / "l1.pdf", tmp_path / "jobs" / "l1",
                           f"{celex}.pdf", celex=celex)


@pytest.fixture
def link(monkeypatch):
    """Fake EUR-Lex: records fetches; the sanity check passes unless told otherwise."""
    state = {"fetched": [], "write": PDF_BYTES, "error": None, "sanity": None}

    def fetch_pdf(celex, destination, data_dir):
        state["fetched"].append(celex)
        if state["error"]:
            raise worker_module.source.SourceError(state["error"], status=502)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(state["write"])

    def sanity(pdf):
        if state["sanity"]:
            raise worker_module.SanityError(state["sanity"])

    monkeypatch.setattr(worker_module.source, "fetch_pdf", fetch_pdf)
    monkeypatch.setattr(worker_module, "sanity_check", sanity)
    monkeypatch.setattr(worker_module.pipeline, "build_command",
                        fake_build(WRITE_REPORT_SCRIPT))
    return state


def test_a_linked_document_is_fetched_checked_and_converted(tmp_path, settings, link):
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job = make_link_job(registry, tmp_path)
        worker.submit(job)
        wait_until(lambda: registry.get(job.id).status == DONE)

        assert link["fetched"] == ["52023PC0702"]
        assert job.pdf_path.read_bytes() == PDF_BYTES
        log = (tmp_path / "jobs" / "l1" / "a2t.log").read_text()
        assert "Fetching 52023PC0702 from EUR-Lex" in log
        assert "Converting" in log
    finally:
        worker.shutdown()


def test_a_fetch_that_fails_fails_the_job_with_the_reason(tmp_path, settings, link):
    link["error"] = "EUR-Lex did not supply a PDF for 52023PC0702."
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job = make_link_job(registry, tmp_path)
        worker.submit(job)
        wait_until(lambda: registry.get(job.id).status == FAILED)

        assert "did not supply a PDF" in registry.get(job.id).error
        assert not job.pdf_path.exists()
        assert not (tmp_path / "jobs" / "l1" / "report.md").exists()  # no pipeline ran
    finally:
        worker.shutdown()


def test_a_fetched_document_that_amends_nothing_fails_the_job(tmp_path, settings, link):
    link["sanity"] = "This document amends only annexes of Directive 2003/87/EC."
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job = make_link_job(registry, tmp_path)
        worker.submit(job)
        wait_until(lambda: registry.get(job.id).status == FAILED)

        assert "only annexes" in registry.get(job.id).error
        assert not job.pdf_path.exists()
    finally:
        worker.shutdown()


def test_a_fetched_file_that_is_not_a_pdf_fails_the_job(tmp_path, settings, link):
    link["write"] = b"<html>Please wait</html>"
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job = make_link_job(registry, tmp_path)
        worker.submit(job)
        wait_until(lambda: registry.get(job.id).status == FAILED)

        assert "does not look like a PDF" in registry.get(job.id).error
    finally:
        worker.shutdown()


def test_a_fetched_document_over_the_size_limit_fails_the_job(tmp_path, link):
    settings = WebSettings(data_dir=tmp_path, cache_dir=tmp_path / "cache", max_workers=1,
                           job_timeout=3600, job_ttl_hours=24, max_upload_bytes=16)
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job = make_link_job(registry, tmp_path)
        worker.submit(job)
        wait_until(lambda: registry.get(job.id).status == FAILED)

        assert "larger than the limit" in registry.get(job.id).error
    finally:
        worker.shutdown()


def test_a_job_cancelled_before_it_starts_fetches_nothing(tmp_path, settings, link):
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job = make_link_job(registry, tmp_path)
        registry.request_cancel(job.id)
        worker.submit(job)
        wait_until(lambda: registry.get(job.id).status == CANCELLED)

        assert link["fetched"] == []
    finally:
        worker.shutdown()


def test_an_uploaded_document_is_not_fetched_or_rechecked_by_the_worker(
        tmp_path, settings, link):
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job = make_job(registry, tmp_path)           # no celex: an upload
        worker.submit(job)
        wait_until(lambda: registry.get(job.id).status == DONE)

        assert link["fetched"] == []
    finally:
        worker.shutdown()


PARTIAL_SCRIPT = (
    "import pathlib, sys;"
    "out = pathlib.Path(sys.argv[1]);"
    "out.mkdir(parents=True, exist_ok=True);"
    "(out / 'report.md').write_text('# report');"
    "(out / '01992L0106-20130701_tracked.docx').write_bytes(b'PK');"
    "print('NOT CONVERTED  Regulation (EU) 2020/1056: could not list versions');"
    "sys.exit(3)"
)


def test_a_partial_conversion_is_done_with_a_note_not_failed(tmp_path, monkeypatch, settings):
    """`convert` exits 3 when some acts converted and others did not."""
    monkeypatch.setattr(worker_module.pipeline, "build_command", fake_build(PARTIAL_SCRIPT))
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job = make_job(registry, tmp_path)
        worker.submit(job)
        wait_until(lambda: registry.get(job.id).status == DONE)

        snapshot = registry.state_snapshot(job.id)
        assert snapshot["status"] == DONE
        assert "could not be converted" in snapshot["error"]
        assert (tmp_path / "jobs" / job.id / "report.md").is_file()
    finally:
        worker.shutdown()


def test_the_exit_status_for_a_partial_conversion_is_not_clicks_usage_error():
    from a2t.cli import EXIT_PARTIAL

    assert EXIT_PARTIAL == 3


def test_a_run_that_exits_cleanly_with_no_word_document_is_failed(tmp_path, monkeypatch, settings):
    """A job with nothing to download is not "done" — and it says why."""
    monkeypatch.setattr(worker_module.pipeline, "build_command",
                        fake_build(NO_DOCUMENT_SCRIPT))
    registry = JobRegistry()
    worker = make_worker(registry, settings)
    try:
        job = make_job(registry, tmp_path)
        worker.submit(job)
        wait_until(lambda: registry.get(job.id).status == FAILED)

        error = registry.get(job.id).error
        assert "produced no Word document" in error
        assert "SKIPPED — retrieval failed" in error   # the log's own words are kept
    finally:
        worker.shutdown()
