"""Phase 3: version selection and CELEX verification.

Version-list discovery hits the network, so these tests inject the list instead.
"""

from datetime import date

import pytest

from a2t.detect.celex import parse_citation
from a2t.retrieval.acquire import Acquisition, acquire
from a2t.retrieval.base import RetrievalError, RetrievedAct, title_matches
from a2t.retrieval.eurlex_builder import build_config
from a2t.retrieval.versions import (
    Version, VersionError, list_versions, parse_pinned, publication_warning,
    select_version,
)

# The 16 real consolidations of Directive 2003/87/EC, as published by Cellar.
ETS_VERSIONS = tuple(
    date(*d) for d in [
        (2004, 11, 13), (2009, 1, 13), (2009, 4, 20), (2009, 6, 25), (2013, 7, 1),
        (2013, 12, 19), (2014, 4, 30), (2015, 10, 29), (2017, 12, 29), (2018, 4, 8),
        (2020, 1, 1), (2021, 1, 1), (2023, 1, 21), (2023, 3, 1), (2023, 6, 5),
        (2024, 3, 1),
    ]
)


# ---------------------------------------------------------------- pinned CELEX


@pytest.mark.parametrize(
    "celex, base, when",
    [
        ("02003L0087-20240301", "02003L0087", date(2024, 3, 1)),
        ("02015D1814-20240101", "02015D1814", date(2024, 1, 1)),
        ("02016R0679-20210101", "02016R0679", date(2021, 1, 1)),
    ],
)
def test_parse_pinned(celex, base, when):
    assert parse_pinned(celex) == (base, when)


@pytest.mark.parametrize("celex", ["02003L0087", "32003L0087", "not-a-celex", ""])
def test_parse_pinned_rejects_unpinned(celex):
    assert parse_pinned(celex) is None


# ------------------------------------------------------------ version selection


def test_selects_version_in_force_at_proposal_date():
    """COM(2026) 616 was drafted on 2026-07-17; the current text was the 2024-03-01
    consolidation — the same version as the reference PDF."""
    got = select_version("02003L0087", date(2026, 7, 17), ETS_VERSIONS)
    assert got == "02003L0087-20240301"


def test_selects_earlier_version_for_an_earlier_proposal():
    got = select_version("02003L0087", date(2023, 4, 1), ETS_VERSIONS)
    assert got == "02003L0087-20230301"


def test_selection_is_exclusive_of_later_consolidations():
    """A consolidation published after drafting must not be chosen."""
    got = select_version("02003L0087", date(2024, 2, 29), ETS_VERSIONS)
    assert got == "02003L0087-20230605"


def test_exact_consolidation_date_selects_itself():
    got = select_version("02003L0087", date(2024, 3, 1), ETS_VERSIONS)
    assert got == "02003L0087-20240301"


def test_no_version_before_date_raises():
    with pytest.raises(VersionError, match="no consolidated version at or before"):
        select_version("02003L0087", date(2003, 1, 1), ETS_VERSIONS)


def test_empty_version_list_raises():
    with pytest.raises(VersionError, match="no consolidated versions"):
        select_version("02003L0087", date(2026, 7, 17), ())


# ------------------------------------------------- publication lag (start date vs
# when the consolidation actually appeared)

# The same 16 consolidations, paired with the date Cellar created each one. EUR-Lex
# consolidates in arrears, so every publication date here postdates its start date —
# by three weeks for the recent ones and by years for the 2014 backfill. 2018-04-08
# is the one version Cellar carries no creation date for.
ETS_RECORDS = tuple(
    Version(date(*start), date(*pub) if pub else None) for start, pub in [
        ((2004, 11, 13), (2014, 6, 30)), ((2009, 2, 2), (2014, 6, 30)),
        ((2009, 4, 20), (2014, 6, 30)), ((2009, 6, 25), (2014, 7, 4)),
        ((2013, 7, 1), (2014, 7, 4)), ((2013, 12, 19), (2014, 7, 4)),
        ((2014, 4, 30), (2016, 1, 19)), ((2015, 10, 29), (2017, 1, 25)),
        ((2017, 12, 29), (2018, 3, 29)), ((2018, 4, 8), None),
        ((2020, 1, 1), (2020, 8, 4)), ((2021, 1, 1), (2021, 9, 22)),
        ((2023, 1, 21), (2023, 2, 14)), ((2023, 3, 1), (2023, 3, 21)),
        ((2023, 6, 5), (2023, 7, 18)), ((2024, 3, 1), (2024, 4, 2)),
    ]
)


def test_no_warning_when_the_version_was_already_published():
    """The ETS proposal's 2024-03-01 base text had been out for two years."""
    assert publication_warning("02003L0087", date(2026, 7, 17), ETS_RECORDS) is None


def test_warns_when_the_version_in_force_was_not_yet_published():
    """A proposal drafted 2024-03-10 falls in the lag window: 02003L0087-20240301
    applied from 1 March but was not published until 2 April."""
    warning = publication_warning("02003L0087", date(2024, 3, 10), ETS_RECORDS)
    assert warning is not None
    assert "02003L0087-20240301" in warning
    assert "2024-04-02" in warning
    assert "02003L0087-20230605" in warning  # what the drafters could actually read
    assert "--as-of" in warning


