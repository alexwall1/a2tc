"""Turn resolved instructions into a set of concrete edits.

The tree is not mutated. Each change records the node it affects, the text before and
the text after, so the emitter can render the original act with revisions laid over
it — which is exactly what a tracked-changes document is.

Replacement text is always the payload as parsed from the PDF. Nothing here composes
legal language; text edits only substitute a phrase the instruction quoted for another
phrase the instruction quoted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from a2t.apply.diff import Edit, Pair, align, diff
from a2t.model.instruction import Instruction, Op
from a2t.model.payload import (
    Provision,
    carry_number,
    from_node,
    opening_number,
    structure,
    with_number,
)
from a2t.model.tree import Act, Kind, Node
from a2t.resolve.resolver import Resolution, ResolutionReport


class ChangeKind(str, Enum):
    REPLACE = "replace"      # node's text is replaced wholesale
    DELETE = "delete"        # node is removed
    INSERT = "insert"        # new provision added under/after an anchor
    TEXT_EDIT = "text-edit"  # a phrase inside the node changes
    TITLE_EDIT = "title-edit"  # a heading changes
    SKIPPED = "skipped"      # nothing renderable (reported, never silent)


@dataclass
class Change:
    kind: ChangeKind
    instruction: Instruction
    node: Node | None = None      # the provision affected
    anchor: Node | None = None    # container receiving an insertion
    old_text: str = ""
    new_text: str = ""
    edit: Edit | None = None
    note: str | None = None
    # A whole-provision change resolved block by block. `edit` still covers the whole
    # provision — the report speaks of the change as one thing — but the document is
    # written from these, so a replacement reads paragraph against paragraph.
    blocks: list[Pair] = field(default_factory=list)

    @property
    def citation(self) -> str:
        target = self.node or self.anchor
        return target.citation if target is not None else "(act)"

    @property
    def needs_review(self) -> bool:
        return self.instruction.confidence < 1.0 or bool(self.note)


@dataclass
class ChangeSet:
    act: Act
    changes: list[Change] = field(default_factory=list)

    def by_node(self) -> dict[int, list[Change]]:
        """Changes indexed by the id of the node they affect."""
        index: dict[int, list[Change]] = {}
        for change in self.changes:
            target = change.node or change.anchor
            if target is not None:
                index.setdefault(id(target), []).append(change)
        return index

    @property
    def renderable(self) -> list[Change]:
        return [c for c in self.changes if c.kind is not ChangeKind.SKIPPED]

    @property
    def skipped(self) -> list[Change]:
        return [c for c in self.changes if c.kind is ChangeKind.SKIPPED]

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for change in self.renderable:
            counts[change.kind.value] = counts.get(change.kind.value, 0) + 1
        parts = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        return f"{len(self.renderable)} change(s): {parts or 'none'}"


def _leaf_holding(node: Node, phrase: str) -> Node:
    """The smallest node under `node` whose own text carries `phrase`."""
    for candidate in node.walk():
        if candidate.text and phrase.casefold() in candidate.text.casefold():
            return candidate
    return node


def _substitute(text: str, find: str, replace: str) -> tuple[str, int]:
    """Case-insensitive substitution, preserving everything else exactly.

    Deleting a phrase leaves the spaces that surrounded it, so "X <phrase> means"
    would become "X  means". Since node text is already whitespace-normalised, tidying
    the seam cannot disturb anything the source intended.
    """
    pattern = re.compile(re.escape(find), re.IGNORECASE)
    new_text, count = pattern.subn(replace.replace("\\", "\\\\"), text)
    if count:
        new_text = re.sub(r" {2,}", " ", new_text)
        new_text = re.sub(r"\s+([,;.)])", r"\1", new_text)
        new_text = new_text.strip()
    return new_text, count


def _current(texts: dict[int, str] | None, node: Node) -> str:
    """The provision's text as amended so far in this run."""
    if texts is None:
        return node.text
    return texts.setdefault(id(node), node.text)


def _remember(texts: dict[int, str] | None, node: Node, text: str) -> None:
    if texts is not None:
        texts[id(node)] = text


