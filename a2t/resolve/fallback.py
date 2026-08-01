"""Hand unresolved instructions to the LLM tier.

Only instructions the rule engine could not place are offered, and only ever as a
choice between provisions that already exist in the retrieved act. A recovered
resolution is marked `resolver="llm"` and carries the model's confidence, so nothing
downstream mistakes it for a rule-engine result.
"""

from __future__ import annotations

from dataclasses import dataclass

from a2t.model.path import Path, resolve as resolve_path
from a2t.model.tree import Act, Node
from a2t.resolve.llm import LlmResolver, build_candidates
from a2t.resolve.resolver import ResolutionReport


@dataclass
class FallbackOutcome:
    attempted: int = 0
    recovered: int = 0
    declined: int = 0
    rejected: int = 0

    def summary(self) -> str:
        return (
            f"attempted {self.attempted}, recovered {self.recovered}, "
            f"declined {self.declined}, rejected {self.rejected}"
        )


def _scope_for(report_path: Path | None, act: Act) -> Node:
    """The node an unresolved instruction was being looked for inside."""
    if report_path is not None and len(report_path.steps) > 1:
        parent = resolve_path(Path(report_path.steps[:-1]), act)
        if parent is not None:
            return parent
    return act.root


def apply_llm_fallback(report: ResolutionReport, act: Act,
                       resolver: LlmResolver) -> FallbackOutcome:
    outcome = FallbackOutcome()

    for result in list(report.unresolved):
        if result.enumerated:
            # "paragraphs 3 and 4 are deleted" names several provisions. Asking for
            # one would silently drop the rest.
            result.instruction.note(
                "addresses several provisions; not offered to the LLM tier"
            )
            continue
        scope = _scope_for(result.path, act)
        options = build_candidates(scope)
        if not options:
            continue

        outcome.attempted += 1
        choice = resolver.choose(result.instruction, scope, options)

        if choice.candidate is None:
            if choice.rejected and "declined" in choice.rejected:
                outcome.declined += 1
            else:
                outcome.rejected += 1
            result.instruction.note(
                f"LLM tier could not resolve this: {choice.rejected}"
            )
            continue

        node = resolve_path(choice.candidate.path, scope)
        if node is None:
            # Cannot happen for a validated candidate, but never trust it blindly.
            outcome.rejected += 1
            result.instruction.note("LLM tier returned an unusable candidate")
            continue

        outcome.recovered += 1
        result.node = node
        result.path = choice.candidate.path
        result.status = "resolved"
        result.reason = None
        result.instruction.resolver = "llm"
        result.instruction.confidence = min(
            result.instruction.confidence, choice.confidence
        )
        result.instruction.note(
            f"resolved by the LLM tier to {node.citation} "
            f"(confidence {choice.confidence:.2f}): {choice.reason}"
        )
    return outcome
