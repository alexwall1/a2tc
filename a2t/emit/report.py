"""The reconciliation report.

Every instruction in the proposal appears here exactly once, with what became of it.
A conversion is only trustworthy if it can account for the whole source, so the report
is written even when everything succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from a2t.apply.changeset import ChangeKind, ChangeSet
from a2t.apply.diff import summarise
from a2t.emit.document import EmitResult
from a2t.model.instruction import TargetBlock
from a2t.resolve.resolver import ResolutionReport


@dataclass
class ActOutcome:
    block: TargetBlock
    celex: str
    version: str
    title: str
    resolution: ResolutionReport
    changeset: ChangeSet
    emitted: EmitResult | None = None
    version_warning: str | None = None


@dataclass
class RunReport:
    source: Path
    proposal_date: str
    detection_targets: list = field(default_factory=list)
    discrepancies: list = field(default_factory=list)
    outcomes: list[ActOutcome] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    preamble: object | None = None

    def render(self) -> str:
        out: list[str] = []
        add = out.append

        add(f"# Conversion report — {self.source.name}\n")
        add(f"Proposal date: **{self.proposal_date}**\n")

        if self.preamble is not None:
            self._preamble_section(add)

        add("## Acts amended\n")
        add("| Act | CELEX (version) | Scope | Identified via |")
        add("|---|---|---|---|")
        for target in self.detection_targets:
            scope = f"Article {target.root_scope}" if target.root_scope else "whole act"
            version = next(
                (o.version for o in self.outcomes if o.celex == target.celex),
                target.consolidated_celex,
            )
            add(f"| {target.citation.raw} | `{version}` | {scope} "
                f"| {target.celex_source} |")
        add("")

        if self.discrepancies:
            add("### Discrepancies in the source document\n")
            for item in self.discrepancies:
                where = f"line {item.line}" if item.line else "—"
                add(f"- **{item.kind}** ({where}): {item.message}")
            add("")

        if self.failures:
            add("### Acts that could not be converted\n")
            for failure in self.failures:
                add(f"- {failure}")
            add("")

        for outcome in self.outcomes:
            self._act_section(outcome, add)

        self._review_section(add)
        return "\n".join(out) + "\n"

    # ------------------------------------------------------------------ sections

    def _preamble_section(self, add) -> None:
        """What `--recitals` put into every document, and what it means.

        The caveat is stated here rather than left implicit: these paragraphs are
        inserted, so accepting the document's changes produces a base act carrying the
        amending act's preamble — which adopting the proposal would not do.
        """
        front = self.preamble
        add("## Preamble of the amending act\n")
        add(f"Inserted ahead of the articles of every document, from source lines "
            f"{front.source_lines[0]}–{front.source_lines[1]}: "
            f"{front.summary()}.\n")
        add("An amending act keeps its own recitals; it does not pass them to the act "
            "it amends. These paragraphs are therefore marked as insertions but are "
            "not amendments — rejecting every change removes them, accepting every "
            "change leaves the base act carrying a preamble that is not its own.\n")
        for note in getattr(front, "notes", []):
            add(f"- ⚠ {note}")
        if getattr(front, "notes", []):
            add("")

    def _act_section(self, outcome: ActOutcome, add) -> None:
        add(f"## {outcome.block.citation}\n")
        add(f"- Base text: `{outcome.version}` — {outcome.title}")
        for line in (outcome.version_warning or "").splitlines():
            add(f"- **Version warning:** {line}")
        add(f"- Instructions: {len(list(outcome.block.walk()))} "
            f"({len(outcome.block.instructions)} top-level)")
        add(f"- Resolution: {outcome.resolution.summary()}")
        add(f"- Changes: {outcome.changeset.summary()}")
        if outcome.emitted:
            add(f"- Document: `{outcome.emitted.path.name}` "
                f"— {outcome.emitted.summary()}")
        add("")

        add("| Instruction | Line | Operation | Target | Diff | Resolver |")
        add("|---|---|---|---|---|---|")
        for change in outcome.changeset.renderable:
            instruction = change.instruction
            detail = summarise(change.edit) if change.edit else "—"
            add(f"| `{instruction.id}` | {instruction.source_line} "
                f"| {change.kind.value} | {change.citation} | {detail} "
                f"| {instruction.resolver} |")
        add("")

        skipped = [
            c for c in outcome.changeset.skipped
            if c.note and "container" not in c.note
        ]
        if skipped:
            add("### Not applied\n")
            for change in skipped:
                # One instruction can skip at several provisions for the same reason,
                # so the note alone does not say which of them this line is about.
                at = f", {change.citation}" if change.node is not None else ""
                add(f"- `{change.instruction.id}` (line "
                    f"{change.instruction.source_line}){at}: {change.note}")
                add(f"  > {change.instruction.raw[:160]}")
            add("")

    def _review_section(self, add) -> None:
        flagged = [
            (o, c) for o in self.outcomes for c in o.changeset.renderable
            if c.needs_review
        ]
        add("## Needs human review\n")
        if not flagged:
            add("Nothing flagged.\n")
            return
        add(f"{len(flagged)} change(s) carry a flag. Each is also a Word comment "
            f"anchored at the provision concerned.\n")
        for outcome, change in flagged:
            instruction = change.instruction
            add(f"### `{instruction.id}` — {outcome.block.citation}, line "
                f"{instruction.source_line}\n")
            add(f"> {instruction.raw[:200]}\n")
            add(f"- Applied to: {change.citation}")
            for note in instruction.notes:
                add(f"- {note}")
            if change.note:
                add(f"- {change.note}")
            add("")


def write_report(report: RunReport, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.render(), encoding="utf-8")
    return path
