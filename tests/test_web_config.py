"""settings_from_env: env parsing errors, the max_workers invariant, and the
password-file secrets handling."""

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
    "A2T_WEB_SECRETS_DIR",
    "A2T_WEB_SECRET_KEY",
)


@pytest.fixture(autouse=True)
def secrets_env(tmp_path, monkeypatch):
    """Every settings_from_env call needs password files present.

    A fresh tmp secrets dir with `user-password`/`admin-password` is created
    and pointed at via `A2T_WEB_SECRETS_DIR`; all other A2T_WEB_* vars are
    cleared so stray host environment cannot leak into a test.
    """
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "user-password").write_text("user")
    (secrets_dir / "admin-password").write_text("admin")
    for name in _ALL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("A2T_WEB_SECRETS_DIR", str(secrets_dir))


def test_settings_from_env_defaults(monkeypatch):
    # Delete every env var except A2T_WEB_SECRETS_DIR: the autouse fixture
    # points that one at a real tmp secrets dir so settings_from_env can read
    # the password files instead of failing closed on the default `.secrets`.
    for name in _ALL_ENV:
        if name == "A2T_WEB_SECRETS_DIR":
            continue
        monkeypatch.delenv(name, raising=False)
    settings = settings_from_env()
    assert settings.max_workers == 1
    assert settings.job_timeout == 3600
    assert settings.job_ttl_hours == 24
    assert settings.poll_seconds == 2
    assert settings.port == 5000
    assert settings.host == "127.0.0.1"
    assert settings.secret_key is None


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


# ---------------------------------------------------------------- password files


def test_settings_reads_passwords_from_secrets_dir(monkeypatch):
    settings = settings_from_env()
    assert settings.user_password == "user"
    assert settings.admin_password == "admin"


def test_settings_missing_password_file_raises(tmp_path, monkeypatch):
    empty = tmp_path / "empty-secrets"
    empty.mkdir()
    monkeypatch.setenv("A2T_WEB_SECRETS_DIR", str(empty))
    with pytest.raises(ValueError, match="user-password"):
        settings_from_env()


def test_settings_empty_password_file_raises(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets-empty"
    secrets.mkdir()
    (secrets / "user-password").write_text("ok")
    (secrets / "admin-password").write_text("\n")
    monkeypatch.setenv("A2T_WEB_SECRETS_DIR", str(secrets))
    with pytest.raises(ValueError, match="empty"):
        settings_from_env()


def test_settings_non_utf8_password_file_raises(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets-bad-utf8"
    secrets.mkdir()
    (secrets / "user-password").write_text("user")
    # UTF-16 byte order mark + content: not valid UTF-8, must not blow up
    # with a raw UnicodeDecodeError that main() would not format.
    (secrets / "admin-password").write_bytes(b"\xff\xfeP\x00w\x00")
    monkeypatch.setenv("A2T_WEB_SECRETS_DIR", str(secrets))
    with pytest.raises(ValueError, match="admin-password"):
        settings_from_env()


def test_settings_strips_password_whitespace(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets-ws"
    secrets.mkdir()
    (secrets / "user-password").write_text("  pw  \n")
    (secrets / "admin-password").write_text(" admin-pw \n")
    monkeypatch.setenv("A2T_WEB_SECRETS_DIR", str(secrets))
    settings = settings_from_env()
    assert settings.user_password == "pw"
    assert settings.admin_password == "admin-pw"


def test_settings_secrets_dir_env_override(tmp_path, monkeypatch):
    other = tmp_path / "other-secrets"
    other.mkdir()
    (other / "user-password").write_text("other-user")
    (other / "admin-password").write_text("other-admin")
    monkeypatch.setenv("A2T_WEB_SECRETS_DIR", str(other))
    settings = settings_from_env()
    assert settings.user_password == "other-user"
    assert settings.admin_password == "other-admin"


def test_settings_accepts_fixed_secret_key(monkeypatch):
    monkeypatch.setenv("A2T_WEB_SECRET_KEY", "abc")
    settings = settings_from_env()
    assert settings.secret_key == "abc"