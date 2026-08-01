"""Repair the drafting defects that real amendment text contains.

Every rule here corresponds to a defect observed in COM(2026) 616 final (PLAN.md
§3.5). They apply to the *instruction sentence* only — never to a payload, whose text
must stay byte-identical to the source.

Each repair records a note on the instruction, so the reconciliation report can show
that the text was adjusted and why.
"""

from __future__ import annotations

import re

from a2t.model.instruction import Instruction

# Editorial asides left in the operative text, e.g.
# "[N.B.: date of application may need to be changed ...]"
EDITORIAL_NOTE = re.compile(r"\[\s*N\.?B\.?\s*:.*?\]", re.I | re.S)

# "in paragraph 2 is amended as follows:" — a stray preposition makes the subject
# read as a scope, leaving the sentence without one.
STRAY_IN = re.compile(r"^in\s+(?=[^,]+?\s+is\s+amended\s+as\s+follows)", re.I)

# The date-replacement instruction uses double quotes; everything else uses single.
QUOTE_MAP = str.maketrans({"“": "‘", "”": "’", '"': "‘"})


def normalise(instruction: Instruction) -> Instruction:
    """Clean an instruction sentence in place, recording every change."""
    original = instruction.raw
    text = original

    if EDITORIAL_NOTE.search(text):
        text = EDITORIAL_NOTE.sub("", text)
        instruction.note("editorial [N.B. ...] aside removed from the instruction")

    if STRAY_IN.match(text):
        text = STRAY_IN.sub("", text)
        instruction.note(
            "malformed 'in X is amended as follows' — leading preposition dropped"
        )

    text = re.sub(r"\s+", " ", text).strip()
    if text != original:
        instruction.raw = text
    return instruction


def unify_quotes(text: str) -> str:
    """Map every quote family onto ‘ ’ so one pattern can match them all.

    Used for *matching* the instruction sentence. Payload text is never passed
    through this.
    """
    return text.translate(QUOTE_MAP)
