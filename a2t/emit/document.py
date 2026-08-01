"""Render a base act with the proposal's amendments as tracked changes.

The document is the original legislation in reading order — chapters, articles,
paragraphs, points — with revisions laid over the provisions the proposal touches.
Accepting every change yields the act as the proposal would leave it; rejecting every
change yields the act as it stands today.

Anything the pipeline could not resolve, or resolved with doubt, becomes a Word comment
anchored at the provision concerned. Nothing is dropped silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from a2t.apply.changeset import Change, ChangeKind, ChangeSet
from a2t.apply.diff import Pair, Span, diff
from a2t.emit.tracked_docx import TrackedDocx
from a2t.model.payload import Provision, printed, structure
from a2t.model.tree import Act, Kind, Node

def _restates_heading(text: str, key: str | None) -> bool:
    """True when replacement text opens with the article's own heading."""
    if not key or not text:
        return False
    return bool(re.match(rf"^\s*Article\s+{re.escape(key)}\b", text, re.I))


# "the following Article 3gaa is inserted" — the instruction sometimes names what it
# inserts. Only a fallback: most do not ("the following Articles are inserted"), so
# the article's own number is read out of the payload's layout instead.
INSERTS_ARTICLE = re.compile(r"\bfollowing\s+Article\s+(?P<key>\d+[a-z]*)\b", re.I)


def _provisions(change: Change) -> list[Provision]:
    """The payload of `change`, split into the blocks it should be printed as."""
    named = INSERTS_ARTICLE.search(change.instruction.raw or "")
    return structure(
        change.new_text,
        layout=change.instruction.payload_layout,
        named=named.group("key") if named else None,
    )


# Changes that consume the whole provision, so its children must not be rendered too.
WHOLE_NODE = {ChangeKind.REPLACE, ChangeKind.DELETE}


@dataclass
class EmitResult:
    path: Path
    paragraphs: int = 0
    revisions: int = 0
    comments: int = 0
    rendered: list[Change] = field(default_factory=list)
    unrendered: list[Change] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.paragraphs} paragraphs, {self.revisions} revised, "
            f"{self.comments} comment(s)"
        )