def _cut(payload: str, patterns: list[re.Pattern]) -> list[str] | None:
    """Cut `payload` where each pattern in turn opens the next provision.

    Returns None unless the openings are all present, in order, and the first of them
    is the start of the payload. Text above the first opening belongs to no segment,
    so cutting from there would drop it — and in this project a payload that cannot be
    divided is reported, never guessed at.
    """
    positions: list[int] = []
    cursor = 0
    for pattern in patterns:
        match = pattern.search(payload, cursor)
        if match is None:
            return None
        # The patterns allow a leading space so a key cannot match mid-word; the
        # segment starts at the key itself.
        positions.append(match.start() if match.start() == 0 else match.start() + 1)
        cursor = match.end()
    if positions != sorted(positions) or payload[:positions[0]].strip():
        return None

    segments = []
    for index, start in enumerate(positions):
        end = positions[index + 1] if index + 1 < len(positions) else len(payload)
        segments.append(payload[start:end].strip())
    return segments if all(segments) else None


def split_enumerated_payload(payload: str, keys: list[str]) -> list[str] | None:
    """Split one payload that carries replacement text for several provisions.

    "paragraphs 5 and 5a are replaced by the following:" is followed by a single
    quoted block holding *both* new paragraphs, each opening with its own number.
    Applying the whole block to each target would duplicate one provision into the
    other — so the block is cut at those markers and each target gets its own part.

    Returns None if the markers are not all present in order, in which case the
    caller must report rather than guess.
    """
    return _cut(
        payload, [re.compile(rf"(?:^|\s){re.escape(key)}\.\s+") for key in keys]
    )


# A heading, as against the many cross-references to other articles that legislation
# is made of. Two things separate them, and both are needed: a heading opens the
# payload or follows the end of a sentence — the test `a2t.model.payload` applies to
# markers, for the same reason — and what comes after it opens a provision rather than
# continuing a sentence, so never a lowercase word. "as specified in Article 3 shall
# apply" fails the first; "in accordance with Article 3, Member States" the second.
def _heading(key: str) -> re.Pattern:
    return re.compile(
        rf"(?:^|(?<=[.;:])\s)[Aa]rticle\s+{re.escape(key)}\b(?!\s+[a-z])"
    )


def split_articles_payload(payload: str, keys: list[str]) -> list[str] | None:
    """Split a payload that carries several whole new articles, at their headings.

    An article is divided from the next by its heading, not by a number in the running
    text. Cutting "Articles 2 and 3 are replaced by the following:" on the numbers
    finds the new articles' *paragraphs* 2. and 3. instead — which sit below the first
    article's paragraph 1, so everything above them, heading included, is lost.
    """
    return _cut(payload, [_heading(key) for key in keys])


def _blocks(instruction: Instruction, node: Node, payload: str, whole: Edit,
            layout: list[str] | None = None,
            breaks: list[bool] | None = None, number: str = "",
            named: str | None = None) -> list[Pair]:
    """Resolve a whole-provision change block by block.

    Only where doing so tells the reader something. A provision that is a single
    paragraph on both sides has nothing to align. And an alignment in which no block
    matched any other says only that the two sides could not be put side by side —
    if the provision as a whole still diffs word by word, that reading is the better
    one, and it is what the change set already holds.
    """
    old = from_node(node)
    # The payload is read from its own source lines, which carry no number, so the
    # number goes on afterwards — as it does on the old side, and for the same reason.
    new = (carry_number(structure(payload, layout, breaks, named), number)
           if payload else [])
    # A payload that restates the article's heading is compared against the heading
    # the act already carries, so the two do not both get printed. One that does not
    # restate it leaves the act's heading standing, untouched and outside the change.
    if node.kind is Kind.ARTICLE and any(p.is_article for p in new):
        old.insert(0, Provision(Kind.ARTICLE, node.key, title=node.title))
    if len(old) < 2 and len(new) < 2:
        return []
    pairs = align(old, new)
    if whole.mode == "word" and not any(p.old and p.new for p in pairs):
        return []
    return pairs


def _rebase_sentence(sentence: Node, replacement: str) -> tuple[Node, str, str] | None:
    """Express a sentence-level edit against the provision that contains it.

    Sentences are derived on demand and never enter the tree, so a change targeting
    one would have nothing to attach to. Rewriting it as an edit *within* the holding
    provision is also what a reader wants: the paragraph, with one sentence changed.
    """
    parent = sentence.parent
    if parent is None or not sentence.text:
        return None
    holder = _leaf_holding(parent, sentence.text)
    if not holder.text or sentence.text not in holder.text:
        return None
    return holder, holder.text, holder.text.replace(sentence.text, replacement, 1)


