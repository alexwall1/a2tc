"""Classify an amendment instruction with an LLM instead of a grammar.

The rule engine in `rules.py` reads intent from surface form, so every paraphrase
needs a new pattern: `is replaced` / `shall be replaced`, `is amended` / `is hereby
amended`, `HAVE ADOPTED` / `HAS ADOPTED`. Paraphrase is unbounded; enumerating it is
not a strategy.

What does *not* change is the safety property. The model states intent only:

* the operation, chosen from a closed enum;
* the scope and target, returned as **verbatim substrings of the instruction
  sentence** — rejected if they are not, so the model cannot invent an address;
* quoted find/replace strings, likewise verbatim.

Payload text is still cut structurally by the segmenter and never comes from a model,
and resolving a phrase to a provision still happens against the real act tree. The
model decides what the drafter meant; it never decides what the legislation says.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from a2t.model.instruction import Instruction, Op
from a2t.parse.normalise import normalise
from a2t.resolve.llm import OpenRouterClient

OPERATIONS = {
    "container": Op.CONTAINER,
    "replace": Op.REPLACE_NODE,
    "delete": Op.DELETE_NODE,
    "insert": Op.INSERT_NODE,
    "append": Op.APPEND_NODE,
    "replace-text": Op.REPLACE_TEXT,
    "delete-text": Op.DELETE_TEXT,
    "external": Op.EXTERNAL,
    "unknown": Op.UNKNOWN,
}

SYSTEM_PROMPT = """\
You read a single instruction from an EU amending act and state its structure.

Return:
- operation, one of:
    container    - introduces sub-instructions ("Article 3 is amended as follows:")
    replace      - a provision's text is replaced ("paragraph 6 is replaced by the following:")
    delete       - a provision is repealed ("point (p) is deleted")
    insert       - a new provision is added ("the following Article 3gaa is inserted:")
    append       - text is added to an existing provision ("the following points are added:")
    replace-text - a quoted phrase is swapped ("'Article 27' is replaced by 'Articles 27 and 27a'")
    delete-text  - a quoted phrase is removed ("the words '...' are deleted")
    external     - the change is set out in a separate annex document
    unknown      - none of the above
- scope: the provision the instruction operates *inside*, if it names one. This is the
  "in X," prefix: "in Article 6, paragraphs 1 and 2 are replaced" has scope "Article 6".
- target: the provision being changed, or for insert/append the provision being created.
- find_text / replace_text: the quoted strings, for replace-text and delete-text only.

Rules:
- scope, target, find_text and replace_text MUST be copied verbatim from the
  instruction. Never paraphrase, normalise or invent them.
- Use "" for anything the instruction does not state.
- Do not include the replacement legislation itself in any field; it is handled
  separately.

Drafting varies. "shall be replaced", "is hereby amended", "is amended" all mean the
same thing. Judge by meaning, not by wording.
"""

RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "amendment_instruction",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": sorted(OPERATIONS)},
                "scope": {"type": "string"},
                "target": {"type": "string"},
                "find_text": {"type": "string"},
                "replace_text": {"type": "string"},
            },
            "required": ["operation", "scope", "target", "find_text", "replace_text"],
            "additionalProperties": False,
        },
    },
}


@dataclass
class ClassifierStats:
    calls: int = 0
    cache_hits: int = 0
    rejected_operation: int = 0
    rejected_span: int = 0
    fell_back: int = 0
    errors: list[str] = field(default_factory=list)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _verbatim(value: str, source: str) -> str | None:
    """Accept a field only if the model copied it out of the instruction."""
    if not value:
        return None
    if _normalise(value) in _normalise(source):
        return value.strip() or None
    return None


class LlmClassifier:
    """Drop-in replacement for `rules.classify`, with the grammar as a fallback."""

    def __init__(self, client: OpenRouterClient, fall_back: bool = True):
        self.client = client
        self.fall_back = fall_back
        self.stats = ClassifierStats()

    def classify(self, instruction: Instruction) -> Instruction:
        normalise(instruction)
        source = instruction.raw
        if not source.strip():
            instruction.op = Op.UNKNOWN
            return instruction

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Instruction: {source}"},
        ]
        try:
            reply, cached = self.client.complete(messages, RESPONSE_SCHEMA)
        except Exception as exc:
            self.stats.errors.append(str(exc))
            return self._fallback(instruction, "request failed")

        self.stats.calls += 1
        if cached:
            self.stats.cache_hits += 1

        operation = OPERATIONS.get(str(reply.get("operation", "")).strip())
        if operation is None:
            self.stats.rejected_operation += 1
            return self._fallback(instruction, "unrecognised operation")

        # Every span must be the model's copy of the drafter's words.
        scope = _verbatim(reply.get("scope", ""), source)
        target = _verbatim(reply.get("target", ""), source)
        find_text = _verbatim(reply.get("find_text", ""), source)
        replace_text = _verbatim(reply.get("replace_text", ""), source)

        invented = [
            name for name, raw, kept in [
                ("scope", reply.get("scope", ""), scope),
                ("target", reply.get("target", ""), target),
                ("find_text", reply.get("find_text", ""), find_text),
                ("replace_text", reply.get("replace_text", ""), replace_text),
            ] if raw and kept is None
        ]
        if invented:
            self.stats.rejected_span += 1
            return self._fallback(
                instruction, f"non-verbatim {', '.join(invented)}"
            )

        instruction.op = operation
        instruction.scope_text = scope
        instruction.target_text = target
        instruction.find_text = find_text
        instruction.replace_text = replace_text
        instruction.resolver = "llm"
        if operation is Op.UNKNOWN:
            instruction.confidence = 0.0
        return instruction

    def _fallback(self, instruction: Instruction, reason: str) -> Instruction:
        from a2t.parse.rules import classify as rule_classify

        instruction.note(f"LLM classification rejected ({reason}); used the grammar")
        if not self.fall_back:
            instruction.op = Op.UNKNOWN
            instruction.confidence = 0.0
            return instruction
        self.stats.fell_back += 1
        return rule_classify(instruction)


def classify_all(blocks, classifier: LlmClassifier) -> None:
    for block in blocks:
        for instruction in block.walk():
            classifier.classify(instruction)
