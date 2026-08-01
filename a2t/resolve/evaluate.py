"""Measure the LLM tier against the rule engine.

The rule engine resolves this corpus completely, so there are no naturally unresolved
instructions to test the fallback on. Instead the tier is evaluated by **ablation**:
real instruction text is handed to the model with the rules' answer withheld, and its
choice is compared against the verified rule-engine node.

This measures agreement on genuine text with real candidate sets. Three honest limits:

* The baseline is the rule engine, not an independent authority.
* Instructions the plausibility check already flagged are excluded — their correct
  answer is itself disputed.
* Only `REPLACE_NODE` and `DELETE_NODE` are scored. For those, exactly one node is the
  answer. Containers, inserts and appends resolve to a *scope* rather than a target, so
  "Article 3c" and "Article 3c / paragraph 1" are both defensible replies and agreement
  would measure convention, not correctness.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from a2t.model.instruction import Op
from a2t.model.tree import Act, Node
from a2t.resolve.llm import LlmResolver, build_candidates
from a2t.resolve.resolver import Resolution


# Operations whose target is exactly one node, so agreement is meaningful.
SCORABLE = {Op.REPLACE_NODE, Op.DELETE_NODE}


@dataclass
class Trial:
    instruction_id: str
    expected: str
    chosen: str | None
    agreed: bool
    confidence: float
    rejected: str | None
    candidates: int


@dataclass
class Evaluation:
    trials: list[Trial] = field(default_factory=list)

    @property
    def scored(self) -> list[Trial]:
        return [t for t in self.trials if t.rejected is None]

    @property
    def agreed(self) -> list[Trial]:
        return [t for t in self.trials if t.agreed]

    @property
    def agreement(self) -> float:
        return len(self.agreed) / len(self.scored) if self.scored else 0.0

    @property
    def disagreements(self) -> list[Trial]:
        return [t for t in self.scored if not t.agreed]

    @property
    def abstentions(self) -> list[Trial]:
        return [t for t in self.trials if t.rejected is not None]

    def summary(self) -> str:
        return (
            f"{len(self.agreed)}/{len(self.scored)} agreed "
            f"({self.agreement:.1%}); {len(self.abstentions)} abstained or rejected"
        )


def evaluate(resolutions: list[Resolution], act: Act, resolver: LlmResolver,
             limit: int | None = None) -> Evaluation:
    """Ask the model to resolve instructions the rules already resolved."""
    evaluation = Evaluation()

    for resolution in resolutions:
        if limit is not None and len(evaluation.trials) >= limit:
            break
        node = resolution.node
        if node is None or resolution.needs_review:
            continue  # disputed baseline: not a fair test
        if resolution.instruction.op not in SCORABLE:
            continue  # the answer is a scope, not a single target node
        scope: Node = node.parent or act.root
        options = build_candidates(scope)
        if len(options) < 2:
            continue  # nothing to choose between

        choice = resolver.choose(resolution.instruction, scope, options)
        chosen_node = _resolve_candidate(scope, choice)
        evaluation.trials.append(
            Trial(
                instruction_id=resolution.instruction.id,
                expected=node.citation,
                chosen=chosen_node.citation if chosen_node else None,
                agreed=chosen_node is node,
                confidence=choice.confidence,
                rejected=choice.rejected,
                candidates=len(options),
            )
        )
    return evaluation


def _resolve_candidate(scope: Node, choice) -> Node | None:
    if choice.candidate is None:
        return None
    from a2t.resolve.llm import _walk

    return _walk(scope, choice.candidate.path)