def _text_edit(instruction: Instruction, node: Node, replacement: str,
               texts: dict[int, str] | None = None) -> Change:
    """A phrase inside a provision changes; everything around it is untouched.

    `texts` threads the running text of each provision, so several instructions
    amending the same provision compose instead of overwriting one another.
    """
    find = instruction.find_text or ""
    if node.kind is Kind.SENTENCE:
        edited, count = _substitute(node.text, find, replacement)
        rebased = _rebase_sentence(node, edited) if count else None
        if rebased is None:
            return Change(ChangeKind.SKIPPED, instruction, node=node,
                          note=f"phrase {find!r} not present in {node.citation}")
        holder, _, _ = rebased
        old = _current(texts, holder)
        new = old.replace(node.text, edited, 1)
        _remember(texts, holder, new)
        return Change(ChangeKind.TEXT_EDIT, instruction, node=holder,
                      old_text=old, new_text=new, edit=diff(old, new),
                      note="edit applied within one sentence of the provision")
    target = _leaf_holding(node, find)
    is_title = (
        target.title is not None
        and find.casefold() in (target.title or "").casefold()
        and find.casefold() not in (target.text or "").casefold()
    )
    source = target.title if is_title else _current(texts, target)
    new_text, count = _substitute(source or "", find, replacement)
    if count and not is_title:
        _remember(texts, target, new_text)

    if count == 0:
        return Change(
            ChangeKind.SKIPPED, instruction, node=target,
            note=f"phrase {find!r} not present in {target.citation}",
        )
    return Change(
        ChangeKind.TITLE_EDIT if is_title else ChangeKind.TEXT_EDIT,
        instruction, node=target,
        old_text=source or "", new_text=new_text,
        edit=diff(source or "", new_text),
        note=(f"{count} occurrences replaced" if count > 1 else None),
    )


