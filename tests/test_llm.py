"""Phase 6: the LLM fallback tier.

No network. A fake client returns scripted replies so the validation boundary — the
part that must never let a bad answer through — is tested directly.
"""

import json
from pathlib import Path as FsPath

import pytest

from a2t.config import KEY_ENV, Settings, find_key, load_settings
from a2t.model.instruction import Instruction, Op
from a2t.model.tree import Act, Kind, Node
from a2t.resolve.llm import (
    RESPONSE_SCHEMA,
    Candidate,
    LlmResolver,
    OpenRouterClient,
    build_candidates,
)

from _samples import cached

CACHE = cached("02003L0087-20240301")


class FakeClient:
    """Stands in for OpenRouterClient, recording what it was asked."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def complete(self, messages, schema):
        self.calls.append(messages)
        if not self.replies:
            raise AssertionError("unexpected extra call")
        return self.replies.pop(0), False


def make_act() -> Act:
    act = Act(celex="TEST", title="Test act")
    article = act.root.add(Node(Kind.ARTICLE, key="3", text=""))
    for key, text in [("a", "‘allowance’ means one tonne."),
                      ("b", "‘emissions’ means the release of gases."),
                      ("h", "‘new entrant’ means any installation.")]:
        article.add(Node(Kind.POINT, key=key, text=text))
    return act


def instruction(raw="point (h) is replaced by the following:") -> Instruction:
    return Instruction(id="t", marker="b", level=2, raw=raw, source_line=1,
                       op=Op.REPLACE_NODE)


# ------------------------------------------------------------------- candidates


def test_candidates_come_from_the_real_tree():
    act = make_act()
    options = build_candidates(act.article("3"))
    assert [o.citation.split("/")[-1].strip() for o in options] == [
        "point (a)", "point (b)", "point (h)"
    ]
    assert all(o.id.startswith("c") for o in options)


def test_candidates_include_a_text_preview():
    options = build_candidates(make_act().article("3"))
    assert "allowance" in options[0].preview


def test_candidate_limit_is_respected():
    assert len(build_candidates(make_act().article("3"), limit=2)) == 2


@pytest.mark.skipif(not (CACHE / "works.parquet").is_file(),
                    reason="retrieve the base act first")
def test_candidates_on_the_real_act():
    import polars as pl

    from a2t.model.assemble import assemble

    works = pl.read_parquet(CACHE / "works.parquet").row(0, named=True)
    act = assemble(works["full_text_html"], "x", works["title"])
    options = build_candidates(act.article("3"))
    assert len(options) > 30
    assert any("point (ag)" in o.citation for o in options)


# ------------------------------------------------------------------- validation


def test_valid_choice_is_accepted():
    act = make_act()
    options = build_candidates(act.article("3"))
    target = next(o for o in options if o.citation.endswith("point (h)"))
    client = FakeClient({"candidate_id": target.id, "confidence": 0.9, "reason": "ok"})
    choice = LlmResolver(client).choose(instruction(), act.article("3"), options)
    assert choice.candidate is target
    assert choice.rejected is None


def test_unknown_candidate_id_is_rejected():
    """The failure that must never reach the document."""
    act = make_act()
    options = build_candidates(act.article("3"))
    client = FakeClient({"candidate_id": "c999", "confidence": 0.99, "reason": "x"})
    resolver = LlmResolver(client)
    choice = resolver.choose(instruction(), act.article("3"), options)
    assert choice.candidate is None
    assert "unknown id" in choice.rejected
    assert resolver.stats.rejected == 1


def test_invented_path_is_rejected():
    act = make_act()
    options = build_candidates(act.article("3"))
    client = FakeClient({"candidate_id": "Article 99", "confidence": 1.0, "reason": ""})
    assert LlmResolver(client).choose(instruction(), act.article("3"),
                                      options).candidate is None


def test_model_may_decline():
    act = make_act()
    options = build_candidates(act.article("3"))
    client = FakeClient({"candidate_id": "none", "confidence": 0.2, "reason": "unclear"})
    resolver = LlmResolver(client)
    choice = resolver.choose(instruction(), act.article("3"), options)
    assert choice.candidate is None
    assert "declined" in choice.rejected
    assert resolver.stats.chose_none == 1


def test_low_confidence_is_rejected():
    act = make_act()
    options = build_candidates(act.article("3"))
    client = FakeClient({"candidate_id": options[0].id, "confidence": 0.1, "reason": ""})
    choice = LlmResolver(client, min_confidence=0.5).choose(
        instruction(), act.article("3"), options
    )
    assert choice.candidate is None
    assert "below" in choice.rejected


def test_no_candidates_means_no_call():
    client = FakeClient()
    empty = Node(Kind.ARTICLE, key="9")
    choice = LlmResolver(client).choose(instruction(), empty, [])
    assert choice.candidate is None
    assert client.calls == []


def test_request_failure_is_contained():
    class Broken:
        def complete(self, messages, schema):
            from a2t.resolve.llm import LlmError

            raise LlmError("connection reset")

    act = make_act()
    resolver = LlmResolver(Broken())
    choice = resolver.choose(instruction(), act.article("3"),
                             build_candidates(act.article("3")))
    assert choice.candidate is None
    assert "request failed" in choice.rejected
    assert resolver.stats.errors


# ------------------------------------------------------------------ the prompt


def test_prompt_offers_only_real_candidates():
    act = make_act()
    options = build_candidates(act.article("3"))
    client = FakeClient({"candidate_id": options[0].id, "confidence": 0.8, "reason": ""})
    LlmResolver(client).choose(instruction(), act.article("3"), options)

    user = client.calls[0][1]["content"]
    for option in options:
        assert option.id in user
    assert "Which candidate does the instruction address?" in user


def test_model_is_never_asked_to_write_legal_text():
    from a2t.resolve.llm import SYSTEM_PROMPT

    lowered = SYSTEM_PROMPT.lower()
    assert "never propose replacement wording" in lowered
    assert "never invent an identifier" in lowered
    assert "answer only with a candidate id" in lowered


def test_schema_is_strict():
    schema = RESPONSE_SCHEMA["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False
    assert set(schema["schema"]["required"]) == {"candidate_id", "confidence", "reason"}


# -------------------------------------------------------------------- fallback


def test_fallback_marks_recovered_resolutions():
    from a2t.model.path import Path as NodePath
    from a2t.model.path import Step
    from a2t.resolve.fallback import apply_llm_fallback
    from a2t.resolve.resolver import Resolution, ResolutionReport

    act = make_act()
    article = act.article("3")
    options = build_candidates(article)
    target = next(o for o in options if o.citation.endswith("point (h)"))

    instr = instruction()
    report = ResolutionReport([
        Resolution(instr, path=NodePath((Step(Kind.ARTICLE, "3"),
                                         Step(Kind.POINT, "zz"))),
                   status="unresolved", reason="not found")
    ])
    client = FakeClient({"candidate_id": target.id, "confidence": 0.8, "reason": "m"})
    outcome = apply_llm_fallback(report, act, LlmResolver(client))

    assert outcome.recovered == 1
    assert report.results[0].status == "resolved"
    assert report.results[0].node.key == "h"
    assert instr.resolver == "llm"
    assert any("resolved by the LLM tier" in n for n in instr.notes)


def test_fallback_leaves_rejected_instructions_unresolved():
    from a2t.resolve.fallback import apply_llm_fallback
    from a2t.resolve.resolver import Resolution, ResolutionReport

    act = make_act()
    instr = instruction()
    report = ResolutionReport([
        Resolution(instr, status="unresolved", reason="not found")
    ])
    client = FakeClient({"candidate_id": "c999", "confidence": 1.0, "reason": ""})
    outcome = apply_llm_fallback(report, act, LlmResolver(client))

    assert outcome.recovered == 0
    assert report.results[0].status == "unresolved"
    assert instr.resolver == "rule"
    assert any("could not resolve" in n for n in instr.notes)


def test_fallback_never_supplies_payload_text():
    """Whatever the model says, replacement text stays as parsed from the PDF."""
    from a2t.resolve.fallback import apply_llm_fallback
    from a2t.resolve.resolver import Resolution, ResolutionReport

    act = make_act()
    instr = instruction()
    instr.payload = "VERBATIM FROM PDF"
    options = build_candidates(act.article("3"))
    report = ResolutionReport([Resolution(instr, status="unresolved")])
    client = FakeClient({
        "candidate_id": options[0].id, "confidence": 0.9,
        "reason": "the text should read 'SOMETHING THE MODEL MADE UP'",
    })
    apply_llm_fallback(report, act, LlmResolver(client))
    assert instr.payload == "VERBATIM FROM PDF"


# ---------------------------------------------------------------------- config


def test_key_read_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(KEY_ENV, "sk-or-from-env")
    assert find_key(tmp_path) == "sk-or-from-env"


def test_key_read_from_file(monkeypatch, tmp_path):
    monkeypatch.delenv(KEY_ENV, raising=False)
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets" / "openrouter-key").write_text("sk-or-from-file\n")
    assert find_key(tmp_path) == "sk-or-from-file"


def test_key_read_from_legacy_file(monkeypatch, tmp_path):
    monkeypatch.delenv(KEY_ENV, raising=False)
    (tmp_path / ".openrouter_key").write_text("sk-or-from-legacy\n")
    assert find_key(tmp_path) == "sk-or-from-legacy"


def test_secrets_file_takes_priority_over_legacy(monkeypatch, tmp_path):
    monkeypatch.delenv(KEY_ENV, raising=False)
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets" / "openrouter-key").write_text("sk-or-from-file\n")
    (tmp_path / ".openrouter_key").write_text("sk-or-from-legacy\n")
    assert find_key(tmp_path) == "sk-or-from-file"


def test_missing_key_raises_actionable_error(monkeypatch, tmp_path):
    from a2t.config import ConfigError

    monkeypatch.delenv(KEY_ENV, raising=False)
    with pytest.raises(ConfigError,
                       match=r"OPENROUTER_API_KEY.*\.secrets/openrouter-key"):
        Settings(api_key=None).require_key()


def test_model_is_configurable(monkeypatch, tmp_path):
    monkeypatch.delenv("A2T_MODEL", raising=False)
    assert load_settings(tmp_path, model="vendor/tiny").model == "vendor/tiny"


# ----------------------------------------------------------------------- cache


def test_second_identical_request_is_served_from_cache(tmp_path, monkeypatch):
    """Re-runs must cost nothing and stay deterministic."""
    posted = {"n": 0}

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(
                {"candidate_id": "c1", "confidence": 0.9, "reason": "ok"}
            )}}]}

    def fake_post(*args, **kwargs):
        posted["n"] += 1
        return Response()

    monkeypatch.setattr("a2t.resolve.llm.requests.post", fake_post)
    client = OpenRouterClient(Settings(api_key="k", model="m"), cache_dir=tmp_path)
    messages = [{"role": "user", "content": "hello"}]

    first, cached_first = client.complete(messages, RESPONSE_SCHEMA)
    second, cached_second = client.complete(messages, RESPONSE_SCHEMA)

    assert first == second
    assert (cached_first, cached_second) == (False, True)
    assert posted["n"] == 1, "the second call must not hit the network"


def test_cache_key_separates_different_models(tmp_path, monkeypatch):
    posted = {"n": 0}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(
                {"candidate_id": "c1", "confidence": 0.9, "reason": ""}
            )}}]}

    monkeypatch.setattr("a2t.resolve.llm.requests.post",
                        lambda *a, **k: (posted.__setitem__("n", posted["n"] + 1),
                                         Response())[1])
    messages = [{"role": "user", "content": "hello"}]
    OpenRouterClient(Settings(api_key="k", model="one"),
                     cache_dir=tmp_path).complete(messages, RESPONSE_SCHEMA)
    OpenRouterClient(Settings(api_key="k", model="two"),
                     cache_dir=tmp_path).complete(messages, RESPONSE_SCHEMA)
    assert posted["n"] == 2


def test_an_instruction_nobody_read_is_not_offered_to_the_llm():
    """Choosing a provision for it would mark it resolved with nothing to apply."""
    from a2t.resolve.fallback import apply_llm_fallback
    from a2t.resolve.resolver import Resolution, ResolutionReport

    act = make_act()
    instr = instruction()
    report = ResolutionReport([
        Resolution(instr, node=act.article("3"), status="unresolved",
                   reason="no rule matched this instruction", unread=True)
    ])
    client = FakeClient()
    outcome = apply_llm_fallback(report, act, LlmResolver(client))

    assert outcome.attempted == 0
    assert client.calls == []
    assert report.results[0].status == "unresolved"
