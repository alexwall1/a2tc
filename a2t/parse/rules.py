"""Classify an instruction sentence into an operation, a scope and a target.

This is the rule engine. It resolves nothing against the base act — it only reads what
the instruction *says*. Turning `target_text` into a node is Phase 5's job, and
whatever this engine cannot classify is handed to the LLM tier as a closed-set choice.

Order matters: text-level operations are checked before node-level ones, because
"the words '...' are deleted" also matches "... is deleted".
"""

from __future__ import annotations

import re

from a2t.model.instruction import Instruction, Op
from a2t.parse.normalise import normalise, unify_quotes

Q = "[‘’]"  # after unify_quotes every quote family is normalised to these

# What can follow a scope: the operative clause, a target noun, or a quoted string.
_AFTER_SCOPE = (
    r"(?:the\s+|points?\b|paragraphs?\b|subparagraphs?\b|sentences?\b"
    r"|Articles?\b|Annexe?s?\b|[‘’])"
)
# Greedy up to the *last* comma before the operative clause, so list-valued scopes
# survive: "in paragraphs 3-e, 3-d, 3-c, and 3-b, the date ..." is one scope.
SCOPE = re.compile(rf"^in\s+(?P<scope>.+),\s*(?={_AFTER_SCOPE})", re.I)
# "in the title of Chapter III the word ..." — same idea without the comma
SCOPE_NO_COMMA = re.compile(r"^in\s+(?P<scope>the\s+title\s+of\s+[^,]+?)\s+the\s+", re.I)

# "Annexes I to V are amended as set out in the Annexes to this Directive" and
# "the text set out in the Annex to this Directive is added as the Annex" both
# defer the actual text to a separate annex document.
EXTERNAL = re.compile(r"set\s+out\s+in\s+the\s+Annexe?s?\b", re.I)
# The terminator is sometimes ";" rather than ":" — see Art. 1(33).
CONTAINER = re.compile(
    r"^(?P<target>.+?)\s+(?:is|shall\s+be)\s+(?:hereby\s+)?amended\s+as\s+follows\s*[:;]?\s*$", re.I
)

DELETE_TEXT = re.compile(
    rf"the\s+(?:words?|date|figures?|numbers?|reference)\s+{Q}(?P<find>.+?){Q}\s+"
    rf"(?:is|are|shall\s+be)\s+deleted",
    re.I,
)
REPLACE_TEXT = re.compile(
    rf"(?:the\s+(?:words?|date|figures?|numbers?|reference)\s+)?"
    rf"{Q}(?P<find>.+?){Q}\s+(?:is|are|shall\s+be)\s+replaced\s+by\s+{Q}(?P<repl>.+?){Q}",
    re.I,
)

INSERT_NODE = re.compile(
    # `what` is free-form: it may name several provisions at once,
    # "the following Articles 1a, 1b and 1c are inserted".
    r"(?:after\s+(?P<anchor>[^,]+?),\s*)?the\s+following\s+"
    r"(?P<what>.+?)\s+(?:is|are|shall\s+be)\s+inserted",
    re.I,
)
APPEND_NODE = re.compile(
    r"the\s+following\s+(?P<what>.+?)?\s*(?:is|are|shall\s+be)\s+added"
    # "... added after the second subparagraph", "... added to point (b)",
    # "... added in the beginning of paragraph 10"
    r"(?:\s+(?P<where>after|to|in\s+the\s+beginning\s+of|at\s+the\s+end\s+of)"
    r"\s+(?P<anchor>.+?))?\s*:?\s*$",
    re.I,
)

REPLACE_NODE = re.compile(
    # The auxiliary verb is occasionally omitted in the source; "by the following"
    # keeps the reading unambiguous. See Directive (EU) 2023/959, Art. 2(1)(d).
    r"^(?P<target>.+?)\s+(?:(?:is|are|shall\s+be)\s+)?replaced\s+by\s+the\s+following",
    re.I,
)
DELETE_NODE = re.compile(r"^(?P<target>.+?)\s+(?:is|are|shall\s+be)\s+deleted", re.I)


def _strip_scope(text: str) -> tuple[str | None, str]:
    match = SCOPE_NO_COMMA.match(text)
    if match:
        return match.group("scope").strip(), text[: match.start()] + text[match.end("scope") + 1:].strip()
    match = SCOPE.match(text)
    if match:
        return match.group("scope").strip(), text[match.end():].strip()
    return None, text


def classify(instruction: Instruction) -> Instruction:
    """Determine op, scope and target for one instruction, in place."""
    normalise(instruction)
    text = unify_quotes(instruction.raw)

    if EXTERNAL.search(text):
        instruction.op = Op.EXTERNAL
        instruction.target_text = text.rstrip(";. ")
        instruction.note("amends annexes via a separate document — out of scope")
        return instruction

    scope, body = _strip_scope(text)
    instruction.scope_text = scope

    container = CONTAINER.match(body)
    if container:
        instruction.op = Op.CONTAINER
        instruction.target_text = container.group("target").strip()
        return instruction

    match = DELETE_TEXT.search(body)
    if match:
        instruction.op = Op.DELETE_TEXT
        instruction.find_text = match.group("find")
        return instruction

    match = REPLACE_TEXT.search(body)
    if match:
        instruction.op = Op.REPLACE_TEXT
        instruction.find_text = match.group("find")
        instruction.replace_text = match.group("repl")
        return instruction

    if re.search(r"the\s+following\b", body, re.I):
        match = INSERT_NODE.search(body)
        if match:
            instruction.op = Op.INSERT_NODE
            instruction.target_text = (match.group("what") or "").strip() or None
            if match.group("anchor"):
                instruction.scope_text = scope or None
                instruction.notes.append(f"insert after {match.group('anchor').strip()}")
            return instruction

        match = APPEND_NODE.search(body)
        if match:
            instruction.op = Op.APPEND_NODE
            instruction.target_text = (match.group("what") or "").strip() or None
            if match.group("anchor"):
                where = re.sub(r"\s+", " ", (match.group("where") or "after")).lower()
                instruction.note(f"append {where} {match.group('anchor').strip()}")
            return instruction

    match = REPLACE_NODE.match(body)
    if match:
        instruction.op = Op.REPLACE_NODE
        instruction.target_text = match.group("target").strip()
        return instruction

    match = DELETE_NODE.match(body)
    if match:
        instruction.op = Op.DELETE_NODE
        instruction.target_text = match.group("target").strip()
        return instruction

    instruction.op = Op.UNKNOWN
    instruction.confidence = 0.0
    instruction.note("no rule matched this instruction")
    return instruction


def classify_all(blocks) -> None:
    for block in blocks:
        for instruction in block.walk():
            classify(instruction)
