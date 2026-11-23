"""Real Commission proposals, through the stages that need no network.

`docs/proposals.tsv` lists 30 proposals for acts amending others, drawn from every year
since 2018. They are not in the repository (public documents, large, not ours); fetch
them with

    a2t corpus fetch $(cut -f1 docs/proposals.tsv) --dir out/proposals

and this module runs each through extraction, detection, segmentation and
classification. Without them every test here is skipped.

What it guards is the shape of the document, not the conversion: that a proposal's
amendments are found at all, and that one that has none says why. The conversion's own
quality is measured by `a2t corpus run out/proposals/*.pdf`, recorded in
docs/baseline.md.
"""

from pathlib import Path

import pytest

from a2t.detect.targets import detect_targets
from a2t.extract.deboilerplate import operative_part
from a2t.extract.pdf import extract_lines
from a2t.parse.rules import classify_all
from a2t.parse.segment import segment

DIRECTORY = Path(__file__).resolve().parents[1] / "out" / "proposals"
PDFS = sorted(DIRECTORY.glob("*.pdf")) if DIRECTORY.is_dir() else []

# Proposals that amend nothing the tool reads, for reasons that are not a bug in it.
# COM(2019) 600 adjusts amounts in a financing decision with no amending sentence.
NOTHING_TO_READ = {"52019PC0600"}

pytestmark = pytest.mark.skipif(not PDFS, reason="no proposals in out/proposals — see the module docstring")


@pytest.fixture(scope="module")
def parsed():
    out = {}
    for pdf in PDFS:
        lines = extract_lines(pdf)
        operative = operative_part(lines).lines
        detection = detect_targets(operative, lines)
        blocks = segment(operative, detection.targets)
        classify_all(blocks)
        out[pdf.stem] = (detection, blocks)
    return out


@pytest.mark.parametrize("name", [p.stem for p in PDFS])
def test_a_proposal_amends_something_or_says_why_it_does_not(parsed, name):
    detection, blocks = parsed[name]
    if detection.targets:
        assert blocks, f"{name}: targets detected but no block built"
        assert sum(1 for b in blocks for _ in b.walk()) > 0, f"{name}: no instructions read"
    elif name not in NOTHING_TO_READ:
        # Annexes only is a legitimate, stated reason; anything else is a gap to fix.
        assert detection.annexes, f"{name}: no amended act found and no reason given"


@pytest.mark.parametrize("name", [p.stem for p in PDFS])
def test_every_target_names_a_celex_and_an_instruction_block(parsed, name):
    detection, blocks = parsed[name]
    for target in detection.targets:
        assert target.celex.startswith("3") and len(target.celex) == 10
    assert {b.celex for b in blocks} <= {t.celex for t in detection.targets}


def test_the_list_matches_what_is_on_disk():
    listed = {line.split("\t")[0] for line in
              (Path(__file__).resolve().parents[1] / "docs" / "proposals.tsv")
              .read_text().splitlines() if line.strip()}
    assert {p.stem for p in PDFS} <= listed
