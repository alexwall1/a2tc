"""Where a job's PDF comes from when the user gives a EUR-Lex link, not a file.

A link is read for the document it names and for nothing else. The CELEX number is
taken out of it and the PDF is then fetched from addresses this module builds itself
(`a2t.corpus.fetch.Fetcher`), so a pasted URL can never make the server request an
address of the user's choosing — only one of three hosts is accepted at all, and only
to read a document number from.

Accepted forms, all for the document's English text:

    https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:52023PC0702
    https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX%3A32023L0959
    https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=COM:2023:702:FIN
    https://eur-lex.europa.eu/eli/dir/2023/959/oj      (and data.europa.eu/eli/…)
    52023PC0702                                         (a bare CELEX number)

Only sector 3 (an adopted act) and sector 5 (a Commission proposal) are accepted. A
consolidated text (sector 0) is the base the amendments are applied to, not a document
that amends anything, and is refused with that explanation.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from a2t.corpus.fetch import FetchError, Fetcher
from a2t.detect.celex import ELI_LETTER

ALLOWED_HOSTS = frozenset({"eur-lex.europa.eu", "publications.europa.eu",
                           "data.europa.eu"})

# 5 2023 PC 0702 — sector, year, descriptor letters, number.
CELEX = re.compile(r"^(?P<sector>[0-9])(?P<year>\d{4})(?P<kind>[A-Z]{1,2})(?P<number>\d{4})$")
COM_REFERENCE = re.compile(r"^COM:(?P<year>\d{4}):(?P<number>\d{1,4}):FIN$", re.I)
ELI_PATH = re.compile(
    r"^/eli/(?P<type>[a-z_]+)/(?P<year>\d{4})/(?P<number>\d{1,4})(?:/|$)", re.I)

# Fetching happens inside the web request; a stalled upstream must not hold it for long.
FETCH_DELAY = 1.0
FETCH_RETRIES = 2
FETCH_TIMEOUT = 40


class SourceError(ValueError):
    """The link cannot be used; the message is written for the person who pasted it."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def celex_from_reference(text: str) -> str:
    """The CELEX number a pasted link or number refers to, or `SourceError`."""
    text = (text or "").strip()
    if not text:
        raise SourceError("Enter a EUR-Lex link or a CELEX number.")

    if "/" not in text and "." not in text and "?" not in text:
        return _checked(re.sub(r"^CELEX:", "", text, flags=re.I))

    parts = urlsplit(text if "://" in text else f"https://{text}")
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.scheme not in ("http", "https") or host not in ALLOWED_HOSTS:
        raise SourceError(
            "Only links to eur-lex.europa.eu, publications.europa.eu or "
            "data.europa.eu are accepted.")

    for value in parse_qs(parts.query).get("uri", []):
        reference = unquote(value).strip()
        if reference.upper().startswith("CELEX:"):
            return _checked(reference[len("CELEX:"):])
        com = COM_REFERENCE.match(reference)
        if com:
            return _checked(f"5{com.group('year')}PC{int(com.group('number')):04d}")
        raise SourceError(
            f"The reference {reference!r} in the link is not one this tool can open; "
            "use a link that carries a CELEX number (uri=CELEX:…).")

    eli = ELI_PATH.match(parts.path)
    if eli:
        letter = ELI_LETTER.get(eli.group("type").lower())
        if letter is None:
            raise SourceError(
                f"An ELI link of type {eli.group('type')!r} names no act this tool "
                "can convert; only directives, regulations and decisions are.")
        return _checked(f"3{eli.group('year')}{letter}{int(eli.group('number')):04d}")

    raise SourceError("That link names no document: it has no CELEX number "
                      "(uri=CELEX:…) and is not an ELI link.")


CONSOLIDATED = (
    "That is a consolidated text, which is what amendments are applied to, not one that "
    "amends anything. Give the amending act or the proposal instead.")


def _checked(celex: str) -> str:
    celex = celex.strip().upper()
    if re.match(r"^0\d{4}[A-Z]{1,2}\d{4}(?:-\d{8})?$", celex):
        raise SourceError(CONSOLIDATED)  # also the pinned form, 02003L0087-20240301
    match = CELEX.match(celex)
    if match is None:
        raise SourceError(f"{celex!r} is not a CELEX number such as 52023PC0702.")
    if match.group("sector") not in ("3", "5"):
        raise SourceError(
            "Only an adopted act (CELEX 3…) or a Commission proposal (CELEX 5…) can "
            "be converted.")
    return celex


def fetch_pdf(celex: str, destination: Path, data_dir: Path) -> None:
    """Put the English PDF of `celex` at `destination`.

    Documents are kept under `data_dir/fetched/` so that asking again for one already
    fetched costs no request. `SourceError` carries a status for the failure: 502 when
    EUR-Lex could not supply it.
    """
    fetched = Path(data_dir) / "fetched"
    try:
        path = Fetcher(fetched, delay=FETCH_DELAY, timeout=FETCH_TIMEOUT,
                       retries=FETCH_RETRIES).fetch(celex)
    except FetchError as exc:
        raise SourceError(
            f"EUR-Lex did not supply a PDF for {celex}. {exc}", status=502) from exc
    shutil.copyfile(path, destination)
