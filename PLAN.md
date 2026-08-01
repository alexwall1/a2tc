# A2T — Amendments to Tracked changes

Convert EU legislative amendment proposals into Word documents with tracked changes,
where the base text is the consolidated legislation and the tracked changes represent
the proposed amendments.

## 0. Status — where we are

**The app is functional.** One command converts a proposal end to end:

```bash
cd ~/Dokument/A2T
.venv/bin/a2t convert amendment_proposal_ets.pdf --llm -o out/ets-llm
```

Output: one `.docx` per amended act with real Word tracked changes, plus `report.md`
listing every instruction and what became of it. ~2 minutes, mostly base-act retrieval.

| Proposal | Resolved | Changes | Flagged |
|---|---|---|---|
| ETS (COM(2026) 616) | 111/111 with `--llm` (109/111 without) | 91 + 3 | 9 |
| Intermodal (COM(2023) 702) | 11/11 | 13 | 6 |

286 tests pass. Emitted documents are verified three ways: OOXML revision structure,
LibreOffice semantic round-trip, and accepting all changes reproducing the amended act.

### The one step that remains before trusting it

**A human must read one converted proposal against its source.** There is no automated
substitute — see the limitation below. Everything else is polish.

### Why agreement with the Official Journal cannot validate this app

A Commission proposal is a *negotiating position*. Council and Parliament amend, drop
and add before adoption, so the published consolidation is **not** "base act + the
Commission's proposal". Disagreement between our output and a later OJ text is the
*correct* outcome for a proposal, not a defect.

The corpus (§14) therefore measures something narrower: whether applying an **adopted**
amending act reproduces the consolidation that followed it. That is a real test of the
mechanics — parsing, targeting, payload cutting, application — and it found four
genuine defects. It says nothing about proposal conversion.

**Corpus work is stopped.** It has yielded its value and now measures a use case the
users will not have.

### What the corpus bought (kept for the record)

Four defects that would have corrupted proposal output too:
enumerated payloads applied whole to each target; unreadable targets silently widening
to whole articles; payloads swallowing footnotes and page furniture; an instruction
losing its auxiliary verb in the source. Final corpus numbers: 100% documents parsed,
98.3% classified, 71.3% resolved by rules (90.0% with the LLM tier), 38.1% agreement.

---

## 1. Scope

**Input:** one PDF containing an EU amendment proposal (COM document).

**Output:** one `.docx` per amended act, containing the act's articles with real Word
tracked changes (`w:ins` / `w:del`), plus a reconciliation report.

The app derives everything else — which acts are amended, their CELEX references, and
the base text — from the PDF alone.

### Decisions taken

| Decision | Choice |
|---|---|
| Recitals | Out of scope — because this proposal amends none, not because they're unavailable. `eurlex-builder` does supply recitals (`include_recitals`, and `fetch_original_recitals_for_consolidated` pulls them from the original act for consolidated texts). Cheap to enable later. |
| Diff granularity | Word-level token diff, falling back to whole-block delete+insert below a similarity threshold. |
| Base act version | Consolidated version whose **start date** is the latest at or before the proposal's date (CELEX sector `0`). Start date is EUR-Lex's own version discriminator: date of application, or entry into force where none is given. Where that version was published *after* the proposal — EUR-Lex consolidates weeks in arrears — the drafters cannot have read it, and both the report and the document's front matter say so. |
| Detection UX | Auto-proceed; every detection decision recorded in the report. |
| Document styling | The Official Journal's own typography, measured off `consolidated_ets.pdf` rather than guessed: Times New Roman throughout, 10pt justified body, chapter headings 8pt, article numbers italic and titles bold, all headings centred. Held in named `OJ *` paragraph styles (`tracked_docx.OJ_STYLES`), never on runs — a revision replaces runs, so run-level formatting would vanish when a change is accepted. The ►M15◄ consolidation markers and their margin column are **not** reproduced: tracked changes already say what they say. An inserted article's heading is split out of its payload and centred when the instruction names one article ("the following Article 3a is inserted"); payloads inserting several at once ("Articles 9a, 9b, 9c and 9d") stay in one block — segmenting those is parsing work, not styling. |
| Unresolved instructions | Always emit a document; flag in-place as Word comments **and** in the report. Never drop silently. |

### Out of scope (v1)

- Annexes. Annex amendments edit tables by row ordinal ("rows twenty-six, twenty-seven",
  "the rows inserted after the twenty-eight row"). Different problem, and the Annex
  document is not part of the proposal PDF.
- Recitals of the amending act (new free-standing text, not changes to anything).
- Languages other than EN.

---

## 2. Base-act retrieval — `eurlex-builder`

Resolved by inspecting the v0.1.1 wheel from PyPI. Python 3.11+, MIT, `pip install
"eurlex-builder[all]"`. Our environment is Python 3.12.3. **Pin `==0.1.1`** — it is a
0.x library and we depend on its output schema.

