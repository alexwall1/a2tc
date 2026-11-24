"""In-memory, lock-guarded registry of conversion jobs.

The registry is the single source of truth for job state. The worker mutates
state only through these methods; reads copy the relevant fields under the
same lock so status polls never see a torn transition.

When constructed with a ``Database``, the registry mirrors every job create
and every terminal transition into the SQLite ``jobs`` table so the admin
log survives restarts and registry sweeps; without one it writes nothing.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from a2t_web.db import Database

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

ACTIVE_STATUSES = frozenset({QUEUED, RUNNING})
TERMINAL_STATUSES = frozenset({DONE, FAILED, CANCELLED})


@dataclass
class Job:
    id: str
    pdf_path: Path
    outdir: Path
    pdf_name: str
    status: str = QUEUED
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    proc: subprocess.Popen | None = field(default=None, repr=False)
    cancelled: bool = False
    # Set when the document is to be fetched from EUR-Lex by the worker rather than
    # having been uploaded; `pdf_path` is then where the fetched file will be put.
    celex: str | None = None


class JobRegistry:
    def __init__(self, db: Database | None = None) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._db = db

    def _persist(self, job: Job) -> None:
        """Mirror one state change into SQLite when a database is configured.

        Called while holding ``self._lock`` so writes cannot interleave with
        other state mutations; the record is built after the mutation so it
        carries the authoritative status/timestamps. Only the create and
        terminal states are stored: RUNNING transitions are not persisted
        (kept small, mirrors the old joblog contract).
        """
        if self._db is None:
            return
        if job.status in TERMINAL_STATUSES:
            self._db.set_job_terminal(
                job.id, job.status, job.error, job.started_at, job.finished_at
            )
        else:
            self._db.insert_job(job)

    def create(self, job_id: str, pdf_path: Path, outdir: Path, pdf_name: str,
               celex: str | None = None) -> Job:
        job = Job(id=job_id, pdf_path=pdf_path, outdir=outdir, pdf_name=pdf_name,
                  celex=celex)
        with self._lock:
            self._jobs[job_id] = job
            self._persist(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def state_snapshot(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return {
                "id": job.id,
                "status": job.status,
                "error": job.error,
                "pdf_name": job.pdf_name,
                "queue_position": self._position_locked(job),
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
            }

    def transition(
        self, job_id: str, *, status: str, error: str | None = None
    ) -> None:
        """Move a job to `status`, setting `error` and timestamps as appropriate.

        Only terminal transitions are persisted (running is not) so the
        durable record stays small and matches the "create and terminal"
        contract.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = status
            if error is not None:
                job.error = error
            now = time.time()
            if status == RUNNING and job.started_at is None:
                job.started_at = now
            if status in TERMINAL_STATUSES:
                job.finished_at = now
                self._persist(job)

    def finalize(self, job_id: str, *, status: str, error: str | None = None) -> str:
        """Write a terminal state unless the worker already recorded one.

        Returns the authoritative status: the requested `status` when the
        transition happened, otherwise the terminal state already on the job.
        Never moves a job away from a terminal state, so a late cancel cannot
        hide a finished job's artifacts. The early-return for an already
        terminal job persists nothing, so a late cancel cannot double-write.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return status
            if job.status in TERMINAL_STATUSES:
                return job.status
            job.status = status
            if error is not None:
                job.error = error
            job.finished_at = time.time()
            self._persist(job)
            return status

    def position(self, job_id: str) -> int | None:
        """0-based place in line for a queued job; None for any other state."""
        with self._lock:
            return self._position_locked(self._jobs.get(job_id))

    def _position_locked(self, job: Job | None) -> int | None:
        if job is None or job.status != QUEUED:
            return None
        ahead = 0
        for other in self._jobs.values():
            if other is job:
                break
            if other.status in ACTIVE_STATUSES:
                ahead += 1
        return ahead

    def set_proc(self, job_id: str, proc: subprocess.Popen | None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.proc = proc

    def was_cancelled(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            return job.cancelled

    def request_cancel(self, job_id: str) -> tuple[bool, subprocess.Popen | None]:
        """Mark for cancellation unless already finished.

        Returns (initiated, live proc to kill). `initiated` is False for an
        unknown or already-terminal job; `proc` is None when the job has not
        started a subprocess yet.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return False, None
            job.cancelled = True
            return True, job.proc

    def active_procs(self) -> list[subprocess.Popen]:
        with self._lock:
            return [job.proc for job in self._jobs.values() if job.proc is not None]

    def job_dir(self, job_id: str) -> Path | None:
        job = self.get(job_id)
        if job is None:
            return None
        return job.outdir

    def sweep(self, now: float, ttl: float) -> list[str]:
        """Drop non-running jobs whose job directory mtime is older than `ttl`.

        Returns the removed job ids; their job directories and the stored
        upload are deleted too.
        """
        removed: list[tuple[str, Path, Path]] = []
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.status == RUNNING:
                    continue
                if _job_mtime(job) < now - ttl:
                    del self._jobs[job_id]
                    removed.append((job_id, job.outdir, job.pdf_path))
        for _job_id, job_dir, pdf_path in removed:
            shutil.rmtree(job_dir, ignore_errors=True)
            pdf_path.unlink(missing_ok=True)
        return [job_id for job_id, _job_dir, _pdf_path in removed]


def _job_mtime(job: Job) -> float:
    try:
        return job.outdir.stat().st_mtime
    except OSError:
        return job.created_at