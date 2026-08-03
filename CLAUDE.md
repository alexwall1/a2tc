# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A2T converts an EU legislative amendment proposal (a COM document, PDF) into Word
documents with real tracked changes. The base text is the consolidated legislation as
it stood when the proposal was drafted, retrieved from EUR-Lex; the tracked changes are
the amendments the proposal makes to it. Everything — which acts are amended, their
CELEX ids, the version to use — is derived from the PDF.

See `README.md` for the user-facing description and `PLAN.md` (untracked working notes,
present locally) for the design reasoning behind each phase.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/a2t convert proposal.pdf -o out/     # the whole pipeline
.venv/bin/pytest                               # full suite
.venv/bin/pytest tests/test_resolve.py -q      # one module
.venv/bin/pytest tests/test_parse.py::test_name # one test
.venv/bin/pytest -q -rs                        # show why tests skipped
```

`pdftotext` (poppler-utils ≥ 0.42) must be on the PATH. There is no linter or
formatter configured.

Each pipeline stage has its own command for inspecting what it produced —
`a2t detect|retrieve|extract|parse|resolve|diff PDF`. `a2t parse --show 1.15.b.i` and
`a2t diff --show 1.15.b.i` print one instruction in full; use them rather than writing
throwaway scripts. `a2t llm-eval` measures the LLM tier against the rule engine, and
`a2t corpus triples|fetch|run` builds and runs a ground-truth corpus of adopted
amending acts.

### Test data

Sample PDFs are gitignored (public EUR-Lex documents, large, not ours) and live in
`~/Dokument/A2T-samples/`. Tests skip themselves when their input is absent, so a fresh
clone runs clean; most of the suite is then skipped. To exercise it, symlink the
samples into the project root under the names the tests expect and populate the
retrieval cache under `out/cache`:

```bash
ln -sf ~/Dokument/A2T-samples/amendment_proposal_ets.pdf amendment_proposal_ets.pdf
ln -sf "$HOME/Dokument/A2T-samples/amending proposal intermodal.pdf" "amending proposal intermodal.pdf"
.venv/bin/a2t retrieve amendment_proposal_ets.pdf --cache out/cache
```

`tests/test_heldout.py` runs against COM(2023) 702 final (intermodal transport), which
was never used while building the pipeline. It is the guard against over-fitting to the
ETS proposal; when adding a rule to handle something in the ETS document, check the
held-out tests still pass.

## Pipeline

Every command composes the same stages in the same order (`a2t/cli.py` is the only
place they are wired together):

```
extract   pdf.extract_lines            pdftotext -layout -> Line(text, page, lineno)
          deboilerplate.operative_part strip page furniture, isolate the enacting terms
          preamble.preamble            (--recitals only) the amending act's own front matter
detect    targets.detect_targets       which acts are amended; celex.py -> CELEX ids
retrieve  versions.select_version      pin the consolidation in force at the proposal date
          eurlex_builder.fetch         eurlex-builder ETL -> HTML + Parquet, cached
          assemble.assemble            HTML -> Act tree of Nodes
parse     segment.segment              operative part -> tree of Instructions + payloads
          rules.classify_all           instruction sentence -> Op, scope, target, payload
resolve   resolver.resolve_block       instruction target text -> a Node of the base act
          fallback.apply_llm_fallback  optional: closed-set LLM choice for what failed
apply     changeset.build_changeset    resolutions -> Changes (old text / new text)
          diff.diff                    old/new -> word- or block-level Edit spans
emit      document.emit_document       the act in reading order with revisions over it
          report.write_report          out/report.md, every instruction accounted for
