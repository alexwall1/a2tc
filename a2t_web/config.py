"""Runtime settings for the web app, built from environment variables.

Password files (``user-password`` / ``admin-password``) are read from the
secrets dir (``A2T_WEB_SECRETS_DIR``, default ``.secrets``) and are required:
a missing or empty-after-trim file raises ``ValueError`` so the app fails
closed at startup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from a2t.retrieval.eurlex_builder import DEFAULT_CACHE

DEFAULT_DATA_DIR = Path("./webapp-data")

_INT_FIELDS = {"max_workers", "job_timeout", "job_ttl_hours", "poll_seconds", "port"}
_ENV_FOR = {
    "max_workers": "A2T_WEB_MAX_WORKERS",
    "job_timeout": "A2T_WEB_JOB_TIMEOUT",
    "job_ttl_hours": "A2T_WEB_JOB_TTL_HOURS",
    "poll_seconds": "A2T_WEB_POLL_SECONDS",
    "port": "A2T_WEB_PORT",
    "secrets_dir": "A2T_WEB_SECRETS_DIR",
    "secret_key": "A2T_WEB_SECRET_KEY",
}


@dataclass(frozen=True)
class WebSettings:
    data_dir: Path = DEFAULT_DATA_DIR
    max_upload_bytes: int = 50 * 1024 * 1024
    cache_dir: Path = DEFAULT_CACHE
    max_workers: int = 1
    job_timeout: int = 3600
    job_ttl_hours: int = 24
    poll_seconds: int = 2
    host: str = "127.0.0.1"
    port: int = 5000
    secrets_dir: Path = Path(".secrets")
    user_password: str = ""
    admin_password: str = ""
    secret_key: str | None = None  # Flask session signing; None → random per process


def _read_secret(secrets_dir: Path, name: str) -> str:
    """Read and trim one password file; raise ValueError when unusable.

    Only the file path (never the contents) is mentioned in errors, so a
    leaked exception message cannot reveal a password.
    """
    path = secrets_dir / name
    if not path.is_file():
        raise ValueError(
            f"Missing password file {path}. Create {secrets_dir / 'user-password'} and "
            f"{secrets_dir / 'admin-password'} to enable access control, then restart."
        )
    try:
        value = path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Password file {path} is not valid UTF-8; write it as plain text."
        ) from exc
    if not value:
        raise ValueError(f"Password file {path} is empty; set a non-empty password.")
    return value


def settings_from_env() -> WebSettings:
    env = os.environ
    raw = {
        "data_dir": env.get("A2T_WEB_DATA_DIR"),
        "max_workers": env.get("A2T_WEB_MAX_WORKERS"),
        "job_timeout": env.get("A2T_WEB_JOB_TIMEOUT"),
        "job_ttl_hours": env.get("A2T_WEB_JOB_TTL_HOURS"),
        "poll_seconds": env.get("A2T_WEB_POLL_SECONDS"),
        "host": env.get("A2T_WEB_HOST"),
        "port": env.get("A2T_WEB_PORT"),
        "cache_dir": env.get("A2T_CACHE"),
        "secrets_dir": env.get("A2T_WEB_SECRETS_DIR"),
        "secret_key": env.get("A2T_WEB_SECRET_KEY"),
    }
    values: dict[str, str | int | Path] = {}
    for key, value in raw.items():
        if value is None:
            continue
        if key in _INT_FIELDS:
            try:
                values[key] = int(value)
            except ValueError:
                raise ValueError(
                    f"{_ENV_FOR[key]} must be an integer, got {value!r}"
                ) from None
        elif key in {"data_dir", "cache_dir", "secrets_dir"}:
            values[key] = Path(value)
        else:
            values[key] = value
    values["user_password"] = _read_secret(
        values.get("secrets_dir", Path(".secrets")), "user-password"
    )
    values["admin_password"] = _read_secret(
        values.get("secrets_dir", Path(".secrets")), "admin-password"
    )
    settings = WebSettings(**values)
    if settings.max_workers > 1:
        raise ValueError(
            "A2T_WEB_MAX_WORKERS must be 1: the base-act retrieval cache has no "
            "locking, so concurrent pipelines could corrupt it"
        )
    return settings