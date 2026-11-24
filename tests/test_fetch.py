"""Fetching acts for the corpus, with the network replaced."""

import pytest

from a2t.corpus import fetch as fetch_module
from a2t.corpus.fetch import FetchError, Fetcher

PDF = b"%PDF-1.7 " + b"x" * 20_000


class Response:
    def __init__(self, status=200, content_type="application/pdf", body=PDF, text=""):
        self.status_code = status
        self.headers = {"content-type": content_type}
        self.content = body
        self.text = text or body.decode("latin-1")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def serve(monkeypatch, routes):
    """`routes` maps a URL prefix to a Response; every request is recorded."""
    asked = []

    def get(url, headers=None, timeout=None):
        asked.append(url)
        for prefix, response in routes.items():
            if url.startswith(prefix):
                return response
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr(fetch_module.requests, "get", get)
    return asked


def test_a_pdf_from_eurlex_is_saved_and_cached(monkeypatch, tmp_path):
    asked = serve(monkeypatch, {"https://eur-lex": Response()})
    fetcher = Fetcher(tmp_path, delay=0)

    path = fetcher.fetch("32008L0101")
    assert path.read_bytes() == PDF
    fetcher.fetch("32008L0101")
    assert len(asked) == 1  # the second call used the cache


def test_an_empty_html_answer_falls_back_to_cellar(monkeypatch, tmp_path):
    """EUR-Lex's PDF address answers some existing documents with an empty HTML page."""
    listing = Response(300, "application/xhtml+xml", b"",
                       text='<a href="http://publications.europa.eu/resource/cellar/abc.0001.02/DOC_1">')
    asked = serve(monkeypatch, {
        "https://eur-lex": Response(200, "text/html; charset=UTF-8", b""),
        "https://publications.europa.eu/resource/celex/": listing,
        "https://publications.europa.eu/resource/cellar/": Response(),
    })
    path = Fetcher(tmp_path, delay=0).fetch("32008L0101")

    assert path.read_bytes() == PDF
    # The listed address is plain http; the fetch follows it over https.
    assert asked[-1] == "https://publications.europa.eu/resource/cellar/abc.0001.02/DOC_1"


def test_a_document_cellar_does_not_list_is_an_error(monkeypatch, tmp_path):
    serve(monkeypatch, {
        "https://eur-lex": Response(200, "text/html", b""),
        "https://publications.europa.eu/resource/celex/":
            Response(300, "application/xhtml+xml", b"", text="<p>nothing</p>"),
    })
    with pytest.raises(FetchError, match="lists no document"):
        Fetcher(tmp_path, delay=0).fetch("32008L0101")


def test_an_implausibly_small_pdf_is_refused(monkeypatch, tmp_path):
    serve(monkeypatch, {"https://eur-lex": Response(body=b"%PDF-1.7 tiny")})
    with pytest.raises(FetchError, match="implausibly small"):
        Fetcher(tmp_path, delay=0).fetch("32008L0101")


def test_a_202_from_eurlex_is_asked_again_before_falling_back(monkeypatch, tmp_path):
    """EUR-Lex answers 202 with an empty page while it prepares the PDF."""
    answers = [Response(202, "text/html; charset=UTF-8", b""),
               Response(202, "text/html; charset=UTF-8", b""), Response()]
    asked = []

    def get(url, headers=None, timeout=None):
        asked.append(url)
        return answers.pop(0)

    monkeypatch.setattr(fetch_module.requests, "get", get)
    monkeypatch.setattr(fetch_module.time, "sleep", lambda s: None)

    path = Fetcher(tmp_path, delay=0).fetch("32014R1357")
    assert path.read_bytes() == PDF
    assert len(asked) == 3
    assert all(url.startswith("https://eur-lex") for url in asked)  # Cellar never asked


def test_a_document_eurlex_never_serves_goes_to_cellar_after_the_retries(
        monkeypatch, tmp_path):
    asked = serve(monkeypatch, {
        "https://eur-lex": Response(202, "text/html", b""),
        "https://publications.europa.eu/resource/celex/": Response(404, "text/html", b"")})
    monkeypatch.setattr(fetch_module.time, "sleep", lambda s: None)

    with pytest.raises(FetchError, match="404"):
        Fetcher(tmp_path, delay=0, retries=2).fetch("32014R1357")
    assert sum(1 for url in asked if url.startswith("https://eur-lex")) == 3  # 1 + 2 retries
