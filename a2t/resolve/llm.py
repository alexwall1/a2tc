"""The LLM fallback tier.

The model's only job is **closed-set selection**: given an instruction the rule engine
could not resolve, and a list of node paths that actually exist in the retrieved act,
it picks one. It never writes legal text and never invents an address.

Three properties make a hallucinated amendment structurally impossible:

1. Candidates are generated from the real tree, so every option is a genuine node.
2. The reply is schema-validated and its `candidate_id` must be in the list; anything
   else is rejected, not repaired.
3. Replacement text always comes verbatim from the PDF — the model is never asked for
   it and its output is never used as document content.

A rejected reply leaves the instruction unresolved, which surfaces as a flag. The tier
can lower coverage-by-rules but can never silently corrupt output.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path as FsPath

import requests

from a2t.config import Settings, load_settings
from a2t.model.instruction import Instruction
from a2t.model.path import Path, Step, candidates as enumerate_candidates
from a2t.model.tree import Kind, Node

# Prompt size drives latency here: the earlier benchmark ran ~15-20 s per call with
# 60 candidates at 140 characters. The choice is between provisions of one scope, so
# a shorter preview is enough to tell them apart.
MAX_CANDIDATES = 40
PREVIEW_CHARS = 80

SYSTEM_PROMPT = """\
You identify which provision of an EU legal act an amendment instruction refers to.

You will be given one instruction from an amending act, the scope it appears under, \
and a numbered list of candidate provisions that exist in the base act.

Choose the single candidate the instruction addresses. Rules:
- Answer only with a candidate id from the list provided.
- If none of the candidates is a convincing match, answer with the id "none".
- Never invent an identifier, and never propose replacement wording.
- Prefer the candidate whose existing text the instruction would sensibly replace, \
delete or extend.
"""

RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "provision_choice",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "candidate_id": {
                    "type": "string",
                    "description": "id of the chosen candidate, or 'none'",
                },
                "confidence": {
                    "type": "number",
                    "description": "0..1 confidence in the choice",
                },
                "reason": {"type": "string", "description": "one short sentence"},
            },
            "required": ["candidate_id", "confidence", "reason"],
            "additionalProperties": False,
        },
    },
}


class LlmError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    id: str
    path: Path
    citation: str
    preview: str


@dataclass
class Choice:
    candidate: Candidate | None
    confidence: float
    reason: str
    rejected: str | None = None  # why a reply was discarded, if it was
    cached: bool = False


@dataclass
class LlmStats:
    calls: int = 0
    cache_hits: int = 0
    rejected: int = 0
    chose_none: int = 0
    resolved: int = 0
    errors: list[str] = field(default_factory=list)


def build_candidates(scope: Node, limit: int = MAX_CANDIDATES) -> list[Candidate]:
    """Addressable provisions under `scope`, as a closed set for the model."""
    out: list[Candidate] = []
    for path in enumerate_candidates(scope):
        if len(out) >= limit:
            break
        node = _walk(scope, path)
        if node is None or node.kind is Kind.SENTENCE:
            continue
        text = node.full_text.strip()
        out.append(
            Candidate(
                id=f"c{len(out) + 1}",
                path=path,
                citation=node.citation or str(path),
                preview=text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else ""),
            )
        )
    return out


def _walk(root: Node, path: Path) -> Node | None:
    node: Node | None = root
    for step in path.steps:
        if node is None:
            return None
        node = (
            node.by_key(step.kind, step.key)
            if step.key is not None
            else node.by_index(step.kind, step.index or 0)
        )
    return node


class OpenRouterClient:
    """Minimal OpenRouter chat client with on-disk response caching."""

    def __init__(self, settings: Settings | None = None,
                 cache_dir: FsPath | str | None = None):
        self.settings = settings or load_settings()
        self.cache_dir = FsPath(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, key: str) -> FsPath | None:
        return self.cache_dir / f"{key}.json" if self.cache_dir else None

    def complete(self, messages: list[dict], schema: dict) -> tuple[dict, bool]:
        """Return `(parsed_json, from_cache)`."""
        digest = hashlib.sha256(
            json.dumps([self.settings.model, messages], sort_keys=True).encode()
        ).hexdigest()[:32]

        path = self._cache_path(digest)
        if path and path.is_file():
            return json.loads(path.read_text(encoding="utf-8")), True

        body = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": 0,
            "response_format": schema,
        }
        try:
            response = requests.post(
                f"{self.settings.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.require_key()}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self.settings.timeout,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except Exception as exc:
            raise LlmError(str(exc)) from exc

        if path:
            path.write_text(json.dumps(parsed), encoding="utf-8")
        return parsed, False


class LlmResolver:
    """Resolves an instruction by choosing among real provisions of the act."""

    def __init__(self, client: OpenRouterClient, min_confidence: float = 0.5):
        self.client = client
        self.min_confidence = min_confidence
        self.stats = LlmStats()

    def choose(self, instruction: Instruction, scope: Node,
               candidates: list[Candidate] | None = None) -> Choice:
        options = candidates if candidates is not None else build_candidates(scope)
        if not options:
            return Choice(None, 0.0, "", rejected="no candidates under scope")

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._render(instruction, scope, options)},
        ]

        try:
            reply, cached = self.client.complete(messages, RESPONSE_SCHEMA)
        except LlmError as exc:
            self.stats.errors.append(str(exc))
            return Choice(None, 0.0, "", rejected=f"request failed: {exc}")

        self.stats.calls += 1
        if cached:
            self.stats.cache_hits += 1

        return self._validate(reply, options, cached)

    # ------------------------------------------------------------------ internals

    def _render(self, instruction: Instruction, scope: Node,
                options: list[Candidate]) -> str:
        where = scope.citation or "the whole act"
        lines = [
            f"Scope: {where}",
            f"Instruction: {instruction.raw}",
        ]
        if instruction.payload:
            lines.append(
                f"Replacement text supplied by the instruction (for identification "
                f"only): {instruction.payload[:400]}"
            )
        lines.append("\nCandidates:")
        for option in options:
            lines.append(f"  {option.id}: {option.citation} — {option.preview}")
        lines.append("\nWhich candidate does the instruction address?")
        return "\n".join(lines)

    def _validate(self, reply: dict, options: list[Candidate], cached: bool) -> Choice:
        by_id = {option.id: option for option in options}
        chosen = str(reply.get("candidate_id", "")).strip()
        confidence = float(reply.get("confidence", 0.0) or 0.0)
        reason = str(reply.get("reason", "")).strip()

        if chosen == "none":
            self.stats.chose_none += 1
            return Choice(None, confidence, reason, rejected="model declined to choose",
                          cached=cached)

        if chosen not in by_id:
            # The one failure mode that must never reach the document.
            self.stats.rejected += 1
            return Choice(
                None, confidence, reason,
                rejected=f"model returned unknown id {chosen!r}", cached=cached,
            )

        if confidence < self.min_confidence:
            self.stats.rejected += 1
            return Choice(
                None, confidence, reason,
                rejected=f"confidence {confidence:.2f} below "
                         f"{self.min_confidence:.2f}",
                cached=cached,
            )

        self.stats.resolved += 1
        return Choice(by_id[chosen], confidence, reason, cached=cached)
