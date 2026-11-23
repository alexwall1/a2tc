# Corpus baseline

A measurement to steer by, taken on 2026-11-22 at the commit that first counted an
unread instruction as unresolved. Later changes should not lower these figures on the
same corpus.

## What was run

`docs/baseline-triples.tsv` lists 87 triples: a consolidated version, the one adopted
act that amended it, and the consolidation that followed. They are every usable triple
`a2t corpus triples` finds for 18 base acts chosen to spread across policy areas
(emissions trading, transport, energy, waste, accounting, financial markets, VAT,
medical devices, consumer rights, data protection). 12 of the 83 amending acts could
not be downloaded, so 74 documents were run.

```bash
a2t corpus fetch $(cut -f3 docs/baseline-triples.tsv | sort -u)
a2t corpus run --triples docs/baseline-triples.tsv
```

The input here is the amending act as adopted, in Official Journal layout, not a COM
proposal. The instructions are the same; the page layout is not.

## Figures

| | |
|---|---|
| Documents parsed | 74 of 74 |
| Documents in which no amended act was detected | 27 |
| Acts converted | 77 of 87 |
| Instructions | 1526 |
| Resolved | 953 of 1419 applicable (67.2%) |
| Flagged for review | 69 |
| Agreement with the published consolidation | 190 of 289 compared (65.7%) |

## Where it fails

Grouped from the ranked list in the run's report. One instruction can appear under
more than one heading only where noted.

| Count | What happened |
|---|---|
| 308 | The instruction names a provision the assembled base act does not contain (281 single, 27 enumerated) |
| 79 | The target phrase could not be parsed |
| 44 | No rule read the operation (33), or the instruction sits inside one that was not read (11) |
| 34 | No addressable target could be derived |
| 27 | Whole documents in which no amended act was detected |
| 10 | Acts not converted: no articles found in the HTML (6), no consolidation at or before the act's date (2), version returned no HTML (2) |
| 7 | A change could not be placed |

### The largest single cause

Older consolidations are assembled with no numbered paragraphs at all: each paragraph
becomes an unnumbered subparagraph, so "Article 13(1)" has nothing to resolve to.
Regulation (EC) No 561/2006 as of 2015 assembles to 29 articles, 0 paragraphs and
0 points; the same act as of 2020 gives 73 paragraphs and 96 points. The same split
shows in Regulation (EU) 2015/757 (2016 against 2023), Directive 2011/83/EU (2018
against 2022) and Regulation (EU) 2017/745 (2020 against 2023).

Splitting the triples by how the version being amended was assembled, and counting the
first act each document amends:

| Version being amended | Triples | Resolved |
|---|---|---|
| Has numbered paragraphs | 38 | 624 of 786 (79.4%) |
| Has no numbered paragraphs | 17 | 110 of 281 (39.1%) |
| Assembly failed | 4 | 33 of 34 |
| Not in the cache under the triple's own version (fallback used, or nothing detected) | 28 | 129 of 175 |

A proposal drafted today is converted against a recent consolidation, which is the
well-assembled kind, so this cause weighs more on the corpus than on ordinary use. It
still has to be fixed first: until it is, it hides every other cause in a third of
the corpus.

## Caveats

- The 12 missing documents are not missing from EUR-Lex. Its PDF address answered with
  an empty HTML page, which `corpus fetch` reports as "may not exist in EN". The same
  documents are served by Cellar.
- Agreement is measured only on whole-provision replacements and text edits, at 0.95
  similarity after normalising markers and quotes (`a2t/corpus/run.py`). Insertions
  and deletions are not compared.
- Eleven of the triples amend Directive 2003/87/EC, the act the rules were developed
  against. They are in the figures.

## After reading the older template's paragraphs, points and indents

Same triples, rerun at `05b6f1a`. This time 87 of the 87 documents downloaded, so the
full figures are not comparable with the table above; the comparison below is on the 71
documents that ran both times.

| | Before | After |
|---|---|---|
| Resolved | 903 of 1348 (67.0%) | 1091 of 1348 (80.9%) |
| Agreement with the published consolidation | 178 of 275 (64.7%) | 245 of 357 (68.6%) |

No document resolved fewer instructions or agreed on fewer provisions than before.
The agreement figures have different denominators: more instructions now resolve, so
more of them can be compared. Full run on all 87 documents: 1241 of 1512 resolved
(82.1%), 287 of 407 in agreement (70.5%).

What remains, largest first: 235 unresolved targets in total, 37 unclassified
instructions, 34 documents in which no amended act is detected, 34 with no addressable
target and 34 whose target phrase could not be parsed.

## The 34 documents in which no amended act is detected

