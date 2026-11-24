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
    LlmClient,
    build_candidates,
)

from _samples import cached

CACHE = cached("02003L0087-20240301")


class FakeClient:
    """Stands in for LlmClient, recording what it was asked."""

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


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """No key anywhere but where a test puts one: not the environment, the project root
    or the secret mount."""
    import a2t.config as config

    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(config, "project_root", lambda: tmp_path / "no-project-root")
    monkeypatch.setattr(config, "SECRET_MOUNT", tmp_path / "no-secret-mount")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_key_read_from_environment(isolated, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "rq-from-env")
    assert find_key(isolated) == "rq-from-env"


def test_key_read_from_api_key_file(isolated):
    (isolated / "api-key.txt").write_text("rq-from-file\n")
    assert find_key(isolated) == "rq-from-file"


def test_key_file_is_found_in_a_parent_directory(isolated):
    (isolated / "api-key.txt").write_text("rq-from-parent\n")
    deep = isolated / "webapp-data" / "uploads"
    deep.mkdir(parents=True)
    assert find_key(deep) == "rq-from-parent"


def test_key_file_is_found_in_the_project_root(isolated, monkeypatch):
    """A web app whose data directory lies elsewhere still finds the key beside its code."""
    import a2t.config as config

    root = isolated / "app"
    root.mkdir()
    (root / "api-key.txt").write_text("rq-from-root\n")
    monkeypatch.setattr(config, "project_root", lambda: root)
    elsewhere = isolated / "elsewhere" / "uploads"
    elsewhere.mkdir(parents=True)
    monkeypatch.chdir(elsewhere)
    assert find_key(elsewhere) == "rq-from-root"


def test_key_file_is_found_in_the_secret_mount(isolated, monkeypatch):
    import a2t.config as config

    mount = isolated / "etc-secrets"
    mount.mkdir()
    (mount / "api-key.txt").write_text("rq-from-mount\n")
    monkeypatch.setattr(config, "SECRET_MOUNT", mount)
    assert find_key(isolated) == "rq-from-mount"


def test_the_environment_takes_priority_over_the_file(isolated, monkeypatch):
    (isolated / "api-key.txt").write_text("rq-from-file\n")
    monkeypatch.setenv(KEY_ENV, "rq-from-env")
    assert find_key(isolated) == "rq-from-env"


def test_an_empty_key_file_is_no_key(isolated):
    (isolated / "api-key.txt").write_text("  \n")
    assert find_key(isolated) is None


