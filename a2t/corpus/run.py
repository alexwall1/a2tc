"""Run the pipeline over a corpus and report where it fails.

Two questions, answered separately:

**Coverage** — what did the pipeline fail to handle? Every stage already self-reports
(unclassified operations, unresolved targets, plausibility flags, unplaced changes), so
this needs no human review. The output is a ranked list of what to fix next.

**Correctness** — where a published consolidation exists, is the result right? For each
amended provision the emitted text is compared against the same provision in the
consolidation that incorporated the amendment. This is the only check in the project
that uses an authority outside our own reasoning.
"""

from __future__ import annotations

import collections
import re
from dataclasses import dataclass, field
from pathlib import Path

from a2t.apply.changeset import ChangeKind, build_changeset
from a2t.detect.targets import detect_targets, find_proposal_date
from a2t.extract.deboilerplate import operative_part
from a2t.extract.pdf import extract_lines
from a2t.model.assemble import assemble
from a2t.model.instruction import Op
from a2t.model.tree import Act
from a2t.parse.rules import classify_all
from a2t.parse.segment import segment
from a2t.resolve.resolver import resolve_block
from a2t.retrieval.acquire import acquire
from a2t.retrieval.base import title_matches
from a2t.textdiff import similarity

AGREEMENT_THRESHOLD = 0.95

# Consolidated texts store a provision's own number separately (in `no-parag` or the
# point marker column) and normalise quote glyphs, while an amending act quotes the
# replacement with its number inline and in the source's own quote style. Neither is
# a difference in the law, so both are normalised away before comparing — otherwise
# the metric penalises formatting rather than substance.
_LEADING_MARKER = re.compile(r"^\s*[\(\u2018\u2019]?\s*\d+[a-z]?\s*[\)\.]\s+"
                             r"|^\s*\([a-z]{1,3}\)\s+", re.I)
_QUOTES = str.maketrans({"\u201c": "'", "\u201d": "'", "\u2018": "'",
                         "\u2019": "'", '"': "'"})


def comparable(text: str) -> str:
    """Strip presentation the consolidation stores differently."""
    text = re.sub(r"\s+", " ", text or "").strip().translate(_QUOTES)
    return _LEADING_MARKER.sub("", text).strip()


@dataclass
class ActResult:
    citation: str
    celex: str
    version: str = ""
    instructions: int = 0
    unclassified: int = 0
    resolved: int = 0
    applicable: int = 0
    flagged: int = 0
    changes: int = 0
    unplaced: int = 0
    skipped_notes: list[str] = field(default_factory=list)
    error: str | None = None

    # ground truth, when a published consolidation is available
    compared: int = 0
    agreed: int = 0

    # LLM resolution tier
    llm_attempted: int = 0
    llm_recovered: int = 0


@dataclass
class DocumentResult:
    source: str
    ok: bool = True
    error: str | None = None
    acts: list[ActResult] = field(default_factory=list)
    annexes_only: bool = False  # nothing amended but annexes, which are out of scope

    @property
    def gaps(self) -> list[str]:
        """Everything the pipeline could not handle, as reason strings."""
        out: list[str] = []
        if self.error:
            out.append(f"document failed: {_generalise(self.error)}")
        elif not self.acts:
            # Parsing without finding anything to amend is the worst failure mode:
            # it produces no output and looks like success.
            out.append("amends annexes only (out of scope)" if self.annexes_only
                       else "no amendment targets detected")
        for act in self.acts:
            if act.error:
                out.append(f"act failed: {_generalise(act.error)}")
            out += [f"unclassified instruction" for _ in range(act.unclassified)]
            # An unclassified instruction is unresolved too, and is already listed.
            unplaced_targets = act.applicable - act.resolved - act.unclassified
            out += [f"unresolved target" for _ in range(max(0, unplaced_targets))]
            out += [f"unplaced change" for _ in range(act.unplaced)]
            out += [_generalise(n) for n in act.skipped_notes]
        return out


def _generalise(message: str) -> str:
    """Strip identifiers so equivalent failures group together."""
    import re

    text = re.sub(r"\b\d{4,}\b", "N", message)
    text = re.sub(r"'[^']{0,80}'", "'…'", text)
    return text[:120]