It is a **batch ETL pipeline**, not a request/response API: write a YAML config → run
the pipeline → read Parquet. The adapter owns that round-trip.

### 2.1 What it gives us

`text_units.parquet` carries exactly the addressing coordinates our amendment grammar
needs:

```sql
CREATE TABLE text_units (
    id, celex_id, type, subtype, number,
    paragraph_num, subparagraph_num, point_letter,   -- structural coordinates
    unit_order, unit_key, title, text, text_translated
);
```

- `article_granularity: "point"` decomposes to lettered points.
- `subparagraph_num` is populated — ordinal subparagraphs (`"1"`, `"2"`) are addressable,
  which is what `the first subparagraph` / `the second subparagraph` instructions need.
- `unit_order` gives document order; `unit_key` gives stable identity.
- `include_recitals` / `include_articles` / `include_annexes` toggle unit types.
- `relations.parquet` has relation types including `amends` and `proposes_to_amend`.

**Consolidated version pinning works**, which our base-version decision depends on.
The date-suffixed CELEX form is supported:

```python
# eurlex_builder/utils.py
def convert_consolidated_to_original(celex_id: str) -> str:
    """Example: 02016R0679-20210101 -> 32016R0679"""
```

So `02003L0087-20240301` addresses a specific consolidated version — the same one as
the reference PDF (`02003L0087 — EN — 01.03.2024 — 016.001`). Selecting the version in
force at the proposal date becomes: resolve available versions, pick the latest with
entry-into-force ≤ proposal date, build the suffixed CELEX.

### 2.2 What we still build ourselves

| Need | Status |
|---|---|
| Article / paragraph / point / subparagraph addressing | **provided** |
| Tree assembly from flat rows | **ours** — rows are flat; we group by coordinates into the §6 tree |
| Sentence-level addressing (`the penultimate sentence`, `second sentence`) | **ours** — no sentence granularity exists; derived lazily, legal-text-aware |
| Unnumbered-subparagraph splitting | **ours** — see below |
| Multi-letter points `(aa)`–`(ai)` | **ours** — see below |

**Resolved in Phase 0** (see `PHASE0.md`). The splitter has two limitations: it splits
points by single letter only (`(aa)`–`(ai)` get absorbed into the `(z)` unit), and it
does not split unnumbered subparagraphs — and since `normalize_string()` collapses all
whitespace, those boundaries are unrecoverable from the Parquet text.

Both dissolve by setting `store_raw_html: true` and parsing
`works.parquet.full_text_html`, where EUR-Lex uses a stable semantic class vocabulary
(`p.norm` = subparagraph, `p.modref` = `▼M` provenance markers,
`p.title-article-norm` = article heading). Verified: Article 9 → 4 subparagraphs
matching the proposal's "fourth subparagraph" target; Article 3 → all 34 points `a`–`ai`.

So `model/assemble.py` treats `full_text_html` as the **primary** structural source and
`text_units.parquet` as coordinates plus cross-check. No internal APIs are used.

### 2.3 Known limitation that does *not* affect us

The README lists **"Broken: Modern proposals and staff working documents"**. This looks
alarming but is irrelevant here: we never ask the library to read the proposal. We parse
the proposal PDF ourselves and only ask the library for **base acts** — directives,
regulations and decisions, all listed as *Validated*.

---

## 3. What the source documents actually look like

Findings from `amendment_proposal_ets.pdf` (COM(2026) 616 final) and
`consolidated_ets.pdf` (CELEX `02003L0087`, version 016.001). Line numbers refer to
`pdftotext -layout` output.

### 3.1 Document boundaries

| Region | Lines | Handling |
|---|---|---|
| Explanatory memorandum | 1–3129 | discard |
| **Operative part** | **3130–5563** | **parse** |
| Signature block | 5563–5570 | discard |
| Legislative financial statement | 5571–7097 | discard |

Delimiters: `HAVE ADOPTED THIS DIRECTIVE:` → `Done at Brussels,`.
Boilerplate: 140 lines matching `^EN\s+(\d+\s+)?EN$`, plus per-page footnote blocks.

### 3.2 Target detection

51 distinct legal acts are cited in the document. **2** are amendment targets. Detection
must therefore be structural, not citation-based.

Targets appear in exactly one syntactic position — an operative article header plus an
anchor sentence:

```
Article 1
Amendments to Directive 2003/87/EC
Directive 2003/87/EC is amended as follows:

Article 2
Amendments to Decision (EU) 2015/1814
Article 1 of Decision (EU) 2015/1814 is amended as follows:
```

Filtering on this position yields exactly the 2 real targets.

Note the second: the target is **`Article 1` of** the Decision, not the whole act.
A target is therefore `(act, root_scope)`, and its child instructions address paragraphs
*of that article*.

**The document contradicts itself about its own targets:**