def test_the_earlier_providers_key_is_never_read(isolated, monkeypatch):
    """A key issued by another company must not be sent to this router's host."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-old")
    (isolated / ".openrouter_key").write_text("sk-or-legacy\n")
    (isolated / ".secrets").mkdir()
    (isolated / ".secrets" / "openrouter-key").write_text("sk-or-secrets\n")
    assert find_key(isolated) is None


def test_missing_key_raises_actionable_error(isolated):
    from a2t.config import ConfigError

    with pytest.raises(ConfigError, match=r"REQUESTY_API_KEY.*api-key\.txt"):
        Settings(api_key=None).require_key()


def test_missing_model_raises_actionable_error():
    from a2t.config import ConfigError

    with pytest.raises(ConfigError, match=r"--model.*A2T_MODEL"):
        Settings(api_key="k", model=None).require_model()


def test_the_endpoint_is_requesty():
    assert Settings().base_url == "https://router.requesty.ai/v1"


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
    client = LlmClient(Settings(api_key="k", model="m"), cache_dir=tmp_path)
    messages = [{"role": "user", "content": "hello"}]

    first, cached_first = client.complete(messages, RESPONSE_SCHEMA)
    second, cached_second = client.complete(messages, RESPONSE_SCHEMA)

    assert first == second
    assert (cached_first, cached_second) == (False, True)
    assert posted["n"] == 1, "the second call must not hit the network"


def test_cache_key_separates_different_models(tmp_path, monkeypatch):
    posted = {"n": 0}

    class Response:
        status_code = 200

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
    LlmClient(Settings(api_key="k", model="one"),
                     cache_dir=tmp_path).complete(messages, RESPONSE_SCHEMA)
    LlmClient(Settings(api_key="k", model="two"),
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


# ------------------------------------------------------------- the router, faked


class Reply:
    """A stand-in for requests.Response."""

    def __init__(self, status=200, content=None):
        self.status_code = status
        self._content = content if content is not None else json.dumps(
            {"candidate_id": "c1", "confidence": 0.9, "reason": "ok"})

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code} Client Error", response=self)

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def router(monkeypatch, *replies):
    """Replace requests.post; returns the list of (url, headers, body) it was called with."""
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append((url, headers, json))
        return replies[min(len(calls), len(replies)) - 1]

    monkeypatch.setattr("a2t.resolve.llm.requests.post", post)
    return calls


MESSAGES = [{"role": "user", "content": "hello"}]


def test_a_request_goes_to_requesty_with_the_bearer_key_and_the_model(monkeypatch):
    calls = router(monkeypatch, Reply())
    LlmClient(Settings(api_key="rq-secret", model="vendor/free-model")).complete(
        MESSAGES, RESPONSE_SCHEMA)

    url, headers, body = calls[0]
    assert url == "https://router.requesty.ai/v1/chat/completions"
    assert headers["Authorization"] == "Bearer rq-secret"
    assert body["model"] == "vendor/free-model"
    assert body["response_format"]["type"] == "json_schema"


def test_a_request_without_a_model_never_leaves_the_machine(monkeypatch):
    calls = router(monkeypatch, Reply())
    with pytest.raises(Exception, match="no model set"):
        LlmClient(Settings(api_key="k", model=None)).complete(MESSAGES, RESPONSE_SCHEMA)
    assert calls == []


def test_a_model_that_refuses_a_json_schema_is_asked_for_a_json_object(monkeypatch):
    calls = router(monkeypatch, Reply(400), Reply())
    reply, cached = LlmClient(Settings(api_key="k", model="m")).complete(
        MESSAGES, RESPONSE_SCHEMA)

    assert reply["candidate_id"] == "c1" and not cached
    assert len(calls) == 2
    retry = calls[1][2]
    assert retry["response_format"] == {"type": "json_object"}
    # The schema travels in the prompt this time, after the original messages.
    assert retry["messages"][:-1] == MESSAGES
    assert "JSON Schema" in retry["messages"][-1]["content"]
    assert "candidate_id" in retry["messages"][-1]["content"]


def test_a_second_refusal_is_an_error_not_a_loop(monkeypatch):
    from a2t.resolve.llm import LlmError

    calls = router(monkeypatch, Reply(400), Reply(400))
    with pytest.raises(LlmError):
        LlmClient(Settings(api_key="k", model="m")).complete(MESSAGES, RESPONSE_SCHEMA)
    assert len(calls) == 2


def test_other_errors_are_not_retried_as_a_json_object(monkeypatch):
    from a2t.resolve.llm import LlmError

    calls = router(monkeypatch, Reply(401))
    with pytest.raises(LlmError):
        LlmClient(Settings(api_key="k", model="m")).complete(MESSAGES, RESPONSE_SCHEMA)
    assert len(calls) == 1


def test_a_reply_wrapped_in_a_code_fence_is_read(monkeypatch):
    fenced = "```json\n" + json.dumps(
        {"candidate_id": "c2", "confidence": 0.8, "reason": "x"}) + "\n```"
    router(monkeypatch, Reply(content=fenced))
    reply, _ = LlmClient(Settings(api_key="k", model="m")).complete(MESSAGES, RESPONSE_SCHEMA)
    assert reply["candidate_id"] == "c2"


def test_a_reply_that_is_not_json_is_an_error(monkeypatch):
    from a2t.resolve.llm import LlmError

    router(monkeypatch, Reply(content="I think it is point (a)."))
    with pytest.raises(LlmError):
        LlmClient(Settings(api_key="k", model="m")).complete(MESSAGES, RESPONSE_SCHEMA)


def test_the_cache_key_separates_routers(tmp_path, monkeypatch):
    """The same model id on another router is another model."""
    calls = router(monkeypatch, Reply())
    LlmClient(Settings(api_key="k", model="m", base_url="https://one.example/v1"),
              cache_dir=tmp_path).complete(MESSAGES, RESPONSE_SCHEMA)
    LlmClient(Settings(api_key="k", model="m", base_url="https://two.example/v1"),
              cache_dir=tmp_path).complete(MESSAGES, RESPONSE_SCHEMA)
    assert len(calls) == 2


# ------------------------------------------------------------ the CLI fails fast


@pytest.mark.parametrize("command", ["resolve", "convert"])
def test_an_llm_run_with_no_key_stops_before_reading_anything(isolated, command):
    from click.testing import CliRunner

    from a2t.cli import main

    pdf = isolated / "proposal.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    result = CliRunner().invoke(main, [command, str(pdf), "--llm"])

    assert result.exit_code != 0
    assert "no Requesty API key" in result.output
    assert "api-key.txt" in result.output


@pytest.mark.parametrize("command", ["resolve", "convert"])
def test_an_llm_run_with_a_key_but_no_model_says_so(isolated, monkeypatch, command):
    from click.testing import CliRunner

    from a2t.cli import main

    monkeypatch.setenv(KEY_ENV, "rq-key")
    monkeypatch.delenv("A2T_MODEL", raising=False)
    monkeypatch.setattr("a2t.config.DEFAULT_MODEL", None)  # as if no default were built in
    pdf = isolated / "proposal.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    result = CliRunner().invoke(main, [command, str(pdf), "--llm"])

    assert result.exit_code != 0
    assert "no model set" in result.output
    assert "A2T_MODEL" in result.output


def test_the_default_model_is_gemma_and_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.delenv("A2T_MODEL", raising=False)
    assert load_settings(tmp_path).model == "gemma-4-31b-it"
    assert load_settings(tmp_path, model="vendor/other").model == "vendor/other"
    monkeypatch.setenv("A2T_MODEL", "from/env")
    assert load_settings(tmp_path).model == "from/env"
    assert load_settings(tmp_path, model="vendor/other").model == "vendor/other"
