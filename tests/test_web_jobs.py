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