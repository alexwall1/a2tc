"""settings_from_env: env parsing errors, the max_workers invariant, the seed
user/password variables, and the derived db_path."""

from pathlib import Path

import pytest

from a2t_web.config import settings_from_env

_ALL_ENV = (
    "A2T_WEB_DATA_DIR",
    "A2T_WEB_MAX_WORKERS",
    "A2T_WEB_JOB_TIMEOUT",
    "A2T_WEB_JOB_TTL_HOURS",
    "A2T_WEB_POLL_SECONDS",
    "A2T_WEB_HOST",
    "A2T_WEB_PORT",
    "A2T_CACHE",
    "A2T_WEB_SECRET_KEY",
    "A2T_ADMIN_USERNAME",
    "A2T_ADMIN_PASSWORD",
    "A2T_USER_USERNAME",
    "A2T_USER_PASSWORD",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Clear every A2T_* var so stray host environment cannot leak in."""
    for name in _ALL_ENV:
        monkeypatch.delenv(name, raising=False)


def test_settings_from_env_defaults():
    settings = settings_from_env()
    assert settings.max_workers == 1
    assert settings.job_timeout == 3600
    assert settings.job_ttl_hours == 24
    assert settings.poll_seconds == 2
    assert settings.port == 5000
    assert settings.host == "127.0.0.1"
    assert settings.secret_key is None
    assert settings.seed_admin_username == "admin"
    assert settings.seed_admin_password == ""
    assert settings.seed_user_username == "user"
    assert settings.seed_user_password == ""
    assert settings.db_path == Path("./webapp-data") / "a2t.db"


def test_settings_from_env_parses_valid_values(monkeypatch):
    monkeypatch.setenv("A2T_WEB_JOB_TIMEOUT", "60")
    monkeypatch.setenv("A2T_WEB_PORT", "8080")
    settings = settings_from_env()
    assert settings.job_timeout == 60
    assert settings.port == 8080


def test_settings_from_env_non_numeric_int_raises_readable_error(monkeypatch):
    monkeypatch.setenv("A2T_WEB_JOB_TIMEOUT", "soon")
    with pytest.raises(ValueError, match="A2T_WEB_JOB_TIMEOUT"):
        settings_from_env()


def test_settings_from_env_max_workers_over_one_rejected(monkeypatch):
    monkeypatch.setenv("A2T_WEB_MAX_WORKERS", "2")
    with pytest.raises(ValueError, match="A2T_WEB_MAX_WORKERS"):
        settings_from_env()


def test_settings_from_env_max_workers_one_accepted(monkeypatch):
    monkeypatch.setenv("A2T_WEB_MAX_WORKERS", "1")
    assert settings_from_env().max_workers == 1


# ------------------------------------------------------------- seed variables


def test_settings_from_env_seed_vars_parsed(monkeypatch):
    monkeypatch.setenv("A2T_ADMIN_USERNAME", "root")
    monkeypatch.setenv("A2T_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("A2T_USER_USERNAME", "alice")
    monkeypatch.setenv("A2T_USER_PASSWORD", "user-secret")
    settings = settings_from_env()
    assert settings.seed_admin_username == "root"
    assert settings.seed_admin_password == "admin-secret"
    assert settings.seed_user_username == "alice"
    assert settings.seed_user_password == "user-secret"


def test_settings_from_env_seed_usernames_default_when_unset():
    settings = settings_from_env()
    assert settings.seed_admin_username == "admin"
    assert settings.seed_user_username == "user"


def test_settings_from_env_seed_passwords_unset_by_default():
    settings = settings_from_env()
    assert settings.seed_admin_password == ""
    assert settings.seed_user_password == ""


# ------------------------------------------------------------- db_path


def test_settings_from_env_db_path_follows_data_dir(monkeypatch):
    monkeypatch.setenv("A2T_WEB_DATA_DIR", "/srv/a2t/data")
    settings = settings_from_env()
    assert settings.data_dir == Path("/srv/a2t/data")
    assert settings.db_path == Path("/srv/a2t/data") / "a2t.db"


def test_settings_accepts_fixed_secret_key(monkeypatch):
    monkeypatch.setenv("A2T_WEB_SECRET_KEY", "abc")
    settings = settings_from_env()
    assert settings.secret_key == "abc"