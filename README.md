# A2T — Amendments to Tracked changes

Converts an EU legislative amendment proposal (a COM document, PDF) into Word
documents with real tracked changes: the base text is the consolidated legislation
as it stood when the proposal was drafted, and the tracked changes are the
amendments the proposal makes to it.

Everything else is derived from the PDF — which acts are amended, their CELEX
identifiers, and the consolidated text to amend, retrieved from EUR-Lex.

## Requirements

- Python 3.11+
- `pdftotext` (poppler-utils) on the PATH — `sudo apt install poppler-utils`

  Poppler 0.42 or newer, for `pdftotext -bbox-layout`. That is where the paragraph
  breaks come from: a new subparagraph often carries no number and no indent, so
  nothing else in the extracted text marks it. An older poppler still works, and
  says so on every run — the text is all there, but such a subparagraph is printed
  as part of the provision above it.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Quick start

```bash
.venv/bin/a2t convert proposal.pdf -o out/
```

You get one `.docx` per amended act, with `w:ins`/`w:del` revisions Word can accept
or reject individually, plus `out/report.md` accounting for every instruction in the
proposal and what became of it. Anything the resolver could not place is flagged
both in the report and as a Word comment at the provision concerned — never dropped
silently.

Base-act retrieval dominates the runtime; expect a couple of minutes.

## Options

`a2t convert PDF`:

| Flag | Effect |
|---|---|
| `-o, --out PATH` | Output directory (default `out`). |
| `--as-of YYYY-MM-DD` | Override the proposal date used to pick the consolidated version. Useful when the cover date is unreadable, or to select a version the drafters were more likely to have read. |
| `--llm / --no-llm` | Offer instructions the rules could not resolve to an LLM tier. Off by default. |
| `--model TEXT` | OpenRouter model id (default `openai/gpt-5-nano`). |
| `--cache PATH` | Where retrieved acts are cached (default `~/.cache/a2t/acts`). |
| `--author TEXT` | Author name recorded on every tracked change (default `A2T`). |
| `--recitals` | Insert the proposal's own preamble ahead of the articles, as tracked insertions. Off by default — see below. |

The LLM tier is optional and needs an OpenRouter key, from `$OPENROUTER_API_KEY` or
a `.openrouter_key` file in the project root. Without `--llm` the app is entirely
offline apart from EUR-Lex retrieval.

## Recitals

`--recitals` puts the amending act's own front matter into every document it writes,
ahead of the articles and in the order the Official Journal prints it:

```
2026/0212 (COD)                                          ← inserted
            Proposal for a
DIRECTIVE OF THE EUROPEAN PARLIAMENT AND OF THE COUNCIL
amending Directive 2003/87/EC and Decision (EU) 2015/1814 …
            (Text with EEA relevance)
THE EUROPEAN PARLIAMENT AND THE COUNCIL OF THE EUROPEAN UNION,
Having regard to …
Whereas:
(1)   The Paris Agreement³, adopted on 12 December 2015 …
      … (98 recitals)
HAVE ADOPTED THIS DIRECTIVE:                             ← inserted
Article 1                                                ← base text + revisions
```

Every one of those paragraphs is a revision, so rejecting the document's changes still
leaves the base act exactly as it stands today.

It is off by default because accepting them does *not* give the act as the proposal
would leave it. An amending act keeps its own recitals — it does not pass them to the
act it amends, and none of the sixteen acts amending Directive 2003/87/EC contains a
single instruction touching that Directive's recitals. So the preamble is printed as
context for the amendments, not as one of them, and `report.md` says so.

Footnote references are read from the page geometry rather than the text, because
`pdftotext` renders a superscript as a digit run against the word before it and there
is no way to tell "Agreement³" from "CO2" by shape. They become real Word footnotes,
printed at the foot of the page that cites them and numbered by Word itself, so the
numbering cannot drift out of step with the text. Rejecting the insertion takes the
note with the reference.

## Other commands

Each stage of the pipeline can be run on its own to inspect what it produced:

```
a2t detect PDF      which acts the proposal amends, and their CELEX ids
a2t retrieve PDF    fetch and structure those base acts
a2t extract PDF     the de-boilerplated operative part
a2t parse PDF       the tree of amendment instructions
a2t resolve PDF     what each instruction resolves to in the base act
a2t diff PDF        the resulting set of edits
```

Run `a2t COMMAND --help` for the flags of each.

## Tests

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Tests that need a sample proposal or a populated retrieval cache skip themselves
when it is absent, so a fresh clone runs clean. To exercise them, put an amendment
proposal PDF in the project root under the name the test expects (see the `PDF =`
line at the top of each test module) and run `a2t retrieve` once to fill the cache.

## Scope

Articles only. Annexes and languages other than English are out of scope. The amending
act's recitals are printed on request (`--recitals`) but never resolved or amended —
nothing in a proposal amends the recitals of the act it amends. Output documents follow the typography of the Official
Journal — Times New Roman, 10pt justified body, centred article headings.

See `PLAN.md` for the design and the reasoning behind it.

## License

MIT — see [LICENSE](LICENSE).