| Source | Acts named |
|---|---|
| Cover title (line 19) | 2003/87/EC, 2015/1814 |
| Operative part | 2003/87/EC, 2015/1814 |
| Financial statement §1.1 (line 5648) | + Regulation (EU) 2015/757, Regulation (EU) 2023/1805 |

The operative part is authoritative. Title and memorandum are corroboration; the
financial statement is not operative. Disagreements are **reported, not resolved**.

### 3.3 CELEX resolution

Year/number order flips between citation styles, and this document contains both:

| Citation | Year | Num | CELEX |
|---|---|---|---|
| `Directive 2003/87/EC` | 2003 | 87 | `32003L0087` |
| `Decision (EU) 2015/1814` | 2015 | 1814 | `32015D1814` |
| `Regulation (EC) No 219/2009` | 2009 | **219** | `32009R0219` |
| `Decision No 1359/2013/EU` | 2013 | **1359** | `32013D1359` |
| `Directive 96/61/EC` | 19**96** | 61 | `31996L0061` |

Resolution order:

1. **ELI URI from footnotes** (primary) — `eli/{dir,reg,dec,reg_del,…}/{year}/{num}`
   maps mechanically to CELEX. Present for both targets:
   `eli/dir/2003/87`, `eli/dec/2015/1814`.
2. **Citation grammar** (fallback) — `No ` prefix signals number-first; plausible-year
   heuristic (1950…now+1) disambiguates; 2-digit years expanded.
3. **Verify after retrieval** — retrieved act's title must match the cited title.
   Mismatch aborts that target rather than producing a wrong document.

Consolidated form: sector `3` → `0` (`32003L0087` → `02003L0087`), selecting the
version whose start date is the latest at or before the proposal date.

**Extraction hazard:** footnote markers glue onto citation numbers in `pdftotext`
output — `Regulation (EU) 2019/33141` is really `2019/331` + footnote `41`
(lines 1396, 1735). Step 3 above is what catches these.

### 3.4 Amendment grammar

| Operation | Example (line) |
|---|---|
| replace block | `paragraph 6 is replaced by the following:` (3255) |
| container | `Article 3 is amended as follows:` → `(a) point (e) is replaced…` (3151) |
| delete block | `point (p) is deleted;` (3196) |
| delete phrase | `in point (ag), the words '…' are deleted;` (3225) |
| inline token replace | `in Article 4, 'Article 27' is replaced by 'Articles 27 and 27a'.` (3752) |
| date replace | `the date "31 December 2030" is replaced by "31 December 2038".` (3506) |
| append | `the following points are added:` (3227) |
| insert article | `the following Article 3gaa is inserted:` (3524) |
| anchored insert | `after Article 3gaa, the following Article 3gab is inserted:` (3695) |
| positional insert | `the following subparagraph is added after the second subparagraph:` (4124) |
| ordinal target | `the penultimate sentence is replaced by the following:` (3507) |
| heading edit | `in the title of Chapter III the word 'stationary' is deleted;` (3771) |
| sentence-scoped | `in paragraph 1, second sentence, '2030' is replaced by '2040';` (4440) |
| multi-target | `in paragraphs 3-e, 3-d, 3-c, and 3-b, the date '31 December 2030' is replaced…` (4962) |

### 3.5 Real defects in the source (drives the LLM tier)

These are present in this one document and each gets a named normalisation rule and a
regression test:

| Defect | Location | Handling |
|---|---|---|
| `in paragraph 2 is amended as follows` — ungrammatical | 4142 | strip leading `in ` before `is amended as follows` |
| `in Article 9, the following paragraphs are added` — restates a scope already active | 3793 | redundant re-scope: idempotent if equal to current scope; ambiguity if not |
| two list items lettered `(a)` | Art. 3 transposition | positional index, not letter, as identity |
| `[N.B.: date of application may need to be changed…]` inline editorial note | 5047 | strip, record in report |
| footnotes embedded inside quoted payloads after `___________` | 3172, 3245 | split from payload, attach as footnote |
| inconsistent payload terminators (`'` / `';` / `'.`) | throughout | fence on next instruction marker, not on punctuation |
| quote marks are asymmetric — 121 `‘` vs 140 `’` in the operative part, the excess being apostrophes (`Member States’`) | throughout | confirms the above: quote counting cannot delimit payloads |
| the date-replacement instruction uses **double** quotes while every other instruction uses single | 3506 | accept both quote families in the grammar |
| 10 lines end in a hyphen | operative part | line-join must not blindly splice; check against a hyphenated-compound list |
| nonstandard citation forms (`Regulation 2021/1119/EU`) | throughout | tolerant citation grammar |

---

## 4. Pipeline

