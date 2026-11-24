"""Runtime settings for the web app, built from environment variables.

Users are seeded into the SQLite database on first run from the
``A2T_ADMIN_*`` / ``A2T_USER_*`` environment variables. The legacy
``.secrets/user-password`` / ``.secrets/admin-password`` files are no longer
read; the OpenRouter key handling in ``a2t.config`` (``.secrets/openrouter-key``)
is unrelated and unchanged.
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
    db_path: Path = DEFAULT_DATA_DIR / "a2t.db"
    seed_admin_username: str = "admin"
    seed_admin_password: str = ""  # set A2T_ADMIN_PASSWORD to seed an admin
    seed_user_username: str = "user"
    seed_user_password: str = ""  # set A2T_USER_PASSWORD to seed a normal user
    secret_key: str | None = None  # Flask session signing; None → random per process


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
        "seed_admin_username": env.get("A2T_ADMIN_USERNAME"),
        "seed_admin_password": env.get("A2T_ADMIN_PASSWORD"),
        "seed_user_username": env.get("A2T_USER_USERNAME"),
        "seed_user_password": env.get("A2T_USER_PASSWORD"),
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
        elif key in {"data_dir", "cache_dir"}:
            values[key] = Path(value)
        else:
            values[key] = value
    data_dir = Path(values.get("data_dir", DEFAULT_DATA_DIR))
    values.setdefault("db_path", data_dir / "a2t.db")
    settings = WebSettings(**values)
    if settings.max_workers > 1:
        raise ValueError(
            "A2T_WEB_MAX_WORKERS must be 1: the base-act retrieval cache has no "
            "locking, so concurrent pipelines could corrupt it"
        )
    return settings