def build_changes(resolution: Resolution,
                  texts: dict[int, str] | None = None) -> list[Change]:
    """Every edit implied by one resolved instruction."""
    instruction = resolution.instruction

    if resolution.status == "not-applicable":
        return [Change(ChangeKind.SKIPPED, instruction,
                       note=resolution.reason or "not applicable")]
    if not resolution.ok:
        return [Change(ChangeKind.SKIPPED, instruction,
                       note=resolution.reason or "unresolved")]

    targets = resolution.targets
    if not targets:
        return [Change(ChangeKind.SKIPPED, instruction, note="no target node")]

    changes: list[Change] = []

    if resolution.targets_title:
        node = targets[0]
        old = node.title or ""
        new = instruction.payload or ""
        return [Change(ChangeKind.TITLE_EDIT, instruction, node=node,
                       old_text=old, new_text=new, edit=diff(old, new))]

    # One instruction, several provisions, one payload carrying all their new text.
    if (resolution.enumerated and instruction.op is Op.REPLACE_NODE
            and instruction.payload and len(targets) > 1):
        keys = [node.key or "" for node in targets]
        # Whole articles are divided by their headings; anything smaller by the number
        # it opens with.
        segments = (
            split_articles_payload(instruction.payload, keys)
            if all(node.kind is Kind.ARTICLE for node in targets)
            else split_enumerated_payload(instruction.payload, keys)
        )
        if segments is None:
            instruction.note(
                f"replaces {len(targets)} provisions but the payload could not be "
                f"split by their numbers ({', '.join(keys)}) — needs review"
            )
            instruction.confidence = min(instruction.confidence, 0.4)
        else:
            return [
                Change(
                    ChangeKind.REPLACE, instruction, node=node,
                    old_text=opening_number(node) + node.full_text,
                    new_text=segment,
                    edit=diff(opening_number(node) + node.full_text, segment),
                    # The layout covers the whole payload, not this one segment of
                    # it, so the segment's own markers have to do the work — and,
                    # for a whole article, the heading it opens with, which only
                    # counts as one when the article it names is the one being
                    # replaced.
                    blocks=_blocks(instruction, node, segment,
                                   diff(opening_number(node) + node.full_text,
                                        segment),
                                   named=node.key
                                   if node.kind is Kind.ARTICLE else None),
                    note=f"payload split across {len(targets)} provisions",
                )
                for node, segment in zip(targets, segments)
            ]

    for node in targets:
        if instruction.op is Op.REPLACE_NODE:
            if node.kind is Kind.SENTENCE:
                rebased = _rebase_sentence(node, instruction.payload or "")
                if rebased is None:
                    changes.append(Change(
                        ChangeKind.SKIPPED, instruction, node=node,
                        note="sentence could not be located in its provision"))
                    continue
                holder, _, _ = rebased
                old = _current(texts, holder)
                new = old.replace(node.text, instruction.payload or "", 1)
                _remember(texts, holder, new)
                changes.append(Change(
                    ChangeKind.TEXT_EDIT, instruction, node=holder,
                    old_text=old, new_text=new, edit=diff(old, new),
                    note="one sentence replaced within the provision",
                ))
                continue
            number = opening_number(node)
            old = number + node.full_text
            new = with_number(instruction.payload or "", number)
            edit = diff(old, new)
            changes.append(Change(
                ChangeKind.REPLACE, instruction, node=node,
                old_text=old, new_text=new, edit=edit,
                blocks=_blocks(instruction, node, new, edit,
                               instruction.payload_layout,
                               instruction.payload_breaks, number),
            ))

        elif instruction.op is Op.DELETE_NODE:
            old = opening_number(node) + node.full_text
            edit = diff(old, "")
            changes.append(Change(
                ChangeKind.DELETE, instruction, node=node,
                old_text=old, new_text="", edit=edit,
                blocks=_blocks(instruction, node, "", edit),
            ))

        elif instruction.op in (Op.INSERT_NODE, Op.APPEND_NODE):
            new = instruction.payload or ""
            if not new:
                changes.append(Change(ChangeKind.SKIPPED, instruction, anchor=node,
                                      note="insertion carries no text"))
            else:
                changes.append(Change(
                    ChangeKind.INSERT, instruction, anchor=node,
                    old_text="", new_text=new, edit=diff("", new),
                ))

        elif instruction.op is Op.REPLACE_TEXT:
            changes.append(_text_edit(instruction, node,
                                      instruction.replace_text or "", texts))

        elif instruction.op is Op.DELETE_TEXT:
            changes.append(_text_edit(instruction, node, "", texts))

        elif instruction.op is Op.CONTAINER:
            # A container carries no edit of its own; its children hold them.
            changes.append(Change(ChangeKind.SKIPPED, instruction, node=node,
                                  note="container — edits are on its children"))

        else:
            changes.append(Change(ChangeKind.SKIPPED, instruction, node=node,
                                  note=f"operation {instruction.op.value} "
                                       f"cannot be rendered"))
    return changes


def _flag_shadowed(changeset: ChangeSet) -> None:
    """Report changes that a wholesale replacement would swallow.

    When a provision is replaced or repealed in full, the emitter renders the
    replacement and does not descend — its children are part of what goes. A separate
    change inside that subtree therefore cannot be rendered. That is correct for the
    document and wrong for the change set, so both sides are flagged rather than one
    silently disappearing.
    """
    whole = [
        c for c in changeset.renderable
        if c.kind in (ChangeKind.REPLACE, ChangeKind.DELETE) and c.node is not None
    ]
    if not whole:
        return
    covering = {id(c.node): c for c in whole}

    for change in changeset.renderable:
        node = change.node or change.anchor
        if node is None:
            continue
        ancestor = node.parent
        while ancestor is not None:
            outer = covering.get(id(ancestor))
            if outer is not None and outer is not change:
                change.note = (
                    f"inside {ancestor.citation}, which {outer.instruction.id} "
                    f"replaces in full — this edit cannot be shown separately"
                )
                change.instruction.confidence = min(
                    change.instruction.confidence, 0.4
                )
                outer.instruction.note(
                    f"replaces a provision that {change.instruction.id} also amends"
                )
                break
            ancestor = ancestor.parent


def build_changeset(report: ResolutionReport, act: Act) -> ChangeSet:
    changeset = ChangeSet(act=act)
    # Several instructions may amend the same provision; threading the running text
    # lets them compose rather than the last one silently winning.
    texts: dict[int, str] = {}
    for resolution in report.results:
        changeset.changes.extend(build_changes(resolution, texts))
    _flag_shadowed(changeset)
    return changeset
