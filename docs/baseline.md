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