```

Two trees, kept distinct: `model/tree.py` is the *base act* (Act/Node/Kind — what the
law says), `model/instruction.py` is the *proposal* (Instruction/Op/TargetBlock — what
the proposal says to do). `model/path.py` addresses the first the way the second refers
to it: keyed steps ("Article 10a", "point (h)") and ordinal steps ("the second
subparagraph", "penultimate" = -2).

## Invariants

These are load-bearing; changing them changes what the tool guarantees.

**Nothing is composed.** Replacement text is always the payload verbatim from the PDF.
No stage writes legal language; text edits only substitute a phrase the instruction
quoted for another phrase the instruction quoted.

**Nothing is dropped silently.** Everything the pipeline cannot handle surfaces twice —
in `out/report.md` and as a Word comment at the provision concerned. `ChangeKind.SKIPPED`
exists so a skipped change is still reported. `Resolution.doubt` and
`Change.needs_review` carry the flags through.

**The LLM tier never authors anything.** It does closed-set selection only: candidates
come from the real tree (`path.candidates`), the reply is schema-validated, and a
`candidate_id` not in the list is rejected rather than repaired (`resolve/llm.py`). The
LLM classifier (`parse/llm_classify.py`) may state intent, but its scope/target strings
must be verbatim substrings of the instruction sentence. A rejected reply lowers
coverage; it can never corrupt output. Off by default (`--llm`); without it the only
network access is EUR-Lex retrieval.

**Layout is trusted only where it is internally consistent.** Instruction nesting comes
from marker type and sequence, never from indentation — see the counter-examples in
`parse/segment.py`. Inside a single payload the geometry *is* consistent, so
`model/payload.py` uses indentation there to recover article/paragraph/point structure.
Some paragraph breaks appear in neither, which is why `extract/pdf.py` reads poppler's
`-bbox-layout` analysis in addition to its text and instructions carry
`payload_layout` / `payload_breaks`.

**The base act tree comes from HTML, not Parquet.** `eurlex-builder`'s splitter absorbs
multi-letter points into `(z)` and does not split unnumbered subparagraphs, and its
whitespace normalisation makes those boundaries unrecoverable. `full_text_html` (hence
`store_raw_html: true`, required) is the structural source; the Parquet text units are
a cross-check only, reconciled in `model/reconcile.py` with two known-expected
divergences.

**Version pinning is not optional.** Retrieving the wrong consolidation silently
misplaces every ordinal target. `retrieval/versions.py` discovers the real
consolidations and selects by start date; `publication_warning` flags the window where
the version in force had not yet been published, and `retrieval/acquire.py` falls back
loudly when a consolidation has no English expression.

**Retrieved titles are verified.** A constructed CELEX is a hypothesis; `title_matches`
confirms it against the retrieved act, and a mismatch skips the act rather than
converting the wrong legislation.

**OOXML python-docx has no API for is written directly.** Revisions (`w:ins`/`w:del`),
comments and footnotes are all built as raw elements in `emit/tracked_docx.py`. The
footnotes part is created and related there too — `CT.WML_FOOTNOTES`, `RT.FOOTNOTES`
and `XmlPart` are all in the library even though the footnote API is not, so "python-docx
can't do it" is rarely the end of the question.

**Three separate footnote populations, only two handled.** A proposal's *preamble*
notes come from the PDF geometry (`--recitals` only). A *payload's* notes are the ones
the segmenter splits off below a `______` rule — or, in the held-out proposal, below no
rule at all — and `emit/document.py:_Notes` matches their `*`/`**`/`(*)` markers back
into the inserted text; markers are consumed in document order because drafters reuse
`*` for every note on one payload. The *base act's own* footnotes (42 in the
consolidated ETS Directive) are still dropped: `assemble.py` never captures the
`p.footnote` bodies and `clean_text` strips the `href="#E…"` references, so a stray
literal marker like `*1` can survive in node text with nothing behind it.

**Recitals are printed, never resolved.** An amending act carries its own recitals and
leaves untouched those of the act it amends — verified across every act amending
Directive 2003/87/EC, none of which contains a recital-amending instruction, and
consolidated texts on EUR-Lex carry no preamble at all. So `extract/preamble.py` parses
and `emit/document.py` prints; no other phase has an opinion about it. `--recitals` is
off by default because the paragraphs are inserted but are not amendments: accepting
them leaves the base act carrying a preamble that adopting the proposal would not give
it. Rejecting them still returns the act as it stands, which is what keeps the flag
safe to offer at all.

## Conventions

Comments explain *why*, with the observed case that motivated the rule — usually a
specific provision or defect in a real document. Module docstrings carry the design
argument for that module and are the fastest way in; read them before the code.

Commit messages are a sentence in the imperative describing the behaviour, not the
patch: "Keep the paragraph breaks a payload's own line spacing shows".

Scope is articles of the amended acts, English only. Annexes, recitals of the amending
act, and other languages are out of scope. Output typography follows the Official
Journal (Times New Roman, 10pt justified body, centred article headings) — tested in
`tests/test_emit.py`, which asserts against the OOXML directly.
