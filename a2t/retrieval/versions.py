"""Select the consolidated version of a base act that the drafters worked against.

Amendments are written against the consolidated text in force when the proposal was
drafted. Retrieving a different version silently misplaces every ordinal target, so
the version has to be pinned rather than assumed.

An arbitrary date does **not** resolve: `02003L0087-20260717` returns no content.
Only dates of actual consolidations work, so the available versions are discovered
from Cellar and the latest one at or before the proposal date is chosen.

The date in `02003L0087-20240301` is the **start date** in EUR-Lex's sense: the date
of application of the amending act, or its date of entry into force where no date of
application is given ("Consolidation methodology", EUR-Lex, 3 February 2026). It is
not the date the consolidated text was produced. EUR-Lex consolidates in arrears —
typically three to six weeks after the start date, and sometimes years — so between a
start date and the corresponding consolidation being published there is a window in
which the version in force is one nobody could yet read. A proposal drafted in that
window was written against the *previous* consolidation. Selection still follows the
start date, because that is what the law was; `publication_warning` lets the caller
flag the cases where the two readings disagree.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

import requests

CELLAR_SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"

VERSIONS_QUERY = """
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT ?celex ?created WHERE {{
  ?w cdm:resource_legal_id_celex ?celex .
  FILTER(STRSTARTS(STR(?celex), '{base}-'))
  OPTIONAL {{ ?w cdm:work_date_creation ?created }}
}} ORDER BY ?celex
"""

PINNED = re.compile(r"^(?P<base>0\d{4}[A-Z]\d{4})-(?P<date>\d{8})$")


class VersionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Version:
    """One consolidated version: when it applies from, and when it was published.

    `published` is None for the handful of versions Cellar carries no creation date
    for — unknown, which is treated as "no reason to warn" rather than as a problem.
    """

    start: date
    published: date | None = None


def parse_pinned(celex: str) -> tuple[str, date] | None:
    match = PINNED.match(celex.strip().upper())
    if match is None:
        return None
    stamp = match.group("date")
    return match.group("base"), date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))


def _parse_created(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


LISTING_ATTEMPTS = 3
LISTING_BACKOFF = 3.0  # seconds, times the attempt number


def _transient(exc: Exception) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    response = getattr(exc, "response", None)
    return response is not None and response.status_code >= 500


@lru_cache(maxsize=64)
def list_version_records(base_celex: str, timeout: int = 90) -> tuple[Version, ...]:
    """Consolidated versions of a sector-0 CELEX, oldest start date first."""
    base = base_celex.strip().upper()
    for attempt in range(LISTING_ATTEMPTS):
        try:
            response = requests.get(
                CELLAR_SPARQL,
                params={"query": VERSIONS_QUERY.format(base=base),
                        "format": "application/sparql-results+json"},
                headers={"Accept": "application/sparql-results+json"},
                timeout=timeout,
            )
            response.raise_for_status()
            rows = response.json()["results"]["bindings"]
            break
        except Exception as exc:  # network, JSON, or endpoint trouble
            # A read timeout or a 5xx from Cellar is usually gone on the next request: it
            # cost one act of Directive 2020/1056 its conversion in an otherwise complete
            # run, and 503s did the same to a corpus run earlier.
            if attempt + 1 < LISTING_ATTEMPTS and _transient(exc):
                time.sleep(LISTING_BACKOFF * (attempt + 1))
                continue
            raise VersionError(f"could not list versions for {base}: {exc}") from exc

    # A version can come back on several rows; keep the earliest creation date seen,
    # since that is when the text first became readable.
    published: dict[date, date | None] = {}
    for row in rows:
        parsed = parse_pinned(row["celex"]["value"])
        if parsed is None:
            continue
        start = parsed[1]
        created = _parse_created(row.get("created", {}).get("value"))
        if start not in published or published[start] is None:
            published[start] = created
        elif created is not None:
            published[start] = min(published[start], created)
    return tuple(Version(s, published[s]) for s in sorted(published))


def list_versions(base_celex: str, timeout: int = 90) -> tuple[date, ...]:
    """Consolidation start dates available for a sector-0 CELEX, oldest first."""
    return tuple(v.start for v in list_version_records(base_celex, timeout))


def select_version(base_celex: str, on_date: date,
                   available: tuple[date, ...] | None = None) -> str:
    """Pinned CELEX for the version in force on `on_date`.

    Raises if the act had no consolidation by then — better than silently handing back
    a version the drafters never saw.
    """
    base = base_celex.strip().upper()
    versions = available if available is not None else list_versions(base)
    if not versions:
        raise VersionError(f"no consolidated versions found for {base}")

    eligible = [d for d in versions if d <= on_date]
    if not eligible:
        raise VersionError(
            f"{base} has no consolidated version at or before {on_date.isoformat()} "
            f"(earliest is {versions[0].isoformat()})"
        )
    chosen = max(eligible)
    return f"{base}-{chosen.strftime('%Y%m%d')}"


def publication_warning(base_celex: str, on_date: date,
                        records: tuple[Version, ...] | None = None) -> str | None:
    """Flag a selected version that had not been published on `on_date`.

    The version in force can postdate its own publication (see the module docstring),
    and a proposal drafted in that window was written against the previous text. Both
    readings are defensible, so this warns rather than overriding the selection.
    """
    base = base_celex.strip().upper()
    versions = records if records is not None else list_version_records(base)
    if not versions:
        return None

    selected = select_version(base, on_date, tuple(v.start for v in versions))
    start = parse_pinned(selected)[1]
    chosen = next(v for v in versions if v.start == start)
    if chosen.published is None or chosen.published <= on_date:
        return None

    readable = [v for v in versions
                if v.published is not None and v.published <= on_date]
    if readable:
        previous = max(readable, key=lambda v: v.start)
        alternative = (
            f"the latest one that was, {base}-{previous.start.strftime('%Y%m%d')}, "
            f"published {previous.published.isoformat()}"
        )
    else:
        alternative = "no consolidated version of this act was published by then"

    return (
        f"{selected} applies from {start.isoformat()} but was not published until "
        f"{chosen.published.isoformat()}, after the proposal date "
        f"{on_date.isoformat()}. The drafters cannot have read it — they will have "
        f"worked from {alternative}. Provision numbering may differ between the two; "
        f"verify the changes, and pass --as-of to select the other version."
    )
