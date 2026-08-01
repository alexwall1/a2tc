# Phase 0 — spike results

All three spikes done. Both capabilities are proven and the one open design question
from `PLAN.md` §2.2 is resolved — differently, and better, than the planned fallback.

Reproduce with:

```bash
.venv/bin/python spikes/phase0_docx.py           # build tracked-changes docx
.venv/bin/python spikes/verify_docx.py           # XML structure checks
libreoffice --headless --convert-to fodt --outdir out/ out/phase0_tracked.docx
.venv/bin/python spikes/verify_accept.py         # accept-all-changes check
.venv/bin/eurlex-builder run spikes/phase0_elb.yaml
.venv/bin/python spikes/probe_targets.py         # resolve real amendment targets
```

---

## Spike 1 — tracked changes: **PASS**

`a2t/emit/tracked_docx.py` writes real Word revisions. Verified at three levels:

**Structure** — 5 `w:ins` and 5 `w:del` run revisions, deleted text correctly using
`w:delText` (never `w:t`), paragraph-mark revisions in `w:pPr/w:rPr`, unique revision
ids, author and date on every revision.

**Semantics** — LibreOffice round-trip to flat ODF produced 10 `text:changed-region`
elements (5 `text:insertion`, 5 `text:deletion`) with authorship preserved. The
revisions are understood as revisions, not styled text.

**Behaviour** — accepting all changes reproduces the amended legislation exactly:
Art. 3(h) and 3(o) replacements present, 3(aj) insertion present, 3(p) deletion gone,
no residual old wording.

The word/block threshold works on real text:

| Case | Similarity | Mode |
|---|---|---|
| Art. 3(h) — two localised edits in a long definition | 0.689 | word |
| Art. 3(o) — substantially rewritten | 0.323 | block |

The 0.4 default cleanly separated them.

### Constraint found

`Paragraph.runs` only sees direct `w:r` children, so a run wrapped in `w:ins`/`w:del`
disappears from that view. Comment anchoring needs the `Run` objects, so the writer
returns a `Block(paragraph, runs, mode)` instead of a bare paragraph.

---

## Spike 2 — Word comments: **PASS, no manual XML needed**

`python-docx` 1.2.0 ships `Document.add_comment(runs, text, author, initials)`.
The plan assumed manual `comments.xml` injection; that work is not needed.

Confirmed: **a comment anchors correctly to a run inside a `w:ins` revision** — the
case that matters, since flagged instructions usually sit on changed text. The ODF
round-trip shows `text:change-start` → `office:annotation` → annotated text →
`office:annotation-end` nested correctly.

---

## Spike 3 — `eurlex-builder`: **PASS with two limitations, both solved**

Retrieved `02003L0087-20240301` in ~5 s: 632 text units (602 article, 30 recital),
29 relations. **Version pinning works** — `is_valid_celex` accepts the date-suffixed
form, and Art. 3(h) came back byte-identical to `consolidated_ets.pdf`, confirming we
retrieved the same consolidated version (016.001, 01.03.2024).

Recitals are retrieved for consolidated texts via
`fetch_original_recitals_for_consolidated`, as expected.

`works.parquet` also supplies `title`, `eli`, `date_entry_into_force`,
`date_end_of_validity`, `is_in_force` — directly usable for CELEX verification and
consolidated-version selection.

### Target resolution: 8/10 real instructions

Probed against actual instructions from Article 1 of the proposal. The two failures
exposed two distinct limitations in the library's splitter.

**Limitation A — multi-letter points are not split.** The point splitter handles
single letters only. Article 3 yields 26 units ending at `(z)`; the text of points
`(aa)`–`(ai)` is *absorbed into the `(z)` unit* rather than lost. Nine definitions
affected. This matters directly: the proposal amends point `(ag)` [Art. 1(2)(f)] and
appends `(aj)`–`(ao)` after `(ai)` [Art. 1(2)(g)].

**Limitation B — unnumbered subparagraphs are not split, and the boundaries are
destroyed.** Where a paragraph is numbered, subparagraphs *are* split (Art. 10a(1a)
correctly yields subparagraphs 1–5). Where an article has no numbered paragraphs,
the whole body is one unit — Article 9 is a single 1 881-character unit. Worse,
`normalize_string()` collapses all whitespace, so the unit text contains **no
newlines** and subparagraph boundaries cannot be recovered from the Parquet text.
The proposal addresses `the fourth subparagraph` of Article 9 [Art. 1(12)(a)].

### Resolution — parse `full_text_html`, don't call internals

`PLAN.md` §2.2 anticipated falling back to
`split_article(split_unnumbered_subparagraphs=True)`, coupling us to a non-exported
internal of a 0.x library. **That is not necessary.** Setting `store_raw_html: true`
puts the source HTML in `works.parquet.full_text_html` (691 KB), and EUR-Lex uses a
stable semantic class vocabulary:

| Selector | Meaning |
|---|---|
| `p.title-article-norm` | article heading |
| `div.eli-title` | article title |
| `p.norm` | **subparagraph** — the boundary we need |
| `p.modref` | `▼M9` / `▼M15` provenance markers (strip) |

Both limitations dissolve against this one mechanism:

- **Article 9** → 4 `p.norm` blocks = 4 subparagraphs. The 4th is *"From 1 January
  2026 and 1 January 2027 respectively, the quantity of allowances shall be increased
  to take into account the coverage of…"* — and the proposal's Art. 1(12)(a) replaces
  "the fourth subparagraph" with text opening on exactly that wording. **The ordinal
  target is confirmed correct against the real proposal**, and old/new share an
  opening, making it a textbook word-level diff.
- **Article 3** → 34 point markers recovered, `a` through `ai`, including all nine
  multi-letter points.

**Architectural consequence:** `model/assemble.py` should treat `full_text_html` as
the *primary* structural source and `text_units.parquet` as coordinates and
cross-check, rather than patching around two separate splitter limitations. This stays
entirely on supported output, so pinning `eurlex-builder==0.1.1` remains a
belt-and-braces measure rather than a dependency on internals.

---

## Plan changes

1. §2.2 open item **closed** — HTML-derived structure, no internal-API fallback.
2. Comment injection work **removed** — `python-docx` 1.2.0 covers it.
3. `store_raw_html: true` is now **required** in the retrieval config, not optional.
4. `model/assemble.py` gains a defined job: build the tree from `full_text_html`,
   reconcile against `text_units.parquet`, and report any divergence.
5. Sentence-level splitting (§2.2) is unchanged and still ours to build — `p.norm`
   gives subparagraphs, not sentences.

## Environment

Python 3.12.3 · `python-docx` 1.2.0 · `eurlex-builder` 0.1.1 · polars 1.43.1 ·
lxml · LibreOffice (verification only, not a runtime dependency).