class DocumentEmitter:
    def __init__(self, act: Act, changeset: ChangeSet, author: str = "A2T",
                 version_warning: str | None = None):
        self.act = act
        self.changeset = changeset
        self.version_warning = version_warning
        self.doc = TrackedDocx(author=author)
        self.by_node = changeset.by_node()
        self.result = EmitResult(path=Path())
        self._seen: set[int] = set()
        self._number = ""  # a paragraph number waiting for the block that carries it

    # ---------------------------------------------------------------------- api

    def emit(self, path: str | Path) -> EmitResult:
        path = Path(path)
        self._front_matter()
        for child in self.act.root.children:
            self._render(child)
        self._insertions_for(self.act.root)
        self._unplaced()

        path.parent.mkdir(parents=True, exist_ok=True)
        self.doc.save(str(path))
        self.result.path = path
        self.result.unrendered = [
            c for c in self.changeset.renderable if id(c) not in self._seen
        ]
        return self.result

    # ------------------------------------------------------------------ chrome

    def _front_matter(self) -> None:
        # An amending act may replace the base act's own title.
        title_change = next(
            (c for c in self.by_node.get(id(self.act.root), [])
             if c.kind is ChangeKind.TITLE_EDIT), None
        )
        if title_change is not None:
            block = self.doc.from_spans(title_change.edit.spans, style="OJ Title")
            self._account(title_change, block)
        else:
            self.doc.unchanged(self.act.title or self.act.celex, style="OJ Title")
        version = self.act.version_date or "—"
        self.doc.unchanged(
            f"Consolidated text {self.act.celex} (applicable from {version}), "
            f"shown with the amendments proposed against it as tracked changes.",
            style="OJ Note",
        )
        self.result.paragraphs += 1

        # Doubt about *which* text this is belongs next to the text, not only in the
        # report — the document is what gets read, forwarded and worked from.
        for line in (self.version_warning or "").splitlines():
            self.doc.unchanged(f"⚠ Version warning: {line}", style="OJ Warning")
            self.result.paragraphs += 1

    # ----------------------------------------------------------------- walking

    def _render(self, node: Node) -> None:
        changes = self.by_node.get(id(node), [])
        whole = next((c for c in changes if c.kind in WHOLE_NODE), None)

        if node.kind is Kind.PARAGRAPH and node.key and not node.text:
            # EUR-Lex keeps a paragraph's number outside its words: the number is a
            # span of its own and the text lives in child subparagraphs, so a
            # numbered paragraph is a node with a key and nothing else. Printed as
            # it stands, the act loses its paragraph numbering entirely — every one
            # of the 262 in the ETS Directive. The number waits here for the first
            # block of text that can carry it.
            self._number = f"{node.key}. "

        deleted = whole is not None and whole.kind is ChangeKind.DELETE
        if node.kind is Kind.CHAPTER:
            self._chapter(node, changes, deleted)
        elif node.kind is Kind.ARTICLE:
            # A replacement payload usually restates the article heading, so keeping
            # the original one too would print "Article 2" twice. Where the change was
            # resolved block by block the heading is one of those blocks, and is
            # printed — and diffed against the act's own — from there.
            restates = whole is not None and whole.kind is ChangeKind.REPLACE and (
                any(pair.block.is_article for pair in whole.blocks) if whole.blocks
                else _restates_heading(whole.new_text, node.key)
            )
            if not restates:
                self._article(node, changes, deleted)

        if whole is not None:
            # The provision is replaced or repealed in full; its children are part of
            # what goes, so they must not also be rendered.
            self._whole_node(node, whole)
            self._insertions_for(node)
            return

        self._own_text(node, changes)
        for child in node.children:
            self._render(child)
        self._insertions_for(node)

    def _chapter(self, node: Node, changes: list[Change],
                 deleted: bool = False) -> None:
        label = f"CHAPTER {node.key}" if node.key else "CHAPTER"
        if deleted:
            self.doc.deleted(label, style="OJ Chapter")
        else:
            self.doc.unchanged(label, style="OJ Chapter")
        title_change = next(
            (c for c in changes if c.kind is ChangeKind.TITLE_EDIT), None
        )
        if title_change is not None:
            block = self.doc.from_spans(title_change.edit.spans,
                                        style="OJ Chapter Title")
            self._account(title_change, block)
        elif node.title:
            self.doc.unchanged(node.title, style="OJ Chapter Title")
        self.result.paragraphs += 1

    def _article(self, node: Node, changes: list[Change],
                 deleted: bool = False) -> None:
        # A repealed provision takes its heading with it, or accepting the change
        # leaves a bare "Article 1" standing over nothing.
        if deleted:
            self.doc.deleted(f"Article {node.key}", style="OJ Article")
            if node.title:
                self.doc.deleted(node.title, style="OJ Article Title")
            self.result.paragraphs += 1
            return

        self.doc.unchanged(f"Article {node.key}", style="OJ Article")
        title_change = next(
            (c for c in changes if c.kind is ChangeKind.TITLE_EDIT), None
        )
        if title_change is not None:
            block = self.doc.from_spans(title_change.edit.spans,
                                        style="OJ Article Title")
            self._account(title_change, block)
        elif node.title:
            self.doc.unchanged(node.title, style="OJ Article Title")
        self.result.paragraphs += 1

    def _own_text(self, node: Node, changes: list[Change]) -> None:
        edits = [c for c in changes if c.kind is ChangeKind.TEXT_EDIT]
        if edits:
            # Several instructions may amend the same provision. They were built
            # against a running text, so the first one's "before" and the last one's
            # "after" bracket the whole change; rendering them separately would show
            # the provision twice.
            combined = diff(edits[0].old_text, edits[-1].new_text)
            block = self.doc.from_spans(self._numbered(combined.spans),
                                        style=self._style(node))
            for change in edits:
                self._account(change, block, count_paragraph=change is edits[0])
            return
        if node.text:
            self.doc.unchanged(self._take_number() + self._prefixed(node),
                               style=self._style(node))
            self.result.paragraphs += 1

    def _whole_node(self, node: Node, change: Change) -> None:
        # The change set already put the paragraph number into the text on both
        # sides of this edit, so that a payload restating it diffs against it rather
        # than replacing it with itself. Adding it again here would print it twice.
        self._take_number()

        if not change.blocks:
            mark = "del" if change.kind is ChangeKind.DELETE else None
            block = self.doc.from_spans(change.edit.spans, style=self._style(node),
                                        paragraph_mark=mark)
            self._account(change, block)
            return

        written = [block for pair in change.blocks for block in self._aligned(pair)]
        # `_account` counts the last one.
        self.result.paragraphs += len(written) - 1
        self._account(change, written[0])

    def _aligned(self, pair: Pair) -> list:
        """The Word paragraphs one step of an alignment is written as.

        A block present on only one side is wholly inserted or wholly deleted, and its
        paragraph mark goes with it — otherwise accepting the change would leave the
        paragraph break of a provision that is no longer there.
        """
        mark = "ins" if pair.old is None else "del" if pair.new is None else None
        block = pair.block
        style = "OJ Article" if block.is_article else self._style(block)
        written = [self.doc.from_spans(pair.edit.spans, style=style,
                                       paragraph_mark=mark)]
        if pair.title is not None:
            written.append(self.doc.from_spans(pair.title.spans,
                                               style="OJ Article Title",
                                               paragraph_mark=mark))
        return written

    def _insertions_for(self, node: Node) -> None:
        for change in self.by_node.get(id(node), []):
            if change.kind is not ChangeKind.INSERT:
                continue
            # An insertion arrives as one flat payload. Written whole it would sit in
            # the body text as a single justified block — article headings out of the
            # run of centred headings, numbered paragraphs and points run together.
            blocks = [
                self.doc.inserted(text, style=style)
                for text, style in self._printed(_provisions(change), node)
            ]
            if not blocks:
                blocks = [self.doc.inserted(change.new_text,
                                            style=self._style(node))]
            # `_account` counts the last one.
            self.result.paragraphs += len(blocks) - 1
            self._account(change, blocks[0])

    def _printed(self, provisions: list[Provision],
                 anchor: Node) -> list[tuple[str, str]]:
        """(text, style) per Word paragraph, for a payload already split into blocks.

        An article contributes two paragraphs of its own — its number and its title —
        because the Official Journal centres them separately.
        """
        out: list[tuple[str, str]] = []
        # Text inserted below an article heading is that article's body. Text inserted
        # without one lands inside the provision it is anchored to, and reads at that
        # provision's level — a point added to a list of points is still a point.
        opens_article = any(p.is_article for p in provisions)
        for provision in provisions:
            if provision.is_article:
                out.append((f"Article {provision.key}", "OJ Article"))
                if provision.title:
                    out.append((provision.title, "OJ Article Title"))
                continue
            style = (
                self._style(anchor)
                if provision.kind is Kind.SUBPARAGRAPH and not opens_article
                else self._style(provision)
            )
            out.append((self._prefixed(provision), style))
        return out

    def _unplaced(self) -> None:
        """Report anything that could not be attached to a provision."""
        orphans = [
            c for c in self.changeset.changes
            if c.kind is ChangeKind.SKIPPED and c.note
            and "container" not in c.note
        ]
        if not orphans:
            return
        self.doc.unchanged("Instructions not applied", style="OJ Heading")
        for change in orphans:
            block = self.doc.unchanged(
                f"{change.instruction.id} (source line "
                f"{change.instruction.source_line}): {change.instruction.raw[:160]}",
                style="OJ Text",
            )
            self.doc.flag(block.runs[0], f"Not applied — {change.note}")
            self.result.comments += 1
            self.result.paragraphs += 1

    # ---------------------------------------------------------------- helpers

    def _take_number(self) -> str:
        """The paragraph number waiting to be printed, if any, taken."""
        number, self._number = self._number, ""
        return number

    def _numbered(self, spans) -> list:
        """`spans`, with any waiting paragraph number in front of them.

        Used where the change touches a phrase inside the provision, which never
        touches the number: it stands unrevised in front of whatever the edit does.
        """
        number = self._take_number()
        return [Span("equal", number), *spans] if number else list(spans)

    def _style(self, node: Node | Provision) -> str:
        if node.kind is Kind.SUBPOINT:
            return "OJ Subpoint"
        if node.kind is Kind.POINT:
            return "OJ Point"
        return "OJ Text"

    def _prefixed(self, node: Node | Provision) -> str:
        """Numbered provisions read better with their marker."""
        return printed(node)

    def _account(self, change: Change, block, count_paragraph: bool = True) -> None:
        self._seen.add(id(change))
        self.result.rendered.append(change)
        if count_paragraph:
            self.result.paragraphs += 1
            if change.edit and change.edit.changed_spans:
                self.result.revisions += 1
        message = self._comment_for(change)
        if message and block.runs:
            self.doc.flag(block.runs[0], message)
            self.result.comments += 1

    def _comment_for(self, change: Change) -> str | None:
        instruction = change.instruction
        parts: list[str] = []
        if instruction.confidence < 1.0:
            parts.append(f"confidence {instruction.confidence:.2f}")
        if instruction.resolver != "rule":
            parts.append(f"resolved by the {instruction.resolver} tier")
        for note in instruction.notes:
            parts.append(note)
        if change.note:
            parts.append(change.note)
        if not parts:
            return None
        head = (
            f"{instruction.id} (source line {instruction.source_line}): "
            f"{instruction.raw[:120]}"
        )
        return head + "\n\n" + "\n".join(f"• {p}" for p in parts)


def emit_document(act: Act, changeset: ChangeSet, path: str | Path,
                  author: str = "A2T",
                  version_warning: str | None = None) -> EmitResult:
    return DocumentEmitter(act, changeset, author, version_warning).emit(path)
