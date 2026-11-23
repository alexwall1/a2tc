"""Download acts from EUR-Lex by CELEX.

Cached on disk and rate-limited: this walks a public service on someone else's
infrastructure, and a corpus run should not look like a crawl.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import requests

PDF_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:{celex}"
USER_AGENT = "a2t/0.1 (legislative amendment conversion research)"

DEFAULT_DELAY = 2.0  # seconds between requests
MIN_PDF_BYTES = 10_000


CELLAR_CELEX = "https://publications.europa.eu/resource/celex/{celex}"


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
            response = self._get(pdf_url(celex), {})
            if not self._is_pdf(response):
                # EUR-Lex's PDF address answers some documents with an empty HTML page
                # ("may not exist in EN") though they exist. The Publications Office's
                # own resolver serves them: it answers a bare CELEX with a list of the
                # manifestations, the first of which is the document itself.
                response = self._from_cellar(celex)
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError(f"{celex}: {exc}") from exc

        content_type = response.headers.get("content-type", "")
        if not self._is_pdf(response):
            raise FetchError(
                f"{celex}: expected a PDF, got {content_type!r} "
                f"({len(response.content)} bytes) — the act may not exist in EN"
            )
        if len(response.content) < MIN_PDF_BYTES:
            raise FetchError(f"{celex}: PDF is implausibly small "
                             f"({len(response.content)} bytes)")

        path.write_bytes(response.content)
        return path

    def _get(self, url: str, headers: dict):
        response = requests.get(
            url, headers={"User-Agent": USER_AGENT, **headers}, timeout=self.timeout)
        self._last_request = time.monotonic()
        if response.status_code != 300:  # 300 is Cellar listing the manifestations
            response.raise_for_status()
        return response

    @staticmethod
    def _is_pdf(response) -> bool:
        return "pdf" in response.headers.get("content-type", "").lower()

    def _from_cellar(self, celex: str):
        headers = {"Accept": "application/pdf", "Accept-Language": "eng"}
        response = self._get(CELLAR_CELEX.format(celex=celex.strip().upper()), headers)
        if response.status_code == 300:
            links = re.findall(r'href="([^"]+/DOC_1)"', response.text)
            if not links:
                raise FetchError(f"{celex}: Cellar lists no document for it")
            time.sleep(self.delay)
            response = self._get(links[0].replace("http://", "https://"), headers)
        return response
