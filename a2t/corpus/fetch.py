"""Download acts from EUR-Lex by CELEX.

Cached on disk and rate-limited: this walks a public service on someone else's
infrastructure, and a corpus run should not look like a crawl.
"""

from __future__ import annotations

import time
from pathlib import Path

import requests

PDF_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:{celex}"
USER_AGENT = "a2t/0.1 (legislative amendment conversion research)"

DEFAULT_DELAY = 2.0  # seconds between requests
MIN_PDF_BYTES = 10_000


class FetchError(RuntimeError):
    pass


def pdf_url(celex: str) -> str:
    return PDF_URL.format(celex=celex.strip().upper())


class Fetcher:
    def __init__(self, cache_dir: Path | str = Path("out/corpus/pdf"),
                 delay: float = DEFAULT_DELAY, timeout: int = 120):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.timeout = timeout
        self._last_request = 0.0

    def path_for(self, celex: str) -> Path:
        return self.cache_dir / f"{celex.strip().upper()}.pdf"

    def is_cached(self, celex: str) -> bool:
        path = self.path_for(celex)
        return path.is_file() and path.stat().st_size >= MIN_PDF_BYTES

    def fetch(self, celex: str) -> Path:
        path = self.path_for(celex)
        if self.is_cached(celex):
            return path

        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

        try:
            response = requests.get(
                pdf_url(celex), headers={"User-Agent": USER_AGENT},
                timeout=self.timeout,
            )
            self._last_request = time.monotonic()
            response.raise_for_status()
        except Exception as exc:
            raise FetchError(f"{celex}: {exc}") from exc

        content_type = response.headers.get("content-type", "")
        if "pdf" not in content_type.lower():
            raise FetchError(
                f"{celex}: expected a PDF, got {content_type!r} "
                f"({len(response.content)} bytes) — the act may not exist in EN"
            )
        if len(response.content) < MIN_PDF_BYTES:
            raise FetchError(f"{celex}: PDF is implausibly small "
                             f"({len(response.content)} bytes)")

        path.write_bytes(response.content)
        return path
