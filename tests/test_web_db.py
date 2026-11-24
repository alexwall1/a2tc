"""Unit tests for the SQLite persistence layer (a2t_web.db)."""

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("flask")
from werkzeug.security import check_password_hash

from a2t_web.db import Database
from a2t_web.jobs import CANCELLED, DONE, FAILED, QUEUED


def make_db(tmp_path: Path, name: str = "a2t.db") -> Database:
    db = Database(tmp_path / name)
    db.init()
    return db


def make_job(**overrides):
    values = {
        "id": "j1",
        "pdf_name": "com.pdf",
        "status": QUEUED,
        "error": None,
        "created_at": 1750000000.0,
        "started_at": None,
        "finished_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# -------------------------------------------------------------- schema / init


def test_init_is_idempotent(tmp_path):
    db = Database(tmp_path / "a2t.db")
    db.init()
    db.init()  # second call must not raise
    assert (tmp_path / "a2t.db").is_file()


def test_init_creates_database_file_and_parent_directories(tmp_path):
    path = tmp_path / "deep" / "nested" / "a2t.db"
    Database(path).init()
    assert path.is_file()


def test_database_file_mode_is_0600(tmp_path):
    db = Database(tmp_path / "a2t.db")
    db.init()
    assert (tmp_path / "a2t.db").stat().st_mode & 0o777 == 0o600
    # writes reopen the file through the same connect path; mode stays 0600
    db.create_user("alice", "pw", "user")
    assert (tmp_path / "a2t.db").stat().st_mode & 0o777 == 0o600


# ----------------------------------------------------------------------- users


def test_create_user_then_verify_login_succeeds(tmp_path):
    db = make_db(tmp_path)
    assert db.create_user("alice", "pw", "user") is True
    assert db.create_user("bob", "pw", "admin") is True
    user = db.verify_login("alice", "pw")
    assert user is not None
    assert user["username"] == "alice"
    assert user["role"] == "user"
    assert "password_hash" not in user
    admin = db.verify_login("bob", "pw")
    assert admin is not None
    assert admin["role"] == "admin"
    assert admin["username"] == "bob"


def test_verify_login_wrong_password_returns_none(tmp_path):
    db = make_db(tmp_path)
    db.create_user("alice", "pw", "user")
    assert db.verify_login("alice", "nope") is None


def test_verify_login_unknown_username_returns_none(tmp_path):
    db = make_db(tmp_path)
    db.create_user("alice", "pw", "user")
    assert db.verify_login("nobody", "pw") is None


def test_create_user_duplicate_returns_false_and_does_not_overwrite(tmp_path):
    db = make_db(tmp_path)
    assert db.create_user("alice", "pw", "user") is True
    assert db.create_user("alice", "other", "user") is False
    assert db.verify_login("alice", "other") is None
    assert db.verify_login("alice", "pw") is not None


def test_password_stored_as_hash_not_plaintext(tmp_path):
    db = make_db(tmp_path)
    db.create_user("alice", "plain", "user")
    row = db.get_user_by_username("alice")
    assert row["password_hash"] != "plain"
    assert check_password_hash(row["password_hash"], "plain")


def test_username_trimmed_on_create_and_lookup(tmp_path):
    db = make_db(tmp_path)
    assert db.create_user("  alice  ", "pw", "user") is True
    assert db.verify_login("alice", "pw") is not None
    assert db.verify_login("  alice  ", "pw") is not None


def test_create_user_rejects_unknown_role(tmp_path):
    db = make_db(tmp_path)
    assert db.create_user("alice", "pw", "sudoer") is False


def test_list_users_ordered_by_id(tmp_path):
    db = make_db(tmp_path)
    db.create_user("first", "pw", "user")
    db.create_user("second", "pw", "admin")
    db.create_user("third", "pw", "user")
    assert [u["username"] for u in db.list_users()] == [
        "first",
        "second",
        "third",
    ]


def test_count_users(tmp_path):
    db = make_db(tmp_path)
    assert db.count_users() == 0
    db.create_user("alice", "pw", "user")
    assert db.count_users() == 1


def test_verify_login_unknown_user_still_runs_dummy_check(tmp_path, monkeypatch):
    """The unknown-user path must still run a password-hash comparison, so a
    response-time difference cannot reveal whether a username exists."""
    import a2t_web.db as db_module

    db = make_db(tmp_path)
    db.create_user("alice", "pw", "user")
    calls: list[str] = []
    real_check = db_module.check_password_hash

    def spy(hash_value, password):
        calls.append(hash_value)
        return real_check(hash_value, password)

    monkeypatch.setattr(db_module, "check_password_hash", spy)
    assert db.verify_login("nobody", "pw") is None
    # one hash comparison happened, against the dummy hash, not a user's
    assert len(calls) == 1
    assert calls[0] != db.get_user_by_username("alice")["password_hash"]
    # a known user still verifies through their real hash afterwards
    assert db.verify_login("alice", "pw") is not None
    assert len(calls) == 2
    assert calls[1] == db.get_user_by_username("alice")["password_hash"]


def test_verify_login_hostile_usernames_do_not_bypass(tmp_path):
    db = make_db(tmp_path)
    db.create_user("alice", "pw", "user")
    hostile = (
        "alice' OR '1'='1",
        'alice" OR 1=1 --',
        "alice; DROP TABLE users;--",
        "alice\\' OR 1=1 --",
    )
    for username in hostile:
        assert db.verify_login(username, "pw") is None, username
    # the real row is untouched and still verifies
    assert db.verify_login("alice", "pw") is not None
    assert db.count_users() == 1


def test_create_user_hostile_username_is_stored_literally(tmp_path):
    db = make_db(tmp_path)
    hostile = "alice' OR '1'='1"
    assert db.create_user(hostile, "pw", "user") is True
    # the literal hostile string is the account name; the plain lookup is not
    # affected and no extra rows appear
    assert db.verify_login(hostile, "pw") is not None
    assert db.verify_login("alice", "pw") is None
    assert db.count_users() == 1


def test_verify_login_hostile_password_returns_none(tmp_path):
    db = make_db(tmp_path)
    db.create_user("alice", "pw", "user")
    assert db.verify_login("alice", "' OR '1'='1") is None
    assert db.verify_login("alice", "x'; DROP TABLE users;--") is None
    assert db.verify_login("alice", "pw") is not None


# ------------------------------------------------------------------------ jobs


def test_insert_job_then_terminal_roundtrip(tmp_path):
    db = make_db(tmp_path)
    db.insert_job(make_job(id="a1", pdf_name="com.pdf"))
    rows = db.list_jobs()
    assert len(rows) == 1
    row = rows[0]
    assert row["job_id"] == "a1"
    assert row["pdf_name"] == "com.pdf"
    assert row["status"] == QUEUED
    assert row["created_at"] == 1750000000.0
    assert row["started_at"] is None
    assert row["finished_at"] is None
    assert row["error"] is None
    db.set_job_terminal("a1", DONE, None, 1750000001.0, 1750000002.0)
    rows = db.list_jobs()
    assert rows[0]["status"] == DONE
    assert rows[0]["started_at"] == 1750000001.0
    assert rows[0]["finished_at"] == 1750000002.0
    assert rows[0]["created_at"] == 1750000000.0


@pytest.mark.parametrize("status", [DONE, FAILED, CANCELLED])
def test_terminal_statuses_persisted(tmp_path, status):
    db = make_db(tmp_path)
    db.insert_job(make_job(id="j", started_at=1750000001.0))
    db.set_job_terminal("j", status, None, 1750000001.0, 1750000002.0)
    rows = db.list_jobs()
    assert rows[0]["status"] == status
    assert rows[0]["started_at"] == 1750000001.0


def test_set_job_terminal_can_write_none_started_at(tmp_path):
    # A job cancelled while still queued has no started_at; the UPDATE must
    # store NULL rather than carry an old value forward.
    db = make_db(tmp_path)
    db.insert_job(make_job(id="q", started_at=1750000001.0))
    db.set_job_terminal("q", CANCELLED, None, None, 1750000002.0)
    rows = db.list_jobs()
    assert rows[0]["status"] == CANCELLED
    assert rows[0]["started_at"] is None
    assert rows[0]["finished_at"] == 1750000002.0


def test_set_job_terminal_unknown_id_is_noop(tmp_path):
    db = make_db(tmp_path)
    db.set_job_terminal("missing", DONE, None, None, 1.0)  # must not raise
    assert db.list_jobs() == []


def test_list_jobs_newest_first_and_limit(tmp_path):
    db = make_db(tmp_path)
    for i in range(5):
        db.insert_job(make_job(id=f"j{i}", created_at=100.0 + i))
    rows = db.list_jobs()
    assert [r["job_id"] for r in rows] == ["j4", "j3", "j2", "j1", "j0"]
    limited = db.list_jobs(limit=2)
    assert [r["job_id"] for r in limited] == ["j4", "j3"]


def test_error_survives_roundtrip_with_newlines_and_tabs(tmp_path):
    db = make_db(tmp_path)
    error = "line1\nline2\tboom\\tail"
    db.insert_job(make_job(id="x", error=error))
    db.set_job_terminal("x", FAILED, error, 1750000001.0, 100.0)
    assert db.list_jobs()[0]["error"] == error


def test_basic_thread_safety_concurrent_writes(tmp_path):
    db = make_db(tmp_path)
    errors: list[Exception] = []

    def writer(job_id: str):
        try:
            db.insert_job(make_job(id=job_id, created_at=time.time()))
            db.set_job_terminal(job_id, DONE, None, None, time.time())
        except Exception as exc:  # pragma: no cover - failure only on bugs
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(f"t{i}",)) for i in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    rows = db.list_jobs()
    assert len(rows) == 20
    assert all(r["status"] == DONE for r in rows)

