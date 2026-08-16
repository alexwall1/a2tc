"""SQLite persistence for the web app: users and durable job history.

The database file lives at ``<data_dir>/a2t.db``. Auth reads users from the
``users`` table; the job registry mirrors every job create and every terminal
transition into the ``jobs`` table so ``/admin`` shows history that survives
restarts and in-memory registry sweeps.

Design notes:

- stdlib ``sqlite3`` only; ``werkzeug.security`` (ships with Flask) provides
  password hashing. No new dependencies.
- Every function opens its own connection via ``_connect``; no connection
  crosses threads. A per-instance ``threading.Lock`` serializes writes, and
  WAL + ``busy_timeout`` reduce contention under the threaded dev server.
- All SQL uses ``?`` placeholders; parameterized queries prevent injection.
- Usernames are trimmed before store and lookup; passwords are stored as
  werkzeug hashes, never plaintext.
- The database file (and its WAL sidecars when present) is chmod 0600 so the
  password hashes and job history are not world-readable.
- ``verify_login`` runs a dummy werkzeug hash check when the username is
  unknown so the unknown-user path costs about the same as a real password
  check (otherwise response time would leak which usernames exist).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from werkzeug.security import check_password_hash, generate_password_hash

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user','admin')),
    created_at REAL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    pdf_name TEXT,
    status TEXT,
    error TEXT,
    created_at REAL,
    started_at REAL,
    finished_at REAL
);
"""

_VALID_ROLES = frozenset({"user", "admin"})

_USER_COLUMNS = "id, username, password_hash, role, created_at"

# Compared against when a submitted username does not exist, so the
# unknown-user path costs about the same as a real password check (a missing
# row would otherwise return ~200x faster than a wrong password on a known
# user, letting callers enumerate usernames by response time). Not a secret;
# never printed.
_DUMMY_PASSWORD_HASH = generate_password_hash("a2t-dummy-password")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._write_lock = threading.Lock()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a fresh connection with the app pragmas and commit on success."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        # The DB holds password hashes and job history: never let the umask
        # default (usually 0644) expose it to other local users.
        os.chmod(self.path, 0o600)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            # WAL sidecars (-wal/-shm) appear lazily after writes; clamp them
            # too while they exist so hashes/history are not world-readable.
            for sidecar in (Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
                if sidecar.exists():
                    os.chmod(sidecar, 0o600)
            conn.close()

    def init(self) -> None:
        """Create the schema and database file; idempotent."""
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------- users

    def create_user(self, username: str, password: str, role: str) -> bool:
        """Insert a hashed user; False for a duplicate or invalid arguments."""
        if role not in _VALID_ROLES:
            return False
        username = username.strip()
        if not username or not password:
            return False
        password_hash = generate_password_hash(password)
        try:
            with self._write_lock:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO users (username, password_hash, role, created_at)"
                        " VALUES (?, ?, ?, ?)",
                        (username, password_hash, role, time.time()),
                    )
            return True
        except sqlite3.IntegrityError:
            # username UNIQUE constraint: the user already exists
            return False

    def get_user_by_username(self, username: str) -> dict | None:
        username = username.strip()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "username": row[1],
            "password_hash": row[2],
            "role": row[3],
            "created_at": row[4],
        }

    def verify_login(self, username: str, password: str) -> dict | None:
        """The user dict (minus the hash) on success; None otherwise.

        Unknown usernames and wrong passwords both return None, so callers
        cannot distinguish them (no user enumeration). When the username is
        missing, a dummy hash check still runs so the response time matches
        the wrong-password-on-real-user path.
        """
        user = self.get_user_by_username(username)
        if user is None:
            check_password_hash(_DUMMY_PASSWORD_HASH, password)
            return None
        if not check_password_hash(user["password_hash"], password):
            return None
        return {key: value for key, value in user.items() if key != "password_hash"}

    def list_users(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_USER_COLUMNS} FROM users ORDER BY id"
            ).fetchall()
        return [
            {
                "id": row[0],
                "username": row[1],
                "password_hash": row[2],
                "role": row[3],
                "created_at": row[4],
            }
            for row in rows
        ]

    def count_users(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    # --------------------------------------------------------------- jobs

    def insert_job(self, job) -> None:
        """Persist a freshly created job (status is normally ``queued``).

        Duck-typed on the ``Job`` dataclass so this module does not import
        ``jobs.py``.
        """
        with self._write_lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO jobs (id, pdf_name, status, error, created_at,"
                    " started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        job.id,
                        job.pdf_name,
                        job.status,
                        job.error,
                        job.created_at,
                        job.started_at,
                        job.finished_at,
                    ),
                )

    def set_job_terminal(
        self,
        job_id: str,
        status: str,
        error: str | None,
        started_at: float | None,
        finished_at: float,
    ) -> None:
        """Record a terminal transition; an unknown job id is a no-op.

        ``started_at`` may be None for jobs finished while still queued (e.g.
        cancelled before the worker started them); a non-None value is the
        timestamp the job actually started.
        """
        with self._write_lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE jobs SET status = ?, error = ?, started_at = ?,"
                    " finished_at = ? WHERE id = ?",
                    (status, error, started_at, finished_at, job_id),
                )

    def list_jobs(self, limit: int | None = None) -> list[dict]:
        """Job history, newest first; key ``job_id`` matches the admin template."""
        sql = (
            "SELECT id, pdf_name, status, error, created_at, started_at,"
            " finished_at FROM jobs ORDER BY created_at DESC"
        )
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "job_id": row[0],
                "pdf_name": row[1],
                "status": row[2],
                "error": row[3],
                "created_at": row[4],
                "started_at": row[5],
                "finished_at": row[6],
            }
            for row in rows
        ]