```
amendment PDF
  ↓
[1] extract        pdftotext -layout / pdfplumber → line model with coordinates
[2] deboilerplate  strip page furniture; isolate operative part
[3] detect         structural target detection → [(citation, root_scope, line)]
[4] resolve        ELI → CELEX; fallback citation grammar; cross-check signals
[5] retrieve       eurlex-builder adapter → base act tree   [verify title match]
[6] segment        split operative part into nested instruction units + payloads
[7] parse          rule engine → Instruction{op, target, payload, confidence}
[8] resolve paths  Instruction.target → node in base tree
      └─ miss / ambiguity ─────────────────────────────┐
[9] LLM tier                                            │  closed-set selection
      OpenRouter → JSON → validated against real nodes ─┘
[10] apply         build amended tree; record change set
[11] diff          word-level token diff, block fallback
[12] emit          .docx with w:ins/w:del + comments; report.md
```

Steps 1–4 need no network. `a2t detect` stops after step 4.

---

## 5. Module layout

As built. ~5 100 non-blank lines; the irreplaceable mechanical core (retrieval,
assembly, diff, OOXML) is roughly 900 of them.

```
a2t/
  cli.py                  extract / detect / parse / retrieve / resolve / diff /
                          convert / llm-eval / corpus
  config.py               .openrouter_key loading, model selection
  textdiff.py             tokenise + similarity, shared by resolve and emit
  extract/
    pdf.py                pdftotext -> lines, with two-column detection
    deboilerplate.py      page furniture, OJ headers, footnotes, operative bounds
  detect/
    targets.py            structural target detection, proposal date
    celex.py              ELI + citation grammar -> CELEX
  retrieval/
    base.py               provider protocol, title verification   <- adapter boundary
    eurlex_builder.py     YAML -> Pipeline -> parquet, cached per pinned CELEX
    versions.py           Cellar SPARQL; start dates, and when each was published
    acquire.py            fall back to the nearest usable version, loudly
  model/
    tree.py               Act / Chapter / Article / Paragraph / Point / Subparagraph
    assemble.py           full_text_html -> tree; BOTH EUR-Lex templates
    path.py               addressing, ordinals, candidate enumeration
    subdivide.py          sentence derivation (no such granularity upstream)
    instruction.py        Instruction, Op, TargetBlock
    reconcile.py          cross-check the tree against text_units.parquet
  parse/
    segment.py            instruction boundaries + structural payload cutting
    rules.py              grammar -> Instruction        (fast path; do not grow)
    normalise.py          the drafting defects in §3.5
    llm_classify.py       LLM classification            (measured at parity)
  resolve/
    resolver.py           phrase -> node, scope accumulation, plausibility check
    llm.py                OpenRouter client, closed-set selection
    fallback.py           offer unresolved targets to the LLM tier
    evaluate.py           ablation harness: LLM vs the rule engine
  apply/
    changeset.py          resolutions -> concrete edits (tree is never mutated)
    diff.py               word-level diff with whole-block fallback
  emit/
    tracked_docx.py       w:ins / w:del writer over python-docx; OJ paragraph styles
    document.py           render the act with revisions and comments
    report.py             reconciliation report
  corpus/
    discover.py           find (base, amending act, published result) triples
    fetch.py              EUR-Lex download, cached and rate-limited
    run.py                run the pipeline over a corpus; rank failures

tests/                    303 tests, no network; skip when fixtures absent
```

---

## 6. Data model

```python
# tree
Act(celex, title, type, in_force_date)
└ Chapter(num, title)?
  └ Article(id="10a", title)
    └ Paragraph(id="1a")
      ├ Subparagraph(index)      # unnumbered → ordinal-addressed
      │ └ Sentence(index)        # derived lazily, only when addressed
      └ Point(id="e")
        └ Subpoint(id="i")

# instruction
Instruction(
    id           = "1.15.b.i",       # hierarchical position in the proposal
    raw_text     = str,
    source_line  = int,
    scope        = Path,             # accumulated from enclosing containers
    op           = Op,
    target       = Path | Ordinal | Multi,
    payload      = str | None,       # verbatim from PDF, never model-generated
    footnotes    = list[str],
    confidence   = float,
    resolver     = "rule" | "llm",
    notes        = list[str],
)

Op = REPLACE_NODE | DELETE_NODE | INSERT_NODE | APPEND_NODE
   | REPLACE_TEXT | DELETE_TEXT | CONTAINER
```

**Scope accumulation** is what makes nesting work:
`(15) Article 10a is amended as follows:` → `(b) paragraph 1a is amended as follows:`
→ `(i) the second subparagraph is replaced by the following:` resolves to
`Article 10a / paragraph 1a / subparagraph[2]`.

---

## 7. LLM tier

**Role:** classify instructions the rule engine could not resolve. Nothing else.

**Invariants:**

- The model **never writes legal text.** Payloads are always verbatim PDF extracts.
- The model performs **closed-set selection**: it is given the instruction, its
  enclosing scope, and the list of *valid candidate paths from the actual retrieved
  tree*. It returns a choice from that list.
- Output is strict JSON against a schema, `temperature=0`, validated against real tree
  nodes. An invalid node reference is rejected outright.
