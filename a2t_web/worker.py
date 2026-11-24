"""Background worker: runs `a2t convert` as a subprocess for queued jobs.

The pipeline runs in a child process so it can be killed (cancel/timeout), its
stdout+stderr land in ``jobs/<id>/a2t.log``, and its memory (polars, network
fetches) never touches the web process.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from a2t.cli import EXIT_PARTIAL
from a2t_web import pipeline, source, storage
from a2t_web.config import WebSettings
from a2t_web.jobs import CANCELLED, DONE, FAILED, RUNNING, Job, JobRegistry
from a2t_web.sanity import SanityError, sanity_check

CANCEL_GRACE_SECONDS = 5.0
CLEANUP_INTERVAL_MINIMUM = 3600.0


def _terminate(proc: subprocess.Popen) -> None:
    """Terminate the pipeline's whole process group.

    `a2t convert` spawns children (pdftotext); the subprocess is started with
    `start_new_session=True`, so its pid is its group id and a group signal
    reaches every descendant.
    """
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=CANCEL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def _failure_error(log_path: Path, returncode: int) -> str:
    log_tail = pipeline.tail(log_path)
    if log_tail:
        return log_tail
    return f"a2t convert exited with code {returncode}"


class Worker:
    def __init__(self, registry: JobRegistry, settings: WebSettings) -> None:
        self.registry = registry
        self.settings = settings
        self._executor = ThreadPoolExecutor(max_workers=settings.max_workers)
        self._cleanup_stop = threading.Event()
        interval = max(CLEANUP_INTERVAL_MINIMUM, settings.job_ttl_hours * 3600 / 2)
        self._cleanup = threading.Thread(
            target=self._cleanup_loop,
            args=(interval,),
            daemon=True,
            name="a2t-web-cleanup",
        )
        self._cleanup.start()

    def submit(self, job: Job) -> None:
        self._executor.submit(self._run, job.id)

    def _run(self, job_id: str) -> None:
        job = self.registry.get(job_id)
        if job is None:
            return
        if self.registry.was_cancelled(job_id):
            self.registry.finalize(job_id, status=CANCELLED)
            return
        self.registry.transition(job_id, status=RUNNING)
        if self.registry.was_cancelled(job_id):
            self.registry.finalize(job_id, status=CANCELLED)
            return
        job.outdir.mkdir(parents=True, exist_ok=True)
        log_path = job.outdir / "a2t.log"
        proc: subprocess.Popen | None = None
        try:
            if job.celex:
                problem = self._obtain(job, log_path)
                if self.registry.was_cancelled(job_id):
                    job.pdf_path.unlink(missing_ok=True)
                    self.registry.finalize(job_id, status=CANCELLED)
                    return
                if problem is not None:
                    job.pdf_path.unlink(missing_ok=True)
                    self.registry.finalize(job_id, status=FAILED, error=problem)
                    return
            command = pipeline.build_command(
                job.pdf_path, job.outdir, self.settings.cache_dir)
            with log_path.open("ab") as log_file:
                proc = subprocess.Popen(
                    command,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                self.registry.set_proc(job_id, proc)
                if self.registry.was_cancelled(job_id):
                    _terminate(proc)
                    self.registry.set_proc(job_id, None)
                    self.registry.finalize(job_id, status=CANCELLED)
                    return
                try:
                    returncode = proc.wait(timeout=self.settings.job_timeout)
                except subprocess.TimeoutExpired:
                    _terminate(proc)
                    self.registry.set_proc(job_id, None)
                    if self.registry.was_cancelled(job_id):
                        self.registry.finalize(job_id, status=CANCELLED)
                    else:
                        self.registry.finalize(
                            job_id,
                            status=FAILED,
                            error=f"timed out after {self.settings.job_timeout}s",
                        )
                    return
                self.registry.set_proc(job_id, None)
            if self.registry.was_cancelled(job_id):
                self.registry.finalize(job_id, status=CANCELLED)
            elif returncode == 0:
                self.registry.finalize(job_id, status=DONE)
            elif returncode == EXIT_PARTIAL:
                # Some acts converted and others could not be. The documents and the
                # report exist, and the report lists what was left out, so the job is
                # done — with a note, rather than failed with its results hidden.
                self.registry.finalize(
                    job_id, status=DONE,
                    error="Some acts could not be converted — see the report and the log.")
            else:
                self.registry.finalize(
                    job_id,
                    status=FAILED,
                    error=_failure_error(log_path, returncode),
                )
        except Exception as exc:
            if proc is not None and proc.poll() is None:
                _terminate(proc)
            self.registry.set_proc(job_id, None)
            self.registry.finalize(job_id, status=FAILED, error=str(exc))

    def _obtain(self, job: Job, log_path: Path) -> str | None:
        """Fetch a linked document and give it the checks an upload gets at the door.

        Runs here, not in the request: fetching can take tens of seconds, and what
        happens to it is written to the job's log, which the status page already shows.
        Returns the reason it cannot be converted, or None when `job.pdf_path` is ready.
        A fetch cannot be interrupted, so a cancel is looked for after it returns; it is
        bounded by the fetcher's own timeouts.
        """

        def note(message: str) -> None:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")

        note(f"Fetching {job.celex} from EUR-Lex…")
        try:
            source.fetch_pdf(job.celex, job.pdf_path, self.settings.data_dir)
        except source.SourceError as exc:
            note(str(exc))
            return str(exc)
        if job.pdf_path.stat().st_size > self.settings.max_upload_bytes:
            return (f"That document is larger than the limit "
                    f"({self.settings.max_upload_bytes} bytes).")
        if not storage.looks_like_pdf(job.pdf_path):
            return "This does not look like a PDF file."
        note("Fetched; checking that it amends something…")
        try:
            sanity_check(job.pdf_path)
        except SanityError as exc:
            note(str(exc))
            return str(exc)
        note("Converting…")
        return None

    def cancel(self, job_id: str) -> bool:
        """Cancel a queued/running job; False for unknown or finished jobs.

        Marks the job CANCELLED right away; a cancelled queued job never flips
        through RUNNING afterwards.
        """
        initiated, proc = self.registry.request_cancel(job_id)
        if not initiated:
            return False
        if proc is not None:
            _terminate(proc)
        self.registry.finalize(job_id, status=CANCELLED)
        return True

    def sweep(self) -> None:
        self.registry.sweep(time.time(), self.settings.job_ttl_hours * 3600)

    def shutdown(self) -> None:
        """Stop the cleanup loop and kill any still-running pipeline processes."""
        self._cleanup_stop.set()
        self._cleanup.join(timeout=1.0)
        self._executor.shutdown(wait=False, cancel_futures=True)
        for proc in self.registry.active_procs():
            if proc.poll() is None:
                _terminate(proc)

    def _cleanup_loop(self, interval: float) -> None:
        self.sweep()
        while not self._cleanup_stop.wait(interval):
            self.sweep()