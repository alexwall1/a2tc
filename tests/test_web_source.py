"""Reading a EUR-Lex link, and fetching the document it names.

Only the document number is taken from a pasted link; the PDF is fetched from
addresses the tool builds. These tests pin both halves, and that nothing else a link
contains is ever requested.
"""

import pytest

pytest.importorskip("flask")

from a2t_web import source
from a2t_web.source import SourceError, celex_from_reference


@pytest.mark.parametrize("text, celex", [
    ("https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:52023PC0702", "52023PC0702"),
    ("https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX%3A32023L0959", "32023L0959"),
    ("https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:52026PC0616&from=EN",
     "52026PC0616"),
    ("https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=COM:2023:702:FIN", "52023PC0702"),
    ("https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=COM:2018:99:FIN", "52018PC0099"),
    ("https://eur-lex.europa.eu/eli/dir/2023/959/oj", "32023L0959"),
    ("https://data.europa.eu/eli/reg/2020/1056/oj", "32020R1056"),
    ("https://eur-lex.europa.eu/eli/dec/2015/1814/oj/eng", "32015D1814"),
    ("https://www.eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:52023PC0702", "52023PC0702"),
    ("eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:52023PC0702", "52023PC0702"),
    ("52023PC0702", "52023PC0702"),
    ("  celex:32023l0959 ", "32023L0959"),
])
def test_a_link_or_number_names_a_document(text, celex):
    assert celex_from_reference(text) == celex


@pytest.mark.parametrize("text, said", [
    ("", "Enter a EUR-Lex link"),
    ("https://example.com/legal-content/EN/TXT/?uri=CELEX:52023PC0702", "Only links to"),
    ("https://eur-lex.europa.eu.evil.example/?uri=CELEX:52023PC0702", "Only links to"),
    ("ftp://eur-lex.europa.eu/?uri=CELEX:52023PC0702", "Only links to"),
    ("https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=OJ:L_202300959", "not one this tool"),
    ("https://eur-lex.europa.eu/homepage.html", "names no document"),
    ("https://eur-lex.europa.eu/eli/treaty/teu_2016/oj", "names no document"),
    ("https://eur-lex.europa.eu/eli/reg_del/2020/xyz/oj", "names no document"),
    ("02003L0087-20240301", "consolidated text"),
    ("https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:02003L0087-20240301",
     "consolidated text"),
    ("62019CJ0001", "Only an adopted act"),
    ("not a number", "not a CELEX number"),
])
def test_a_link_that_cannot_be_used_is_refused_with_a_reason(text, said):
    with pytest.raises(SourceError, match=said):
        celex_from_reference(text)


def test_a_consolidated_text_is_refused_as_what_amendments_apply_to():
    with pytest.raises(SourceError, match="amends anything"):
        celex_from_reference("02003L0087")


class _Fetcher:
    """Stands in for a2t.corpus.fetch.Fetcher; records what it was asked for."""

    asked: list[str] = []

    def __init__(self, cache_dir, **kwargs):
        self.cache_dir = cache_dir
        self.kwargs = kwargs

    def fetch(self, celex):
        _Fetcher.asked.append(celex)
        path = self.cache_dir / f"{celex}.pdf"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4 fetched")
        return path


def test_the_document_is_fetched_by_number_and_copied_to_the_job(monkeypatch, tmp_path):
    _Fetcher.asked = []
    monkeypatch.setattr(source, "Fetcher", _Fetcher)
    destination = tmp_path / "uploads" / "job.pdf"
    destination.parent.mkdir()

    source.fetch_pdf("52023PC0702", destination, tmp_path)

    assert _Fetcher.asked == ["52023PC0702"]
    assert destination.read_bytes() == b"%PDF-1.4 fetched"
    assert (tmp_path / "fetched" / "52023PC0702.pdf").is_file()  # kept for next time


def test_a_failed_fetch_is_a_502_that_says_which_document(monkeypatch, tmp_path):
    class Failing(_Fetcher):
        def fetch(self, celex):
            raise source.FetchError(f"{celex}: EUR-Lex answered 202 with no PDF")

    monkeypatch.setattr(source, "Fetcher", Failing)
    with pytest.raises(SourceError) as excinfo:
        source.fetch_pdf("52023PC0702", tmp_path / "job.pdf", tmp_path)
    assert excinfo.value.status == 502
    assert "52023PC0702" in str(excinfo.value)
