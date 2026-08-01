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

The LLM tier is optional and needs an OpenRouter key, from `$OPENROUTER_API_KEY` or
a `.openrouter_key` file in the project root. Without `--llm` the app is entirely
offline apart from EUR-Lex retrieval.

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

Articles only. Annexes, recitals of the amending act, and languages other than
English are out of scope. Output documents follow the typography of the Official
Journal — Times New Roman, 10pt justified body, centred article headings.

See `PLAN.md` for the design and the reasoning behind it.