- One retry on invalid output, then the instruction is flagged and left unresolved.

This makes a hallucinated amendment structurally impossible: an unresolvable
instruction becomes a visible flag, never a silent wrong edit.

**Transport:** OpenRouter, OpenAI-compatible endpoint. Key from `.openrouter_key`
(bare `sk-or-v1…` token, single line), overridable by `OPENROUTER_API_KEY`.
Model configurable; default to a small fast model. Responses cached by instruction hash
so re-runs are free and deterministic.

---

## 8. Tracked-changes writer

`python-docx` has **no** tracked-changes API — `w:ins` / `w:del` must be written into
the XML directly:

- inserted run → wrapped in `<w:ins w:id w:author w:date>`
- deleted run → wrapped in `<w:del …>`, and `<w:t>` becomes `<w:delText>`
- deleted/inserted paragraph marks need the corresponding `w:rPr` markers on the
  paragraph mark, or Word renders the change but not the paragraph merge

Approach: a thin writer over `python-docx` that owns run construction and revision ids,
rather than post-processing a finished document.

Word **comments** require a `comments.xml` part plus `commentRangeStart/End` and
`commentReference` runs. Recent `python-docx` may support this; **verify early**, and
fall back to manual part injection if not. This is on the critical path for the
"flag in place" decision, so it gets checked in Phase 0.

Every emitted document opens in Word with Review → Track Changes showing a normal
accept/reject flow. Validation includes opening output in Word/LibreOffice.

---

## 9. Build phases

| Phase | Deliverable | Verifiable by |
|---|---|---|
| **0. Spikes** ✅ | **done — see `PHASE0.md`.** Tracked-changes writer built and verified 3 ways; comments work via `python-docx` 1.2.0 (no manual XML); base act retrieved at a pinned consolidated version, 8/10 real targets resolved, remaining 2 solved via `full_text_html` | accept-all-changes reproduces the amended text exactly |
| **1. Extract** ✅ | **done.** `a2t/extract/`, `a2t extract` CLI, 16 tests | operative part == lines 3130–5563 exactly; 49 footers + 1 footnote block removed; all 11 payload footnotes kept |
| **2. Detect** ✅ | **done.** `a2t/detect/`, `a2t detect` CLI, 32 tests | exactly 2 targets, correct CELEX via ELI, `Article 1` scope on the Decision, 2015/757+2023/1805 reported as the single discrepancy |
| **3. Retrieve** ✅ | **done.** `a2t/retrieval/`, `a2t/model/`, `a2t retrieve` CLI, 58 tests | Art. 10a paragraph sequence matches the PDF exactly; all 34 Art. 3 points and Art. 9's 4 subparagraphs recovered; 0 unexpected divergences vs Parquet |
| **4. Parse** ✅ | **done.** `a2t/parse/`, `a2t parse` CLI, 52 tests | 35 + 3 top-level, 122 instructions, **122/122 classified by rules alone**; every §3.4 op and §3.5 defect covered |
| **5. Resolve** ✅ | **done.** `a2t/resolve/`, `a2t resolve` CLI, 28 tests | **114/114 (100%) resolved by rules alone**; 6 flagged for review, incl. a confirmed numbering error in the proposal |
| **6. LLM tier** ✅ | **done.** `a2t/resolve/llm.py`, `fallback.py`, `evaluate.py`, `a2t llm-eval` + `a2t resolve --llm`, 33 tests | ablation on real instructions: **95% agreement** with the verified rule engine (two models, identical scores); invalid ids, low confidence and abstentions all rejected, never applied |
| **7. Apply + diff** ✅ | **done.** `a2t/apply/`, `a2t diff` CLI, 28 tests | a one-date change renders as **-1 +1 words at 99% similar**; 91 + 3 changes built; 87% of replacements stay word-level |
| **8. Emit** ✅ | **done.** `a2t/emit/document.py`, `report.py`, `a2t convert` CLI, 19 tests | both documents pass 11 structural checks; LibreOffice recognises 264 ins / 220 del / 21 comments; **accepting all changes reproduces the amended act** (10/10 provision checks) |
| **9. Harden** ✅ | **done.** COM(2023) 702 (intermodal) converted end to end; 19 held-out tests | 7 generalisation gaps found and fixed; both corpora convert with 0 unresolved and 0 unplaced changes; **285 tests pass** |

Phases 1–5 need no network and no LLM.

---

## 10. CLI

