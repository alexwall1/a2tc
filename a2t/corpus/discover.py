"""Find amending acts whose effect the EU has already published.

When an amending act is adopted, the Publications Office eventually issues a new
consolidated version of the act it amended. Where exactly one amending act separates
two consolidations, all three documents are retrievable and the middle one's correct
outcome is a matter of public record:

    02003L0087-20230301  →  32023L0959  →  02003L0087-20230605
        base text          amending act      the published answer

That makes a self-validating test case. Consolidations that absorb several amending
acts at once cannot be used — the 2023-06-05 consolidation of the ETS Directive takes
in both 2023/958 and 2023/959, so neither can be checked against it alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

import requests

from a2t.retrieval.versions import CELLAR_SPARQL, list_versions

# Corrigenda share the amended act's number: "32023L0959R(01)". They are editorial
# fixes, not separate amending acts.
CORRIGENDUM = re.compile(r"R\(\d+\)$")
AMENDING_ACT = re.compile(r"^3\d{4}[LRD]\d{4}$")

CONSOLIDATES_QUERY = """
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT ?celex WHERE {{
  ?w cdm:resource_legal_id_celex ?wc .
  FILTER(STR(?wc) = '{celex}')
  ?w cdm:act_consolidated_consolidates_resource_legal ?t .
  ?t cdm:resource_legal_id_celex ?celex .
}}
"""


@dataclass(frozen=True)
class Triple:
    """A base text, the act that amended it, and the published result."""

    base: str        # 02003L0087
    before: str      # 02003L0087-20230301
    amending: str    # 32023L0959
    after: str       # 02003L0087-20230605

    def __str__(self) -> str:
        return f"{self.before} + {self.amending} -> {self.after}"


@dataclass(frozen=True)
class Rejected:
    before: str
    after: str
    reason: str
    acts: tuple[str, ...] = ()


@lru_cache(maxsize=256)
def consolidates(celex: str, timeout: int = 120) -> frozenset[str]:
    """Every resource a consolidated act incorporates."""
    response = requests.get(
        CELLAR_SPARQL,
        params={"query": CONSOLIDATES_QUERY.format(celex=celex),
                "format": "application/sparql-results+json"},
        headers={"Accept": "application/sparql-results+json"},
        timeout=timeout,
    )
    response.raise_for_status()
    return frozenset(
        row["celex"]["value"] for row in response.json()["results"]["bindings"]
    )


def _amending_acts(celexes: frozenset[str]) -> set[str]:
    """Keep only genuine amending acts: drop consolidations and corrigenda."""
    return {
        celex for celex in celexes
        if AMENDING_ACT.match(celex) and not CORRIGENDUM.search(celex)
    }


def find_triples(base_celex: str,
                 versions: tuple[date, ...] | None = None
                 ) -> tuple[list[Triple], list[Rejected]]:
    """Clean triples for one base act, plus the pairs that had to be rejected."""
    base = base_celex.strip().upper()
    dates = versions if versions is not None else list_versions(base)
    triples: list[Triple] = []
    rejected: list[Rejected] = []

    for earlier, later in zip(dates, dates[1:]):
        before = f"{base}-{earlier.strftime('%Y%m%d')}"
        after = f"{base}-{later.strftime('%Y%m%d')}"
        try:
            introduced = _amending_acts(consolidates(after)) - _amending_acts(
                consolidates(before)
            )
        except Exception as exc:
            rejected.append(Rejected(before, after, f"lookup failed: {exc}"))
            continue

        if len(introduced) == 1:
            triples.append(Triple(base, before, introduced.pop(), after))
        elif not introduced:
            rejected.append(Rejected(before, after, "no amending act identified"))
        else:
            rejected.append(Rejected(
                before, after,
                f"{len(introduced)} amending acts in one consolidation",
                tuple(sorted(introduced)),
            ))
    return triples, rejected