def test_get_job_returns_the_stored_record_or_none(tmp_path):
    from types import SimpleNamespace

    db = Database(tmp_path / "a2t.db")
    db.init()
    db.insert_job(SimpleNamespace(id="j1", pdf_name="x.pdf", status="queued", error=None,
                                  created_at=1.0, started_at=None, finished_at=None))
    assert db.get_job("j1") == {"job_id": "j1", "pdf_name": "x.pdf", "status": "queued",
                                "error": None, "created_at": 1.0, "started_at": None,
                                "finished_at": None}
    assert db.get_job("missing") is None


def test_fail_orphans_closes_open_jobs_and_leaves_finished_ones(tmp_path):
    from types import SimpleNamespace

    db = Database(tmp_path / "a2t.db")
    db.init()
    for job_id, status in [("q", "queued"), ("r", "running"), ("d", "done"), ("f", "failed")]:
        db.insert_job(SimpleNamespace(id=job_id, pdf_name="x.pdf", status=status,
                                      error="original" if status == "failed" else None,
                                      created_at=1.0, started_at=None, finished_at=None))
    assert db.fail_orphans("cut off", 99.0) == 2
    states = {j["job_id"]: (j["status"], j["error"], j["finished_at"]) for j in db.list_jobs()}
    assert states["q"] == ("failed", "cut off", 99.0)
    assert states["r"] == ("failed", "cut off", 99.0)
    assert states["d"] == ("done", None, None)
    assert states["f"] == ("failed", "original", None)