```bash
a2t convert amendment_proposal_ets.pdf -o out/
# detected targets:
#   Directive 2003/87/EC     → 02003L0087  (ELI)   whole act
#   Decision (EU) 2015/1814  → 02015D1814  (ELI)   scope: Article 1
# note: financial statement §1.1 also names Regulation (EU) 2015/757,
#       Regulation (EU) 2023/1805 — not amended in operative part; ignored
# wrote out/02003L0087_tracked.docx, out/02015D1814_tracked.docx, out/report.md

a2t detect  <pdf>                     # detection only, no network
a2t convert <pdf> --target 02003L0087 # restrict to one act
a2t convert <pdf> --no-llm            # rule engine only; flag everything unresolved
a2t convert <pdf> --model <id>        # override OpenRouter model
a2t convert <pdf> --dump-instructions # parsed instruction tree as JSON
```

---

## 11. Testing

- **Golden fixtures** — cached base-act trees; no network in the test suite.
- **Grammar corpus** — every §3.4 pattern and every §3.5 defect as a named case.
- **Property**: applying an instruction then reading back the node yields the payload.
- **Invariant**: payload text in the output is byte-identical to the PDF extract
  (guards against the LLM ever contributing text).
- **Coverage metric** — % of the 38 real instructions resolved by rules alone, tracked
  as a number that should go up.
- **Manual gate** — output opened in Word, changes accepted, result compared against
  the proposal read by hand.

---

## 12. Risks

| Risk | Mitigation |
|---|---|
| `eurlex-builder` is v0.1.1; output schema may change | pin `==0.1.1`; depend on the documented Parquet schema, not internals; adapter boundary keeps the blast radius to one module |
| ~~Unnumbered subparagraphs under-split~~ | **closed in Phase 0** — structure parsed from `full_text_html`; no internal APIs |
| Consolidated version in force at proposal date can't be resolved | date-suffixed CELEX is supported; fall back to latest consolidated **with an explicit warning in the report and in the document**, never silently |
| Version in force at the proposal date wasn't published until after it | EUR-Lex consolidates in arrears (~3–6 weeks, sometimes years), so a proposal drafted just after a start date was written against the *previous* text. Both readings are defensible, so selection is unchanged and the disagreement is **flagged in the report and on the document**, naming the version that was readable and `--as-of` to switch |
| Base-act structure doesn't match what drafters assumed | title+structure verification; mismatches reported per target |
| PDF extraction noise (glued footnotes, hyphenation, columns) | verify-after-retrieve; consider pdfplumber for coordinate-aware extraction |
| Ordinal addressing ("penultimate sentence") is fragile | sentence splitting is legal-text-aware and only applied when addressed; **plausibility check** compares the replacement against the addressed node and flags low overlap for review |
| Silent wrong edits | closed-set LLM, verbatim payloads, everything unresolved is visibly flagged |

---

## 13. Path to the web app

The pipeline is already a pure function `PDF → (documents, report)` with the only
side effects at the edges (retrieval, OpenRouter). For the web version:

- keep `a2t/` as a library; CLI and web are both thin callers
- retrieval cache and LLM cache become shared services
- long conversions run as jobs; the report becomes the review UI
- the flagged-instruction list is the natural place for human-in-the-loop correction,
  and corrections are training data for better rules

No architectural change required — this is why the LLM tier is kept narrow and the
document model is explicit.

---

## 14. Corpus findings (post-Phase 9)

`a2t corpus` builds a self-validating test set from **adopted** amending acts: where
exactly one amending act separates two consolidations of the act it amends, the EU has
published the correct outcome. 16 usable triples from 4 base acts; 6 pairs rejected
because a single consolidation absorbed several amending acts.

### The measurement that mattered

| Baseline | Resolved |
|---|---|
| Before the assembler fix | 23/224 (10.3%) |
| After | **141/224 (62.9%)** |

The first number was not measuring the grammar. Consolidations before ~2024 use an
older EUR-Lex template with **no `eli-subdivision` wrappers**; the assembler keyed
entirely on the new one and silently produced an act with zero articles, so every
instruction was counted as an unresolved target. One document went from 5/83 to 76/83.

`assemble()` now raises `AssemblyError` rather than returning an empty tree, and
supports both templates. An act with no articles is a retrieval failure, never a
resolution failure.

### Rules vs LLM classification

Measured on identical inputs, one stage swapped:

| | Rules | LLM (`gpt-5-nano`) |
|---|---|---|
| Resolved | 141/224 (62.9%) | 136/217 (62.7%) |
| Flagged | 17 | 13 |
| Agreement with published consolidation | 4/16 | 4/14 |
| Invented spans reaching the pipeline | — | **0** (17 rejected, fell back) |

**Parity.** Classification was never the bottleneck — 9 unclassified out of 249. The
verbatim-substring guard worked perfectly, so the LLM path is safe to keep as an
option, but there is no evidence it is better. Both numbers were dominated by the
assembler defect.

### LLM at the resolution stage — the stage that mattered

| | Rules only | Rules + LLM resolution |
|---|---|---|
| Resolved | 141/224 (62.9%) | **203/224 (90.6%)** |
| Recovered by the tier | — | 62/78 attempted (79.5%) |
| Invalid ids returned | — | **0** (16 abstentions) |
| Agreement with published consolidation | 4/16 (25.0%) | 8/36 (22.2%) |

