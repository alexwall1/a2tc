"""Turn what an instruction *says* into the node it *means*.

Instructions are relative. "the second subparagraph is replaced by the following"
only means something inside the scope its ancestors established:

    (15) Article 10a is amended as follows:          -> Article 10a
      (b) paragraph 1a is amended as follows:        -> Article 10a / paragraph 1a
        (i) the second subparagraph is replaced ...  -> ... / subparagraph 2

So resolution walks the instruction tree carrying a context path, extended by each
container and by any `in X,` scope prefix on the instruction itself.

Nothing is guessed. A target that does not resolve is reported unresolved and handed
to the LLM tier as a closed-set choice, never approximated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from a2t.model.instruction import Instruction, Op, TargetBlock
from a2t.model.path import Path, Step, parse_ordinal, resolve
from a2t.model.tree import Act, Kind, Node
from a2t.textdiff import similarity

KIND_NOUNS = {
    "article": Kind.ARTICLE,
    "articles": Kind.ARTICLE,
    "paragraph": Kind.PARAGRAPH,
    "paragraphs": Kind.PARAGRAPH,
    "subparagraph": Kind.SUBPARAGRAPH,
    "subparagraphs": Kind.SUBPARAGRAPH,
    "point": Kind.POINT,
    "points": Kind.POINT,
    "sentence": Kind.SENTENCE,
    "sentences": Kind.SENTENCE,
    "chapter": Kind.CHAPTER,
    "subpoint": Kind.SUBPOINT,
}

# "Article 10a", "paragraph 1a", "point (ag)"
KEYED = re.compile(
    r"^(?:the\s+)?(?P<noun>[a-z]+)\s+\(?(?P<key>\d+(?:-?[a-z]+)?|[a-z]{1,3})\)?$", re.I
)
# "the second subparagraph", "the penultimate sentence", "second sentence"
ORDINAL = re.compile(
    r"^(?:the\s+)?(?P<ordinal>[a-z]+(?:-to-last)?|\d+(?:st|nd|rd|th))\s+"
    r"(?P<noun>subparagraph|sentence|paragraph|point|indent)$",
    re.I,
)

ORDINAL_LIST = re.compile(r"^(?:the\s+)?(?P<noun>[a-z]+)\s+(?P<keys>.+)$", re.I)


# Below this old/new overlap, a replacement is unlikely to be addressing the node
# the instruction named.
PLAUSIBLE = 0.35


@dataclass
class Resolution:
    instruction: Instruction
    path: Path | None = None
    node: Node | None = None
    status: str = "unresolved"  # resolved | unresolved | not-applicable
    reason: str | None = None
    plausibility: float | None = None
    doubt: str | None = None
    targets_title: bool = False
    # An instruction may address several provisions at once ("in paragraphs 3-e,
    # 3-d, 3-c, and 3-b, ..."). Each is resolved; `node` stays None.
    nodes: list = field(default_factory=list)
    enumerated: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "resolved"

    @property
    def targets(self) -> list:
        """Every node this instruction applies to."""
        if self.nodes:
            return list(self.nodes)
        return [self.node] if self.node is not None else []

    @property
    def needs_review(self) -> bool:
        return self.doubt is not None


@dataclass
class ResolutionReport:
    results: list[Resolution] = field(default_factory=list)

    @property
    def applicable(self) -> list[Resolution]:
        return [r for r in self.results if r.status != "not-applicable"]

    @property
    def resolved(self) -> list[Resolution]:
        return [r for r in self.results if r.ok]

    @property
    def unresolved(self) -> list[Resolution]:
        return [r for r in self.applicable if not r.ok]

    @property
    def doubtful(self) -> list[Resolution]:
        return [r for r in self.resolved if r.needs_review]

    @property
    def coverage(self) -> float:
        total = len(self.applicable)
        return len(self.resolved) / total if total else 1.0

    def summary(self) -> str:
        return (
            f"{len(self.resolved)}/{len(self.applicable)} resolved "
            f"({self.coverage:.1%}); {len(self.unresolved)} unresolved, "
            f"{len(self.doubtful)} needing review"
        )


TITLE_OF = re.compile(r"^(?:the\s+)?title\s+of\s+(?P<inner>.+)$", re.I)
# "the title is replaced by the following" — the act's own heading.
OWN_TITLE = re.compile(r"^the\s+title(?:\s+of\s+this\s+\w+)?$", re.I)

# "paragraphs 3-e, 3-d, 3-c, and 3-b" — one instruction, several targets.
PLURAL_HEAD = re.compile(
    r"^(?:the\s+)?(?P<noun>[a-z]+s)\s+\(?(?P<key>\d+(?:-?[a-z]+)?|[a-z]{1,3})\)?$", re.I
)
BARE_KEY = re.compile(
    r"^(?:and\s+)?\(?(?P<key>\d+(?:-?[a-z]+)?|[a-z]{1,3})\)?$", re.I
)


def parse_enumeration(text: str | None) -> tuple[Kind, list[str]] | None:
    """Parse a phrase addressing several provisions at once.

    Returns `(kind, keys)` when the phrase names more than one, else None. Such a
    phrase has no single node, so it must be reported rather than collapsed onto
    whichever member happens to parse.
    """
    if not text:
        return None
    fragments = [f.strip() for f in re.split(r",|\band\b", text) if f.strip()]
    if len(fragments) < 2:
        return None

    head = PLURAL_HEAD.match(fragments[0])
    if head is None:
        return None
    kind = KIND_NOUNS.get(head.group("noun").lower())
    if kind is None:
        return None

    keys = [head.group("key").lower()]
    for fragment in fragments[1:]:
        match = BARE_KEY.match(fragment)
        if match is None:
            return None
        keys.append(match.group("key").lower())
    return kind, keys


def targets_title(text: str | None) -> bool:
    """"in the title of Chapter III ..." edits the heading, not the body."""
    return bool(text) and TITLE_OF.match(text.strip()) is not None


def parse_phrase(text: str | None) -> list[Step]:
    """Parse a scope or target phrase into steps.

    Handles compound phrases such as "paragraph 1, second sentence", which appear as a
    single `in ...,` prefix in the source.
    """
    if not text:
        return []
    cleaned = re.sub(r"\s+", " ", text.strip().rstrip(":;., "))
    title = TITLE_OF.match(cleaned)
    if title:
        cleaned = title.group("inner")
    steps: list[Step] = []
    for fragment in [f.strip() for f in cleaned.split(",") if f.strip()]:
        step = _parse_fragment(fragment)
        if step is None:
            return []  # an unparseable fragment invalidates the whole phrase
        steps.append(step)
    return steps


def _parse_fragment(fragment: str) -> Step | None:
    match = ORDINAL.match(fragment)
    if match:
        kind = KIND_NOUNS.get(match.group("noun").lower())
        index = parse_ordinal(match.group("ordinal"))
        if kind is not None and index is not None:
            return Step(kind, index=index)

    match = KEYED.match(fragment)
    if match:
        kind = KIND_NOUNS.get(match.group("noun").lower())
        if kind is not None:
            return Step(kind, key=match.group("key").lower())
    return None


def _is_redundant(context: Path, steps: list[Step]) -> bool:
    """True when a scope restates the context it already sits in.

    Art. 1(12)(b) reads "in Article 9, the following paragraphs are added" while
    already inside `Article 9 is amended as follows`. Applying the scope again would
    look for Article 9 *within* Article 9.
    """
    return bool(steps) and any(step in context.steps for step in steps)


class Resolver:
    def __init__(self, act: Act, root_scope: str | None = None):
        self.act = act
        self.root = Path()
        if root_scope:
            self.root = Path((Step(Kind.ARTICLE, key=root_scope),))

    def run(self, block: TargetBlock) -> ResolutionReport:
        report = ResolutionReport()
        for instruction in block.instructions:
            self._visit(instruction, self.root, report)
        return report

    # ------------------------------------------------------------------ internals

    def _context_for(self, instruction: Instruction, context: Path) -> Path:
        """Apply the instruction's own `in X,` prefix to the inherited context."""
        steps = parse_phrase(instruction.scope_text)
        if not steps:
            if instruction.scope_text:
                instruction.note(
                    f"scope {instruction.scope_text!r} could not be parsed"
                )
            return context
        if _is_redundant(context, steps):
            instruction.note(
                f"redundant scope {instruction.scope_text!r} — already in context"
            )
            return context
        # An Article step is absolute: it restarts from the act root.
        if steps[0].kind is Kind.ARTICLE:
            return Path(tuple(steps))
        return Path(context.steps + tuple(steps))

    def _visit(self, instruction: Instruction, context: Path,
               report: ResolutionReport) -> None:
        # "in paragraphs 3-e, 3-d, 3-c, and 3-b, ..." — several scopes.
        enumeration = parse_enumeration(instruction.scope_text)
        if enumeration is not None:
            report.results.append(self._resolve_enumeration(instruction, context,
                                                            *enumeration))
            return

        scoped = self._context_for(instruction, context)

        # "paragraphs 8 and 8a are deleted" — several targets. Without this the
        # phrase fails to parse and the instruction collapses onto its container,
        # which would delete the whole article.
        # An insertion names the provisions it creates, which cannot be resolved
        # against the existing act; its container was resolved above.
        enumeration = (
            None if instruction.op in (Op.INSERT_NODE, Op.APPEND_NODE)
            else parse_enumeration(instruction.target_text)
        )
        if enumeration is not None:
            report.results.append(self._resolve_enumeration(instruction, scoped,
                                                            *enumeration))
            return

        if instruction.op is Op.EXTERNAL:
            report.results.append(
                Resolution(instruction, status="not-applicable",
                           reason="handled by a separate annex document")
            )
            return

        target_steps = parse_phrase(instruction.target_text)

        if instruction.op is Op.CONTAINER:
            child_context = self._extend(scoped, target_steps)
            result = self._check(instruction, child_context)
            report.results.append(result)
            for child in instruction.children:
                self._visit(child, child_context, report)
            return

        if instruction.op in (Op.REPLACE_TEXT, Op.DELETE_TEXT):
            # The target is a phrase inside the scoped node, not a node itself.
            result = self._check(instruction, scoped)
            if result.ok and instruction.find_text:
                if not _contains(result.node, instruction.find_text,
                                 title_only=targets_title(instruction.scope_text)):
                    result.status = "unresolved"
                    result.reason = (
                        f"text {instruction.find_text!r} not found in "
                        f"{result.node.citation}"
                    )
            report.results.append(result)
            return

        if instruction.op in (Op.INSERT_NODE, Op.APPEND_NODE):
            # The new node does not exist yet; resolve the container receiving it.
            # An empty path means the act itself — a new article at top level.
            if not scoped.steps:
                report.results.append(
                    Resolution(instruction, path=scoped, node=self.act.root,
                               status="resolved")
                )
                return
            report.results.append(self._check(instruction, scoped))
            return

        if instruction.target_text and OWN_TITLE.match(instruction.target_text.strip()):
            node = resolve(scoped, self.act) if scoped.steps else self.act.root
            report.results.append(
                Resolution(instruction, path=scoped, node=node,
                           status="resolved" if node is not None else "unresolved",
                           reason=None if node else "title target not found",
                           targets_title=True)
            )
            return

        # A target phrase we cannot read must not fall through to the container.
        # "points (b) and (c) of point (10) are replaced" would otherwise resolve to
        # the whole of Article 3 — a plausible-looking replacement that silently
        # shadows every sibling edit inside it.
        if instruction.target_text and not target_steps:
            report.results.append(Resolution(
                instruction, path=scoped, status="unresolved",
                reason=f"target {instruction.target_text!r} could not be parsed; "
                       f"refusing to fall back to {scoped or 'the act'}",
            ))
            return

        result = self._check(instruction, self._extend(scoped, target_steps))
        if result.ok and instruction.op is Op.REPLACE_NODE and instruction.payload:
            self._assess_plausibility(result)
        report.results.append(result)

    def _assess_plausibility(self, result: Resolution) -> None:
        """Compare the replacement text against the node the instruction named.

        A replacement normally restates the provision it replaces, so very low overlap
        is evidence the named node is not the intended one. Where a sibling matches far
        better, the proposal itself is probably misnumbered.

        The instruction is still applied exactly as written — silently retargeting it
        would mean rewriting legislation on the drafter's behalf. The doubt is recorded
        so a human can adjudicate.
        """
        node, payload = result.node, result.instruction.payload
        result.plausibility = similarity(node.full_text, payload)
        if result.plausibility >= PLAUSIBLE:
            return

        siblings = (
            [n for n in node.parent.of_kind(node.kind) if n is not node]
            if node.parent else []
        )
        best, score = None, result.plausibility
        for sibling in siblings:
            candidate = similarity(sibling.full_text, payload)
            if candidate > score:
                best, score = sibling, candidate

        if best is not None and score >= PLAUSIBLE:
            result.doubt = (
                f"replacement text matches {best.citation} ({score:.0%}) far better "
                f"than the addressed {node.citation} ({result.plausibility:.0%}) — "
                f"likely a numbering error in the proposal; applied as written"
            )
        else:
            result.doubt = (
                f"replacement text overlaps the addressed {node.citation} by only "
                f"{result.plausibility:.0%}"
            )
        result.instruction.confidence = min(result.instruction.confidence, 0.5)
        result.instruction.note(result.doubt)

    def _resolve_enumeration(self, instruction: Instruction, context: Path,
                             kind: Kind, keys: list[str]) -> Resolution:
        """Apply one instruction to each of the provisions it names."""
        found, missing = [], []
        for key in keys:
            path = Path(context.steps + (Step(kind, key=key),))
            node = resolve(path, self.act)
            (found if node is not None else missing).append(node if node else key)

        instruction.note(
            f"addresses {len(keys)} {kind.value}s at once: {', '.join(keys)}"
        )
        if missing:
            return Resolution(
                instruction, status="unresolved", enumerated=True,
                reason=f"{kind.value}s {', '.join(map(str, missing))} not found",
            )
        result = Resolution(instruction, nodes=found, status="resolved",
                            enumerated=True)
        if instruction.find_text:
            absent = [n.citation for n in found
                      if not _contains(n, instruction.find_text)]
            if absent:
                result.status = "unresolved"
                result.reason = (
                    f"text {instruction.find_text!r} not found in "
                    f"{len(absent)} of {len(found)} targets"
                )
        return result

    def _extend(self, context: Path, steps: list[Step]) -> Path:
        if not steps:
            return context
        if steps[0].kind is Kind.ARTICLE:
            return Path(tuple(steps))
        return Path(context.steps + tuple(steps))

    def _check(self, instruction: Instruction, path: Path) -> Resolution:
        if not path.steps:
            return Resolution(
                instruction, path=path, status="unresolved",
                reason="no addressable target could be derived",
            )
        node = resolve(path, self.act)
        if node is None:
            node = _ordinal_within_subtree(path, self.act)
            if node is not None:
                instruction.note(
                    f"{path.steps[-1]} counted across the whole of "
                    f"{node.parent.citation if node.parent else 'the article'}"
                )
        if node is None:
            return Resolution(
                instruction, path=path, status="unresolved",
                reason=f"{path} does not exist in {self.act.celex}",
            )
        return Resolution(instruction, path=path, node=node, status="resolved")


def _contains(node: Node, needle: str, title_only: bool = False) -> bool:
    """Case-insensitive text search; chapter titles are rendered in capitals."""
    haystack = node.title or "" if title_only else " ".join(
        part for part in (node.title, node.full_text) if part
    )
    return needle.casefold() in haystack.casefold()


def _ordinal_within_subtree(path: Path, act: Act) -> Node | None:
    """Count an ordinal across a whole subtree when it has no direct sibling match.

    "in Article 2, the first subparagraph" addresses the first subparagraph *anywhere*
    in Article 2 — which lives under paragraph 1, not directly under the article.
    """
    last = path.steps[-1]
    if last.index is None or last.kind not in (Kind.SUBPARAGRAPH, Kind.SENTENCE):
        return None
    parent = resolve(Path(path.steps[:-1]), act)
    if parent is None:
        return None
    found = [n for n in parent.walk() if n is not parent and n.kind is last.kind]
    if not found:
        return None
    position = len(found) + last.index if last.index < 0 else last.index - 1
    return found[position] if 0 <= position < len(found) else None


def resolve_block(block: TargetBlock, act: Act) -> ResolutionReport:
    return Resolver(act, block.root_scope).run(block)