def test_no_warning_on_the_day_the_version_appears():
    assert publication_warning("02003L0087", date(2024, 4, 2), ETS_RECORDS) is None


def test_unknown_publication_date_does_not_warn():
    """2018-04-08 has no creation date in Cellar; unknown is not a reason to warn."""
    assert publication_warning("02003L0087", date(2018, 4, 10), ETS_RECORDS) is None


def test_warns_when_nothing_had_been_published_at_all():
    records = (Version(date(2024, 3, 1), date(2024, 4, 2)),)
    warning = publication_warning("02003L0087", date(2024, 3, 10), records)
    assert "no consolidated version of this act was published by then" in warning


def test_no_warning_without_versions():
    assert publication_warning("02003L0087", date(2024, 3, 10), ()) is None


def test_acquisition_keeps_warnings_separable():
    """Both doubts can attach to one version; the report bullets them separately."""
    act = RetrievedAct("02003L0087-20240301", "Directive 2003/87/EC", "<p/>", None)
    assert Acquisition(act, "x", "x").warning is None
    both = Acquisition(act, "x", "y", ("not yet published", "no English text"))
    assert both.warning.splitlines() == ["not yet published", "no English text"]


class _Provider:
    """Fetches only the versions it was told have English text."""

    def __init__(self, *usable: str):
        self.usable = set(usable)
        self.tried: list[str] = []

    def fetch(self, celex: str) -> RetrievedAct:
        self.tried.append(celex)
        if celex not in self.usable:
            raise RetrievalError(f"no content for {celex}")
        return RetrievedAct(celex, "Regulation (EU) 2021/1119", "<p/>", None)


def _versions(monkeypatch, *starts):
    monkeypatch.setattr(
        "a2t.retrieval.versions.list_version_records",
        lambda base, timeout=90: tuple(Version(s) for s in starts),
    )


def test_substitution_goes_backwards_never_forwards(monkeypatch):
    """A later consolidation is nearer in days but holds changes the drafters lacked."""
    _versions(monkeypatch, date(2013, 7, 1), date(2015, 10, 29), date(2016, 3, 1))
    provider = _Provider("02003L0087-20130701", "02003L0087-20160301")

    acquired = acquire(provider, "02003L0087", date(2016, 1, 1))

    assert acquired.requested == "02003L0087-20151029"
    assert acquired.pinned == "02003L0087-20130701"  # not the nearer 2016 one
    assert "which is earlier" in acquired.warning
    assert "02003L0087-20160301" not in provider.tried


def test_only_later_consolidations_falls_back_to_the_act_as_adopted(monkeypatch):
    """COM(2025) 524: the ECL was first consolidated in 2026, after its own adoption.

    The 2026 consolidation carries the proposal's own enacted amendments, so it is
    never used; the text in force in 2025 was the act as adopted.
    """
    _versions(monkeypatch, date(2021, 7, 9), date(2026, 4, 7))
    provider = _Provider("02021R1119-20260407", "32021R1119")

    acquired = acquire(provider, "02021R1119", date(2025, 7, 2))

    assert acquired.pinned == "32021R1119"
    assert acquired.substituted
    assert "had not been amended" in acquired.warning
    assert "02021R1119-20260407" not in provider.tried


def test_error_when_neither_a_consolidation_nor_the_act_as_adopted_can_be_had(
    monkeypatch,
):
    _versions(monkeypatch, date(2021, 7, 9), date(2026, 4, 7))
    provider = _Provider("02021R1119-20260407")  # sector 3 unavailable too

    with pytest.raises(RetrievalError) as excinfo:
        acquire(provider, "02021R1119", date(2025, 7, 2))

    message = str(excinfo.value)
    assert "02021R1119-20210709" in message
    assert "32021R1119" in message
    assert "2026-04-07" in message


def test_list_versions_derives_start_dates_from_records(monkeypatch):
    monkeypatch.setattr("a2t.retrieval.versions.list_version_records",
                        lambda base, timeout=90: ETS_RECORDS)
    assert list_versions("02003L0087") == tuple(v.start for v in ETS_RECORDS)


# ------------------------------------------------------------ title verification


def test_title_confirms_correct_act():
    citation = parse_citation("Directive 2003/87/EC")
    title = ("Directive 2003/87/EC of the European Parliament and of the Council "
             "of 13 October 2003 establishing a system for greenhouse gas emission "
             "allowance trading")
    assert title_matches(citation, title)


def test_title_confirms_decision():
    citation = parse_citation("Decision (EU) 2015/1814")
    assert title_matches(
        citation,
        "Decision (EU) 2015/1814 of the European Parliament and of the Council "
        "of 6 October 2015 concerning the establishment and operation of a market "
        "stability reserve",
    )


