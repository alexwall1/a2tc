"""Runtime configuration: OpenRouter credentials and model selection."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

KEY_FILENAME = ".openrouter_key"
KEY_ENV = "OPENROUTER_API_KEY"
MODEL_ENV = "A2T_MODEL"

# A small model is enough: the tier performs closed-set selection over candidates
# supplied in the prompt, not open-ended generation. It must support structured
# outputs so the response can be schema-validated.
#
# Verified on OpenRouter as supporting `structured_outputs`:
#   openai/gpt-5-nano                  (default — small, cheap, 400k context)
#   google/gemini-2.5-flash-lite
#   qwen/qwen3-30b-a3b-instruct-2507   (3B active parameters)
#   ibm-granite/granite-4.1-8b
#   openai/gpt-oss-20b                 (open weights)
DEFAULT_MODEL = "openai/gpt-5-nano"

BASE_URL = "https://openrouter.ai/api/v1"


class ConfigError(RuntimeError):
    pass


@dataclass
class Settings:
    api_key: str | None = None
    model: str = DEFAULT_MODEL
    base_url: str = BASE_URL
    timeout: int = 60

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def require_key(self) -> str:
        if not self.api_key:
            raise ConfigError(
                f"no OpenRouter API key: set {KEY_ENV} or create {KEY_FILENAME} "
                f"in the project root"
            )
        return self.api_key


def find_key(start: Path | None = None) -> str | None:
    """The API key from the environment, or from `.openrouter_key` walking upwards."""
    from_env = os.environ.get(KEY_ENV)
    if from_env and from_env.strip():
        return from_env.strip()

    here = (start or Path.cwd()).resolve()
    for directory in [here, *here.parents]:
        candidate = directory / KEY_FILENAME
        if candidate.is_file():
            key = candidate.read_text(encoding="utf-8").strip()
            if key:
                return key
    return None


def load_settings(start: Path | None = None, model: str | None = None) -> Settings:
    return Settings(
        api_key=find_key(start),
        model=model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL,
    )
