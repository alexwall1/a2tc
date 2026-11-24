"""Regression tests for the client-side markdown renderer (app.js).

The renderer is pure at load time (no DOM access until its functions are
called), so the test concatenates the real `a2t_web/static/app.js` with a
short driver and runs it under `node`, piping sample markdown through
`renderMarkdown` and asserting on the returned HTML string. This is the
security boundary of the report preview: nothing but the renderer's fixed
tag whitelist may ever be emitted, so `<script>`-style payloads must survive
only as escaped text.

The whole module skips itself when `node` is not installed, following the
repo convention (see README) that tests whose prerequisites are absent run
clean on a fresh clone.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent.parent / "a2t_web" / "static" / "app.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)

# Every tag the renderer is allowed to emit; anything else must arrive as
# escaped text.
_ALLOWED_TAGS = frozenset(
    {
        "h1", "h2", "h3", "h4", "h5", "h6",
        "p", "br", "strong", "em", "code",
        "table", "thead", "tbody", "tr", "th", "td",
        "ul", "li", "blockquote",
    }
)

# An opening tag whose name is followed by whitespace before the closing
# bracket: i.e. a tag carrying an attribute.
_TAG_WITH_ATTRIBUTE = re.compile(r"<\w+[^>]*\s")

_SAMPLES = {
    "script": 'note <script>alert(1)</script>',
    "img": '<img src=x onerror="alert(1)">',
    "svg": "<svg onload=alert(1)>",
    "iframe": '<iframe srcdoc="<script>x</script>"></iframe>',
    "attr": '| A | B |\n|---|---|\n| **a" onclick="alert(1)** | ok |',
    "heading": "# Hi",
    "bold": "**bold**",
    "table": "| a | b |\n|---|---|\n| 1 | 2 |",
}


@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    """Render every sample once through the real app.js under node."""
    driver = (
        APP_JS.read_text(encoding="utf-8")
        + "\nvar INPUTS = "
        + json.dumps(_SAMPLES)
        + ";\nvar out = [];\n"
        + "for (var key in INPUTS) { out.push(renderMarkdown(INPUTS[key])); }\n"
        + "process.stdout.write(JSON.stringify(out));\n"
    )
    result = subprocess.run(
        ["node", "-e", driver],
        capture_output=True,
        text=True,
        check=True,
    )
    outputs = json.loads(result.stdout)
    assert len(outputs) == len(_SAMPLES)
    return dict(zip(_SAMPLES, outputs))


def test_script_payload_stays_text(rendered):
    html = rendered["script"]
    assert "<script" not in html
    assert "&lt;script&gt;" in html
    assert "alert(1)" in html


def test_tag_payloads_stay_text(rendered):
    for key in ("img", "svg", "iframe"):
        html = rendered[key]
        assert f"&lt;{key}" in html
        assert f"<{key}" not in html
        assert _TAG_WITH_ATTRIBUTE.search(html) is None


def test_attribute_injection_in_table_cell_is_neutralised(rendered):
    html = rendered["attr"]
    assert _TAG_WITH_ATTRIBUTE.search(html) is None
    assert "<a " not in html
    assert "href=" not in html
    assert "src=" not in html
    assert "<th>A</th>" in html
    assert "<td>" in html
    # The payload survives only as escaped text inside the cell: the quotes
    # are escaped and "onclick" never becomes an attribute on a tag.
    assert "&quot; onclick=&quot;" in html
    assert _TAG_WITH_ATTRIBUTE.search(html) is None


def test_positive_control_formats_markdown(rendered):
    assert "<h1>Hi</h1>" in rendered["heading"]
    assert "<strong>bold</strong>" in rendered["bold"]
    table = rendered["table"]
    assert "<th>a</th>" in table
    assert "<th>b</th>" in table
    assert "<td>1</td>" in table
    assert "<td>2</td>" in table


def test_only_whitelisted_tags_are_emitted(rendered):
    for html in rendered.values():
        for tag in re.findall(r"</?([a-z0-9]+)", html):
            assert tag in _ALLOWED_TAGS, tag