def test_title_rejects_wrong_act():
    """The guard against a plausible-but-wrong constructed CELEX."""
    citation = parse_citation("Directive 2003/87/EC")
    assert not title_matches(citation, "Directive 2008/98/EC on waste")


def test_title_catches_glued_footnote_marker():
    """PDF extraction turns 'Regulation (EU) 2019/331' + footnote 41 into 2019/33141.

    The bogus citation parses, but the retrieved act's title will not match it.
    """
    bogus = parse_citation("Regulation (EU) 2019/33141")
    # The number bound truncates rather than rejecting, so the citation still parses —
    # as 3314, not the real 331. Only the title check can tell.
    assert bogus is not None and bogus.number == 3314
    real_title = ("Commission Delegated Regulation (EU) 2019/331 of 19 December 2018 "
                  "determining transitional Union-wide rules for harmonised free "
                  "allocation of emission allowances")
    assert not title_matches(bogus, real_title)


def test_title_matches_number_first_style():
    citation = parse_citation("Regulation (EC) No 219/2009")
    assert title_matches(citation, "Regulation (EC) No 219/2009 of 11 March 2009")


def test_empty_title_never_matches():
    assert not title_matches(parse_citation("Directive 2003/87/EC"), "")


# ------------------------------------------------------------------------ config


def test_config_requires_raw_html():
    """full_text_html is the primary structural source, not an optional extra."""
    config = build_config("02003L0087-20240301", "/tmp/x")
    assert config["processing"]["text_extraction"]["store_raw_html"] is True
    assert config["processing"]["text_extraction"]["article_granularity"] == "point"
    assert config["data"]["celex_ids"] == ["02003L0087-20240301"]


def test_config_disables_translation():
    config = build_config("02003L0087-20240301", "/tmp/x")
    translation = config["processing"]["translation"]
    assert translation["translate_full_text"] is False
    assert translation["translate_text_units"] is False


def test_no_consolidation_by_the_proposal_date_falls_back_to_the_act_as_adopted(
    monkeypatch,
):
    """Regulation (EC) No 80/2009 against a 2024 proposal: first consolidated later.

    `select_version` refuses a version the drafters never saw, which is right, but the
    act as adopted is what they did see — the same answer as when the consolidation
    in force exists but has no English text.
    """
    _versions(monkeypatch, date(2025, 3, 1))
    provider = _Provider("32009R0080", "02009R0080-20250301")

    acquired = acquire(provider, "02009R0080", date(2024, 4, 24))

    assert acquired.pinned == "32009R0080"
    assert acquired.substituted
    assert "had not been amended" in acquired.warning
    assert "02009R0080-20250301" not in provider.tried


def test_no_consolidation_at_all_still_raises(monkeypatch):
    """With nothing to say the act was never amended, guessing would be wrong."""
    _versions(monkeypatch)

    with pytest.raises(VersionError):
        acquire(_Provider("32009R0080"), "02009R0080", date(2024, 4, 24))


def test_no_consolidation_by_the_date_and_no_act_as_adopted_says_both(monkeypatch):
    _versions(monkeypatch, date(2025, 3, 1))

    with pytest.raises(RetrievalError) as excinfo:
        acquire(_Provider("02009R0080-20250301"), "02009R0080", date(2024, 4, 24))

    assert "32009R0080" in str(excinfo.value)
    assert "2025-03-01" in str(excinfo.value)


def _write_works(directory, language, source):
    import polars as pl

    directory.mkdir(parents=True)
    pl.DataFrame({"celex_id": ["02012L0029-20121115"], "title": ["Richtlijn 2012/29/EU"],
                  "full_text_html": ["<p class='title-article-norm'>Artikel 1</p>"],
                  "language": [language], "content_source": [source]}
                 ).write_parquet(directory / "works.parquet")


def test_text_in_another_language_is_not_taken_for_the_act(tmp_path):
    """The library fills in whatever language Cellar has when there is no English one."""
    from a2t.retrieval.eurlex_builder import EurlexBuilderProvider

    provider = EurlexBuilderProvider(cache_dir=tmp_path)
    target = provider._dir_for("02012L0029-20121115")
    _write_works(target, "nld", "cellar_html_nld")

    with pytest.raises(RetrievalError, match="no English text"):
        provider._load("02012L0029-20121115", target)


def test_english_text_is_loaded(tmp_path):
    from a2t.retrieval.eurlex_builder import EurlexBuilderProvider

    provider = EurlexBuilderProvider(cache_dir=tmp_path)
    target = provider._dir_for("02012L0029-20121115")
    _write_works(target, "eng", "cellar_html_eng")

    assert provider._load("02012L0029-20121115", target).celex == "02012L0029-20121115"


def test_a_pre_2000_title_with_the_number_first_confirms_the_act():
    from a2t.detect.celex import parse_citation
    from a2t.retrieval.base import title_matches

    citation = parse_citation("Regulation (EC) 1257/96")
    assert title_matches(citation, "Council Regulation (EC) No 1257/96 of 20 June 1996 "
                                   "concerning humanitarian aid")
