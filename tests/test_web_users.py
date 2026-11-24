"""CLI for creating users in the a2t SQLite database (a2t_web.users)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("flask")

from a2t_web.db import Database
from a2t_web.users import create_user_file, main, seed_users


def make_db(tmp_path: Path, name: str = "a2t.db") -> Database:
    db = Database(tmp_path / name)
    db.init()
    return db


def seed_settings(**overrides):
    values = {
        "seed_admin_username": "admin",
        "seed_admin_password": "",
        "seed_user_username": "user",
        "seed_user_password": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_seed_users_nothing_seeded_without_passwords(tmp_path):
    db = make_db(tmp_path)
    seed_users(seed_settings(), db)
    assert db.count_users() == 0


def test_seed_users_skipped_once_any_user_exists(tmp_path):
    db = make_db(tmp_path)
    db.create_user("existing", "pw", "user")
    seed_users(
        seed_settings(seed_admin_password="admin-pw", seed_user_password="user-pw"),
        db,
    )
    assert db.count_users() == 1
    assert db.get_user_by_username("existing") is not None
    assert db.get_user_by_username("admin") is None
    assert db.get_user_by_username("user") is None


def test_seed_users_same_admin_and_user_username_keeps_admin(tmp_path):
    db = make_db(tmp_path)
    seed_users(
        seed_settings(
            seed_admin_username="same",
            seed_admin_password="admin-pw",
            seed_user_username="same",
            seed_user_password="user-pw",
        ),
        db,
    )
    assert db.count_users() == 1
    assert db.get_user_by_username("same")["role"] == "admin"
    assert db.verify_login("same", "admin-pw") is not None
    assert db.verify_login("same", "user-pw") is None


def test_cli_create_admin(tmp_path, capsys):
    make_db(tmp_path)
    db_path = tmp_path / "a2t.db"
    rc = main(
        ["create-admin", "--db", str(db_path), "--username", "root", "--password", "secret"]
    )
    assert rc == 0
    assert "root" in capsys.readouterr().out
    user = Database(db_path).verify_login("root", "secret")
    assert user is not None
    assert user["role"] == "admin"


def test_cli_create_user(tmp_path, capsys):
    make_db(tmp_path)
    db_path = tmp_path / "a2t.db"
    rc = main(
        ["create-user", "--db", str(db_path), "--username", "alice", "--password", "pw"]
    )
    assert rc == 0
    user = Database(db_path).verify_login("alice", "pw")
    assert user is not None
    assert user["role"] == "user"
    assert capsys.readouterr().out


def test_cli_duplicate_returns_nonzero_and_message(tmp_path, capsys):
    db = make_db(tmp_path)
    db.create_user("alice", "pw", "user")
    db_path = tmp_path / "a2t.db"
    rc = main(
        ["create-user", "--db", str(db_path), "--username", "alice", "--password", "other"]
    )
    assert rc != 0
    assert "alice" in capsys.readouterr().err
    # the original password is untouched
    assert Database(db_path).verify_login("alice", "pw") is not None


def test_cli_missing_db_prints_clear_error(tmp_path, capsys):
    missing = tmp_path / "missing" / "a2t.db"
    rc = main(
        ["create-admin", "--db", str(missing), "--username", "root", "--password", "pw"]
    )
    assert rc != 0
    assert "not found" in capsys.readouterr().err.lower()


def test_create_user_file_unknown_role_returns_false(tmp_path):
    db_path = tmp_path / "a2t.db"
    Database(db_path).init()
    assert create_user_file(db_path, "alice", "pw", "sudoer") is False
    assert Database(db_path).count_users() == 0
    assert create_user_file(db_path, "alice", "pw", "admin") is True