def run_document(pdf: Path, provider, truth: Act | None = None,
                 truth_celex: str | None = None,
                 base_version: str | None = None,
                 classifier=None, llm_resolver=None) -> DocumentResult:
    """Run the full pipeline over one act, collecting failures rather than raising."""
    result = DocumentResult(source=pdf.name)
    try:
        lines = extract_lines(pdf)
        operative = operative_part(lines).lines
        detection = detect_targets(operative, lines)
        result.annexes_only = bool(detection.annexes)
        blocks = segment(operative, detection.targets)
        if classifier is None:
            classify_all(blocks)
        else:
            from a2t.parse.llm_classify import classify_all as llm_classify_all

            llm_classify_all(blocks, classifier)
        on_date = find_proposal_date(lines)
    except Exception as exc:
        result.ok = False
        result.error = str(exc)
        return result

    if on_date is None:
        result.ok = False
        result.error = "no document date found"
        return result

    by_celex = {t.celex: t for t in detection.targets}
    for block in blocks:
        act_result = ActResult(citation=block.citation, celex=block.celex)
        result.acts.append(act_result)
        every = list(block.walk())
        act_result.instructions = len(every)
        act_result.unclassified = sum(1 for i in every if i.op is Op.UNKNOWN)

        target = by_celex.get(block.celex)
        if target is None:
            act_result.error = "no detection target"
            continue
        # A triple states exactly which text the amending act was written against.
        # Deriving it from the document date instead would pick whatever was in force
        # at adoption, which for an act amended in stages is the wrong text.
        try:
            if base_version and target.consolidated_celex == base_version.split("-")[0]:
                retrieved = provider.fetch(base_version)
                pinned = base_version
            else:
                acquired = acquire(provider, target.consolidated_celex, on_date)
                retrieved, pinned = acquired.retrieved, acquired.pinned
        except Exception as exc:
            act_result.error = str(exc)
            continue
        acquired = type("A", (), {"retrieved": retrieved, "pinned": pinned})()
        act_result.version = acquired.pinned
        if not title_matches(target.citation, acquired.retrieved.title):
            act_result.error = "title mismatch"
            continue

        try:
            act = assemble(acquired.retrieved.full_text_html, acquired.pinned,
                           acquired.retrieved.title)
        except Exception as exc:
            act_result.error = str(exc)
            continue
        report = resolve_block(block, act)
        if llm_resolver is not None and report.unresolved:
            from a2t.resolve.fallback import apply_llm_fallback

            outcome = apply_llm_fallback(report, act, llm_resolver)
            act_result.llm_attempted = outcome.attempted
            act_result.llm_recovered = outcome.recovered
        act_result.applicable = len(report.applicable)
        act_result.resolved = len(report.resolved)
        act_result.flagged = len(report.doubtful)

        changeset = build_changeset(report, act)
        act_result.changes = len(changeset.renderable)
        act_result.skipped_notes = [
            c.note for c in changeset.skipped
            if c.note and "container" not in c.note
        ]

        from a2t.emit.document import DocumentEmitter

        emitter = DocumentEmitter(act, changeset)
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            emitted = emitter.emit(Path(tmp) / "out.docx")
        act_result.unplaced = len(emitted.unrendered)

        if truth is not None and block.celex == truth_celex:
            compared, agreed = compare_against(changeset, truth)
            act_result.compared, act_result.agreed = compared, agreed

    return result


def compare_against(changeset, truth: Act) -> tuple[int, int]:
    """Check amended provisions against the consolidation that published them.

    Only replacements and text edits are checked: they name a provision that exists in
    both versions. Insertions create provisions the old tree cannot address, and
    deletions leave nothing to compare.
    """
    compared = agreed = 0
    for change in changeset.renderable:
        if change.kind not in (ChangeKind.REPLACE, ChangeKind.TEXT_EDIT):
            continue
        if change.node is None:
            continue
        published = _same_provision(change.node, truth)
        if published is None:
            continue
        compared += 1
        if similarity(comparable(change.new_text),
                      comparable(published)) >= AGREEMENT_THRESHOLD:
            agreed += 1
    return compared, agreed


def _same_provision(node, truth: Act) -> str | None:
    """Locate a node's counterpart in another version of the same act."""
    from a2t.model.tree import Kind

    chain = []
    current = node
    while current is not None and current.kind is not Kind.ACT:
        chain.append(current)
        current = current.parent
    chain.reverse()

    article = next((n for n in chain if n.kind is Kind.ARTICLE), None)
    if article is None or article.key is None:
        return None
    found = truth.article(article.key)
    if found is None:
        return None

    for step in chain[chain.index(article) + 1:]:
        if found is None:
            return None
        found = (
            found.by_key(step.kind, step.key) if step.key is not None
            else found.by_index(step.kind, step.index)
        )
    return found.full_text if found is not None else None


def summarise(results: list[DocumentResult]) -> str:
    """A ranked table of what the pipeline could not handle."""
    gaps = collections.Counter(g for r in results for g in r.gaps)
    lines = []

    total_acts = sum(len(r.acts) for r in results)
    ok_acts = sum(1 for r in results for a in r.acts if not a.error)
    instructions = sum(a.instructions for r in results for a in r.acts)
    resolved = sum(a.resolved for r in results for a in r.acts)
    applicable = sum(a.applicable for r in results for a in r.acts)
    flagged = sum(a.flagged for r in results for a in r.acts)
    compared = sum(a.compared for r in results for a in r.acts)
    agreed = sum(a.agreed for r in results for a in r.acts)

    lines.append(f"documents      {len(results)} "
                 f"({sum(1 for r in results if r.ok)} parsed)")
    lines.append(f"acts           {total_acts} ({ok_acts} converted)")
    lines.append(f"instructions   {instructions}")
    if applicable:
        lines.append(f"resolved       {resolved}/{applicable} "
                     f"({resolved / applicable:.1%})")
    lines.append(f"flagged        {flagged}")
    attempted = sum(a.llm_attempted for r in results for a in r.acts)
    if attempted:
        recovered = sum(a.llm_recovered for r in results for a in r.acts)
        lines.append(f"LLM tier       recovered {recovered}/{attempted} "
                     f"({recovered / attempted:.1%}) of unresolved targets")
    if compared:
        lines.append(f"agreement with published consolidation  "
                     f"{agreed}/{compared} ({agreed / compared:.1%})")

    lines.append("")
    if not gaps:
        lines.append("no gaps recorded")
        return "\n".join(lines)

    lines.append("gaps, most frequent first:")
    width = max(len(g) for g in gaps)
    for gap, count in gaps.most_common():
        lines.append(f"  {count:4}  {gap:<{width}}")
    return "\n".join(lines)
