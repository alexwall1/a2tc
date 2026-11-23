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