Read from each document's first amending sentence, after the second run.

| Kind | Documents | Example opening |
|---|---|---|
| Amends an annex only (out of scope) | 13 | "Annex VIII to Directive 2012/27/EU is replaced by the text in Annex I to this regulation" |
| The article is itself the instruction, with no "is amended as follows" container | 16 | "Article 97 of Directive 2006/112/EC is replaced by the following:", "In Article 12 of Directive 2003/87/EC, the following paragraphs are added:", "The following Article is inserted in Directive 2006/112/EC:" |
| Container with a lead-in the anchor does not accept | 2 | "Accordingly, Regulation (EC) No 1257/96 is hereby amended as follows:" |
| Other: amends several acts at once, or the operative part is not isolated | 3 | |

So detection is not one narrow fix. Only the second row is a regex, and it is two
documents. The largest group is an amending article that carries a single instruction
naming the act inside its target phrase, which the segmenter has no block for.

## After detecting annex-only documents, linking-word anchors and single-instruction articles

Rerun at `427c629`, all 87 documents.

| | Previous run | This run |
|---|---|---|
| Documents with no amended act found and no explanation | 34 | 9 |
| Documents reported as amending annexes only (out of scope) | 0 | 15 |
| Resolved | 1241 of 1512 (82.1%) | 1260 of 1526 (82.6%) |
| Agreement with the published consolidation | 287 of 407 (70.5%) | 290 of 410 (70.7%) |

The totals barely move because each newly detected document carries one instruction;
12 documents now produce one each, and the two of them that replace a provision agree
with the published text. Insertions are not compared, so the other nine are unchecked
by the corpus (two were read by hand: Article 129a of Directive 2006/112/EC lands after
Article 129; Article 44(10) of Regulation 2017/745 is replaced in place).

One document looks worse in the log (32019L2177: 46 of 51 resolved to 25 of 30) because
Cellar answered 503 while listing versions of Directive 2015/849 on that run; it is the
same code both times. Corpus runs should be repeated, or retried per act, before
a drop of that size is believed.

Still undetected (9), by reading the first lines of each: an omnibus act amending some
60 acts, one article per act ("Accordingly, … is hereby amended as follows:", 32009R0219);
operative parts the isolator does not bound; a sentence whose verb wraps onto the second
line (32009R1073: "the following para-graph shall be inserted"); and decisions whose
amendment is worded in the recital.

## After reading compact citations, refusing unreadable scopes and fixing the comparator

Rerun at `e42032c`, all 87 documents.

| | Previous run | This run |
|---|---|---|
| Resolved | 1260 of 1526 (82.6%) | 1329 of 1547 (85.9%) |
| Agreement with the published consolidation | 290 of 410 (70.7%) | 364 of 429 (84.8%) |
| Instructions refused because their scope cannot be read | not counted | 31 |

The agreement rise has two causes and they are not the same kind of improvement. Scoring
the same 429 compared changes with both comparators:

| Comparator | Agreeing |
|---|---|
| The one used before this commit | 308 of 429 (71.8%) |
| The one used now | 364 of 429 (84.8%) |

So about one point of the rise is conversion: 19 more changes can be compared and 18 more
of them agree under the old measure, because "In Article 27(3), …" now lands on Article
27(3). The other thirteen points are the measure no longer counting presentation as
error: a paragraph number or note marker in the payload, "CO2" against "CO 2", hyphen
variants, an article heading the consolidation keeps apart, and a replacement that the
consolidation prints as several sibling provisions.

Why resolved rose when the plan expected it to fall: refusing an unreadable scope removes
the wrong placements (they were counted as resolved), but reading compact citations
resolved more instructions than were refused.

Before the change, six of twenty sampled disagreements were placements on the wrong
provision — the base act's Article 1, second subparagraph — with no flag. They are now
placed correctly where the citation is compact, and refused with a stated reason where
the scope is still unreadable. The 31 refusals are the next list to read: each is a
scope wording the parser does not know.

## After refusing containers and scopes the tree cannot hold, and not letting a scope swallow quoted words

Rerun at `6a6a663`, all 87 documents.

| | Previous run | This run |
|---|---|---|
| Resolved | 1329 of 1547 (85.9%) | 1332 of 1514 (88.0%) |
| Agreement with the published consolidation | 364 of 429 (84.8%) | 370 of 435 (85.1%) |

Read the first row with care: 33 fewer instructions count as applicable, because those
that sit inside an annex are now reported as "amends an annex — out of scope" and leave
the denominator, as annex-by-reference instructions already did. Only three more
instructions resolved. Most of the 2.1 points is the denominator, not conversion.