Coverage rose sharply and the accuracy *rate* held: of the 20 provisions that became
comparable only because the tier resolved them, 4 matched — about the same rate as the
rule-resolved baseline. The model never returned an id outside the candidate set, and
abstained 16 times rather than guessing.

Cost is latency, not money: ~15-20 s per call, because each prompt carries up to 60
candidate provisions. 78 calls for this corpus.

**Conclusion: use the LLM at resolution, keep the grammar at classification.**

### The wider corpus — 64 triples, 16 base acts

Expanded from 4 base acts to 16 by querying Cellar for the most-consolidated acts and
sampling substantive legislation across domains: VAT, banking (CRR), securities
(MiFID II), insurance (Solvency II), environment, energy, waste, chemicals (REACH),
cosmetics, pharma, aviation, agriculture.

| | 16 triples | 64 triples |
|---|---|---|
| Documents parsed | 100% | 100% |
| Classified | 96.4% | 98.3% (1 940/1 973) |
| Resolved | 62.9% | **74.1%** (1 161/1 566) |
| Agreement | 25.0% (n=16) | **24.9% (n=445)** |

Resolution *improved* on a 4x larger and more diverse corpus, so the earlier figure was
depressed by an ETS-heavy sample. Agreement held at 24.9% on 28x the comparisons — it
is no longer dismissable as small-n.

The wider corpus also exposed **65 unplaced changes** (built but never rendered), which
was 0 on both earlier corpora, and a cluster of failures on deep point paths
(`point (b) / point (vii)`) in dense acts such as MiFID II.

### The agreement question — answered by inspection

Agreement sat at ~25% and it was unclear whether the output was wrong or the metric
unfair. Five low-agreement cases were examined by hand. Two genuine defects, both
silent:

**Enumerated payloads were not split.** "paragraphs 5 and 5a are replaced by the
following:" is followed by *one* quoted block holding both new paragraphs. The whole
block was applied to each target, so paragraph 5 received its own text plus 5a's, and
5a received paragraph 5's. The block splits cleanly at its own numbering — verified
against the published paragraph 5, which is 1 430 characters where the split falls at
1 433.

**An auxiliary verb is missing from the official text.** Directive (EU) 2023/959,
Article 2(1)(d) reads `paragraph 7 replaced by the following:` — not "*is* replaced".
The operation pattern required the auxiliary, so instruction (d) was not recognised as
an instruction at all and its text was absorbed into the previous instruction's
payload: one amendment lost, another corrupted, no error raised.

Effect on that document: 2/4 matched -> **5/5 matched**.

Effect on the corpus:

| | before | after |
|---|---|---|
| Resolved | 74.1% | 74.2% |
| **Agreement** | **24.9%** (111/445) | **30.3%** (135/446) |

Coverage barely moved, which is the point — these were correctness bugs hiding inside
successful-looking output. **The metric is trustworthy and is now the primary KPI.**

### Four inspection rounds — what they found

Each round examined five low-agreement cases by hand. Coverage barely moved; agreement
rose 13 points.

| Fix | Kind | Agreement |
|---|---|---|
| baseline | | 24.9% (111/445) |
| enumerated payloads split per target | real defect | 30.3% (135/446) |
| unreadable targets refused, not widened | real defect | 32.5% (133/409) |
| payload fenced at its closing quote; inline furniture stripped | real defect | **38.1% (156/409)** |
| provision numbers and quote glyphs normalised before comparing | metric correction | (included above) |

**An unreadable target must never widen to its container.** `points (b) and (c) of
point (10)` could not be parsed, so the target silently became the whole of Article 3 —
a 523-character replacement for the Definitions article, shadowing six correct sibling
edits. The same guard caught `the sole footnote is deleted`, which had been *deleting
the entire subparagraph* containing the footnote. Coverage fell 2.9 points; that is the
right trade, because a refused amendment is a flag and a mistargeted one is a document
that looks finished.

**Payloads must end at their own closing quote.** Fencing only on the next instruction
marker let footnotes and `ELI:` lines fall inside quoted legislation — general to every
two-column OJ act, because reading-order extraction places footnotes inline where the
page-bottom detector never sees them.

**The metric correction** strips a provision's own number and unifies quote glyphs.
Consolidated texts store both differently from an amending act's quotation; neither is
a difference in the law. Recorded as a metric change made *after* seeing failures, and
so the one to hold most loosely.

MiFID II went 5/8 -> 8/8; the ETS Decision 2/4 -> 5/5.

### The LLM resolution tier — coverage yes, correctness no

Measured on a 22-document subset, rules vs rules-plus-tier:

| | resolved | agreement |
|---|---|---|
| rules only | 162/260 (62.3%) | 10/50 (20.0%) |
| + LLM tier | **234/260 (90.0%)** | 15/74 (20.3%) |

