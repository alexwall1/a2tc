"""Runtime configuration: the LLM provider's credentials, endpoint and model.

The LLM tier talks to Requesty (https://router.requesty.ai/v1), an OpenAI-compatible
router. Its key is read from `REQUESTY_API_KEY` or from a file named `api-key.txt`;
nothing from the tool's earlier provider is read any more, because a key issued by one
company must never be sent to another company's host.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

KEY_FILENAME = "api-key.txt"
KEY_ENV = "REQUESTY_API_KEY"
MODEL_ENV = "A2T_MODEL"

# Render mounts a "secret file" at /etc/secrets/<name> as well as in the app directory.
SECRET_MOUNT = Path("/etc/secrets")

BASE_URL = "https://router.requesty.ai/v1"

# The tier performs closed-set selection over candidates supplied in the prompt, not
# open-ended generation, so a small model is enough. It has to answer in JSON; models
# that cannot honour a strict JSON schema are asked for a JSON object instead (see
# `a2t.resolve.llm.LlmClient`), and every reply is validated either way.
#
# There is deliberately no default: a model id that does not exist on the router would
# fail every job at runtime. Set `A2T_MODEL`, or pass `--model`, to a Requesty model id.
DEFAULT_MODEL: str | None = None


class ConfigError(RuntimeError):
    pass


@dataclass
class Settings:
    api_key: str | None = None
    model: str | None = DEFAULT_MODEL
    base_url: str = BASE_URL
    timeout: int = 60

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def require_key(self) -> str:
        if not self.api_key:
            raise ConfigError(
                f"no Requesty API key: set {KEY_ENV}, or put the key in a file named "
                f"{KEY_FILENAME} in the project root (or {SECRET_MOUNT / KEY_FILENAME})"
            )
        return self.api_key

    def require_model(self) -> str:
        if not self.model:
            raise ConfigError(
                f"no model set: pass --model or set {MODEL_ENV} to a Requesty model id"
            )
        return self.model


def project_root() -> Path:
    """The directory above the `a2t` package — where the deployed app's files sit."""
    return Path(__file__).resolve().parent.parent


def _read(path: Path) -> str | None:
    try:
        key = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return key or None


def find_key(start: Path | None = None) -> str | None:
    """The API key: the environment first, then `api-key.txt`.

    The file is looked for walking upwards from `start` (the proposal's directory) and
    from the working directory, then in the project root, so an app whose data directory
    lies elsewhere still finds the key beside its code, and finally in the secret mount.
    """
    from_env = os.environ.get(KEY_ENV)
    if from_env and from_env.strip():
        return from_env.strip()

    directories: list[Path] = []
    for origin in (start or Path.cwd(), Path.cwd()):
        here = Path(origin).resolve()
        directories += [here, *here.parents]
    directories += [project_root(), SECRET_MOUNT]

    seen: set[Path] = set()
    for directory in directories:
        if directory in seen:
            continue
        seen.add(directory)
        key = _read(directory / KEY_FILENAME)
        if key:
            return key
    return None


def load_settings(start: Path | None = None, model: str | None = None) -> Settings:
    return Settings(
        api_key=find_key(start),
        model=model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL,
    )