What changed underneath: instructions inside a container that names an annex, or
anything else the tree cannot hold, are no longer resolved against the act root; each is
now reported with the reason. The refusals say what is missing —
"names a provision kind the model does not have (title)" accounts for 10 of them, the
list of what to build for the 2006/112/EC amendments that insert titles, chapters and
sections.

Still the largest gaps: 151 unresolved targets, 32 unclassified instructions (28 "no
rule matched"), 15 documents that amend annexes only, 9 in which no amended act is found.

## After annexes by reference became external, and two more replacement wordings

Rerun at `f3f0f2c`, all 87 documents.

| | Previous run | This run |
|---|---|---|
| Resolved | 1332 of 1514 (88.0%) | 1334 of 1490 (89.5%) |
| Agreement with the published consolidation | 370 of 435 (85.1%) | 372 of 437 (85.1%) |

As predicted: 24 annex-by-reference instructions left the applicable count and two more
instructions resolved ("replaced as follows", "replaced by the words ‘…’"). Of the 1.5
points, 1.4 are the denominator. The instructions no rule matches fell from 28 to 4, and
each of those now says why (`inserted before`, no title kind).

### The two proposals the tool is for, before and after this session

The corpus holds adopted acts, so the two proposals in `out/samples` were converted with
the code as it stood before this session's first change (`66888c0`) and with `f3f0f2c`,
against the same retrieved base acts.

- COM(2026) 616 (ETS): `report.md` and both Word documents are identical.
- COM(2023) 702 (intermodal): one difference, and it is a correction. Points (l) and
  (m) inserted into Article 9(1) of Regulation (EU) 2020/1056 were placed at the very end
  of the document, after the final article, because "In Article 9(1)" could not be read
  and the insertion fell back to the act root. They now follow point (k) of Article 9(1).
  The other act's document is identical.

## After keeping unsupported operations visible, and the small guarantee fixes

Rerun at `6a75e07`, all 87 documents.

| | Previous run | This run |
|---|---|---|
| Resolved | 1334 of 1490 (89.5%) | 1340 of 1496 (89.6%) |
| Agreement with the published consolidation | 372 of 437 (85.1%) | 372 of 437 (85.1%) |
| Unclassified instructions | 28 | 8 |
| Blocks (one per act and scope) | 142 | 138 |

What these changes were for is not in the percentages:

- An instruction using a verb the segmenter did not know — "is repealed", "become
  paragraphs 3 and 4", "is renumbered", "is moved", "is substituted" — used to be read as
  a continuation of the payload above it, and vanished. Each is now an instruction that is
  reported as unresolved with its own reason ("renumbering and moving provisions is not
  supported", "'repealed' is not read as a deletion"). "Article 6 shall read as follows:"
  is read as a replacement. None of these occurs in the 87-document corpus on an
  instruction line, which is why the figures do not move; a synthetic document
  reproduced the loss, and it took the following instruction with it.
- An act nothing was consolidated for by the proposal date, but which has later
  consolidations, now falls back to the act as adopted (it raised before). Two corpus acts
  were in this position.
- An act amended in two articles of one proposal is one block, so one document; the four
  fewer blocks are such cases. Blocks that amend different scopes of an act stay apart,
  and their documents no longer share a file name.
- `report.md` lists the proposal's own articles (transposition, entry into force) as not
  converted. `convert` exits non-zero if any act was not converted. `diff` and `llm-eval`
  report a missing PDF, date or act as an error, and `retrieve` uses the same acquisition
  and fallbacks as `convert`.
- Both sample proposals produce identical Word documents.

### What titles, sections and annexes would unlock, from this run

| | Instructions | Documents |
|---|---|---|
| Inside or by reference to an annex | about 55 | 15 amend annexes only |
| Titles, sections, subsections, "introductory wording" | about 24 | 5 acts did not retrieve (Directive 2006/112/EC, which inserts titles and sections) |

Most of the title and section instructions come from one act family, the VAT Directive.
Annex instructions are spread over many acts. Neither appears in the two sample proposals.

## After ranges, sub-point citations, keys beside a subparagraph and introductory words

Rerun at `2e04cb4`, all 87 documents. The denominator is the same as in the previous run
(1496), so this rise is conversion and not bookkeeping.

| | Previous run | This run |
|---|---|---|
| Resolved | 1340 of 1496 (89.6%) | 1426 of 1496 (95.3%) |
| Compared with the published consolidation | 437 | 500 |
| Agreeing | 372 (85.1%) | 427 (85.4%) |
| Flagged for review | 80 | 90 |

86 more instructions resolved. 63 of them can be compared with the published text, and
55 of those agree (87%) — about the same as the rest, so the new resolutions are not
mostly wrong placements. The other 23 are deletions and insertions, which are not
compared. Ten more changes carry a review flag, which is the "counted across the whole of
…" note on a point found beside its subparagraph.

What was added, by instructions affected in the previous run's list:

- Introductory words ("the introductory phrase is replaced by the following:"), read as
  the provision's own text before its list, without touching the points below.
- A key looked for beside an unnumbered subparagraph ("in the first subparagraph, point
  (a)"): the tree keeps points on the paragraph, not on the block that introduces them.
- Runs of provisions ("paragraphs 12 to 18", "Articles 7a to 7e", "points (b) to (g) and
  (i) and (n)"), expanded against the act's own siblings; a missing end is refused.
- A point cited with its sub-point ("point (b)(iii)"), long roman keys ("point (xxiv)"),
  a point inside a point ("point (g), point (i)") and chapters numbered with digits.

Both proposals convert to identical reports and Word documents after these changes.

## After treating a chapter title as a title, and reporting what cannot be shown

Rerun at `462988d`, all 87 documents.

| | Previous run | This run |
|---|---|---|
| Resolved | 1426 of 1496 (95.3%) | 1426 of 1496 (95.3%) |
| Agreement with the published consolidation | 427 of 500 (85.4%) | 427 of 500 (85.4%) |
| Changes unplaced in the document | 11 | 3 |
| Flagged for review | 90 | 88 |

The figures that count did not move; what moved is what the output says. The eleven
unplaced changes were not an emitter problem:

- Eight were a regression this work had introduced, and a bug that predated it for roman
  numerals: "the title of Chapter 8 is replaced by the following:" was read as replacing
  the *chapter*, articles and all, so every edit inside it was reported as impossible to
  show. It is now a title edit and the chapter is left alone. The resolved count does not
  change because the instruction was counted as resolved either way.
- One was the same provision replaced twice: Directive 2025/516 replaces the first
  subparagraph of Article 222 of Directive 2006/112/EC in its Article 3 and again in its
  Article 5. Only one can be shown against the base text; the other is now flagged with
  the reason instead of being dropped without a word.
- Two, in the same directive's Article 262, are replaced in full by a later article.
  That was already reported and still is.

Any change no provision accepts now carries a note ("not rendered — no provision in the
document accepted this change") and therefore appears in the report under review, and a
test asserts it. The three that remain unplaced are the legitimate ones above.

## Real proposals as test data

`docs/proposals.tsv` lists 30 Commission proposals amending other acts (COM documents, 5
each from 2018, 2020, 2021, 2022 and 2023, two or three from the other years), chosen
at random within each year from the Publications Office's listing of "Proposal for a …
amending …" titles. They are fetched with

    a2t corpus fetch $(cut -f1 docs/proposals.tsv) --dir out/proposals
    a2t corpus run out/proposals/*.pdf

and `tests/test_proposals.py` checks detection and segmentation of each (skipped when the
files are absent). They have no ground truth: a proposal is not what was adopted, so the
run measures coverage, not agreement.

| | First run | After the fixes below |
|---|---|---|
| Acts converted | 38 of 39 | 43 of 43 |
| Instructions | 865 | 911 |
| Resolved | 655 of 704 (93.0%) | 729 of 784 (93.0%) |

What reading real proposals found, none of it visible in the 87 adopted acts or the two
samples:

- **An act named only in its article's heading** ("Amendments to Regulation (EU)
  2018/1862", then "(1) In Article 2, paragraph 2 is replaced by the following:") was not
  read at all — 2 of 30 proposals. The heading is now the anchor when nothing in the
  article names the act.
- **"Regulation (EC) 1257/96"**, number first with no "No" and a two-digit year, parsed
  to nothing, and its title could not confirm the act even once it did.
- **Base acts retrieved in the wrong language.** When Cellar has no English text the
  retrieval library silently supplies another language and records it only in a column.
  Ten acts in the cache were Dutch, German, Italian, French or Spanish; "Artikel 1"
  matches no article pattern, so instructions were looked for in the wrong act or none
  was assembled. Such an act is now treated as having no English consolidation and falls
  back as one does. This also moved the adopted-act corpus.

Adopted-act corpus after the language fix: 1470 of 1546 resolved (95.1%) with 130 of 142
acts converted (from 121), agreement unchanged at 427 of 500.

EUR-Lex's PDF address returns an empty HTML page for some documents that exist, which is
why 12 corpus documents never downloaded; `corpus fetch` now falls back to the Publications
Office's own resolver, which serves them.

Still unread among the 30: three annex-only proposals (reported as such) and one decision
that adjusts amounts without an amending sentence.