The tier recovered 72 of 95 unresolved targets (75.8%), declined 23, returned **zero
invalid node ids**. But of the 24 provisions that became comparable only because of it,
5 matched — ~21%, indistinguishable from the rule engine's 20%.

**Its answers are as good as the grammar's and no better.** Enable it because a
resolved-and-flagged amendment is more useful to a reviewer than a silently absent one,
not because it improves correctness. Every recovery carries `resolver="llm"` and its
confidence into the report and the Word comments.

Both methods converging on the same ~20% ceiling points at something **downstream of
resolution** — how edits are applied, or what the comparison measures — rather than at
target-finding.

### Whole-document extraction — tried and rejected

Tested the alternative architecture: give the model a window of the proposal plus an
index of the base act, and have it return edits directly, bypassing segmentation,
classification and resolution entirely.

| | Rules pipeline | Whole-document |
|---|---|---|
| Changes found (ETS) | **91** | 36 (40%) |
| Rejected: citation did not resolve | — | 43 of 86 proposed |
| Rejected: non-verbatim quote | — | 7 |
| Median replace similarity | 0.76 | 0.47 |
| Word-mode diffs | 87% | 42% |

**The decisive finding: verbatim is not complete.** For Article 10(3) the model quoted
3 300 characters where the payload is 6 397. The quote passes a substring check —
a truncated quote *is* a substring — so verification approved a silently shortened
provision. That is worse than a rejected edit, because it looks like success.

This is inherent to asking a model to transcribe legislation (~180 000 characters
across 17 calls, ~107 s each). Payloads cut structurally from the PDF are correct by
construction and cannot truncate.

---

## 15. Refactor plan

The pipeline shape is validated: 64 documents, 16 base acts, 100% parsed. What was
misallocated was effort, not architecture. **Rebalance, do not rebuild.**

### The one structural change

`segment.py` does two jobs. Cutting payloads at structural delimiters is correct by
construction and must stay. Finding instruction boundaries is where numbering variation
bit (`(1)` vs `1.` vs unnumbered).

**The model points; we cut.** Let the LLM identify instruction boundaries when the
marker pass looks wrong, and keep cutting payloads mechanically. This removes the
failure class without weakening the guarantee that emitted text is the source's own.

### Promote

**LLM resolution becomes the default**, not `--llm`: 62.9% -> 90.6%, zero invalid node
ids in 78 calls, 16 abstentions. The single measured win.

Consequence: ~15-20 s per call makes conversion a background job, not a request —
design the web version for that shape now.

### Demote

`parse/rules.py` stays as the free, deterministic first pass, but **stops growing**.
Every new drafting variant goes to the LLM tier, never to another regex. The grammar
becomes a fast path and a consistency check, not the interpreter.

### Deleted

`a2t/whole/` — measured and rejected; the finding stays in §14. Removed, along with
the `spikes/` verification scripts, when the repository was prepared for publication:
both depended on sample PDFs that are no longer committed.

### What is left, in order

1. **Read one converted proposal by hand.** `out/final-ets/` or a fresh
   `a2t convert ... --llm`. Check the 9 flagged changes first — they are listed in
   `report.md` under "Needs human review" and appear as Word comments. This is the
   only real gate; nothing else establishes that the output is right.
2. **Understand the 2 unresolved ETS instructions** (without `--llm`) and the flagged
   ones — notably `1.7.b`, where the proposal itself says "second subparagraph" but
   supplies the third. Confirmed against the official consolidated PDF.
3. **Decide whether `--llm` becomes the default.** It takes ETS from 109/111 to
   111/111 and hard corpus documents from 71% to 90%. Its answers are *as accurate as*
   the grammar's, not more — so it buys coverage, not correctness. Every recovery is
   marked `resolver=llm` with its confidence.
4. **Scope decisions, if needed.** Annexes are deliberately out of scope; recitals are
   supported but untested. Both are choices, not bugs.
5. **Web app.** The pipeline is already a pure function. The LLM tier's ~15-20 s per
   call makes conversion a background job, not a request.

### Known defects, if corpus work ever resumes

* `Article 13(2)` as a scope form is unparsed — only `Article 13 / paragraph 2` works.
* Deep point paths (`point (b) / point (vii)`) — largest cause of unresolved targets.
* 30 corpus documents detect no targets; detection is rules-only, untouched since
  Phase 2.
* Amending acts often apply **in stages**, so a consolidation may contain only part of
  an act — a flaw in the triple design, adjacent to the negotiation point above.

### Protect

The corpus harness. It found the assembler bug, falsified the classification
hypothesis, and killed the whole-document design on evidence rather than argument.
Keep it green; keep growing it.

### The honest summary

The mechanical half of this app earned its place; the interpretive half did not. Keep
everything that guarantees the emitted text is the real law byte for byte — retrieval,
version pinning, assembly, structural payload cutting, diff, OOXML. Let the model do
interpretation.
