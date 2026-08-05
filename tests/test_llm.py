import os

import pytest

pytest.importorskip("claude_agent_sdk")

from archaeon import llm  # noqa: E402


class _FakeResult:
    subtype = "success"

    def __init__(self, result):
        self.result = result


class _FakeNonResult:
    """A non-terminal message (e.g. init/assistant) the classifier ignores."""

    subtype = "init"


def _fake_query_factory(messages):
    async def fake_query(prompt, options):
        fake_query.last_options = options
        for message in messages:
            yield message
    return fake_query


def test_ask_extracts_final_result(monkeypatch):
    fake = _fake_query_factory([_FakeNonResult(), _FakeResult("  EMB-42  ")])
    monkeypatch.setattr(llm, "query", fake)
    classifier = llm.AgentClassifier("cheap-model")
    assert classifier.ask("which ticket?") == "EMB-42"
    # model and single-shot constraints are passed through to the SDK options
    assert fake.last_options.model == "cheap-model"
    assert fake.last_options.max_turns == 1
    assert fake.last_options.allowed_tools == []


def test_max_turns_is_configurable(monkeypatch):
    fake = _fake_query_factory([_FakeResult("ok")])
    monkeypatch.setattr(llm, "query", fake)
    classifier = llm.AgentClassifier("cheap-model", max_turns=4)
    classifier.ask("verify this claim")
    assert fake.last_options.max_turns == 4


def test_ask_returns_empty_when_no_success(monkeypatch):
    monkeypatch.setattr(llm, "query", _fake_query_factory([_FakeNonResult()]))
    classifier = llm.AgentClassifier("cheap-model")
    assert classifier.ask("which ticket?") == ""


def test_cli_auth_env_hides_api_key_then_restores(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "bearer-test")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-test")
    with llm._cli_auth_env():
        # API-key vars are hidden so the CLI falls through to its own login
        assert "ANTHROPIC_API_KEY" not in os.environ
        assert "ANTHROPIC_AUTH_TOKEN" not in os.environ
        # the OAuth/subscription token is left in place
        assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-test"
    # restored on exit
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-test"
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "bearer-test"


def test_cli_auth_env_noop_when_key_absent(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with llm._cli_auth_env():
        assert "ANTHROPIC_API_KEY" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ


class _FakeCostedResult:
    """Terminal ResultMessage carrying the SDK's real cost fields."""

    subtype = "success"

    def __init__(self, result, usd=0.42):
        self.result = result
        self.total_cost_usd = usd
        self.num_turns = 2
        self.usage = {"input_tokens": 1200, "output_tokens": 80,
                      "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}


def test_ask_without_a_meter_is_unchanged(monkeypatch):
    fake = _fake_query_factory([_FakeCostedResult("  YES  ")])
    monkeypatch.setattr(llm, "query", fake)
    classifier = llm.AgentClassifier("cheap-model")
    assert classifier.ask("q") == "YES"
    assert classifier._meter is None


def test_ask_records_one_call_into_the_meter(monkeypatch):
    from archaeon.cost import CostMeter

    monkeypatch.setattr(llm, "query",
                        _fake_query_factory([_FakeCostedResult("YES")]))
    meter = CostMeter()
    classifier = llm.AgentClassifier("sonnet-model", max_turns=4,
                                     meter=meter, stage="verify")
    assert classifier.ask("q") == "YES"

    assert len(meter.calls) == 1
    call = meter.calls[0]
    assert call.stage == "verify"
    # model is caller-supplied; the SDK message has no top-level model field
    assert call.model == "sonnet-model"
    assert call.usd == 0.42
    assert call.input_tokens == 1200
    assert call.num_turns == 2


def test_meter_untouched_when_no_success_message(monkeypatch):
    from archaeon.cost import CostMeter

    monkeypatch.setattr(llm, "query", _fake_query_factory([_FakeNonResult()]))
    meter = CostMeter()
    classifier = llm.AgentClassifier("m", meter=meter, stage="verify")
    assert classifier.ask("q") == ""
    assert meter.calls == []


class _FakeErroredResult:
    """Terminal ResultMessage for a turn that ended in an SDK error.

    ``error_max_turns`` / ``error_during_execution`` carry the same
    total_cost_usd / usage / num_turns fields as a success result and have
    no ``.result`` — the quota was spent regardless.
    """

    def __init__(self, subtype, usd=0.31):
        self.subtype = subtype
        self.total_cost_usd = usd
        self.num_turns = 4
        self.usage = {"input_tokens": 900, "output_tokens": 12,
                      "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}


@pytest.mark.parametrize("subtype",
                         ["error_max_turns", "error_during_execution"])
def test_errored_terminal_call_is_recorded(monkeypatch, subtype):
    """verify_claims swallows per-claim exceptions, so an overloaded or
    turn-exhausted call otherwise burns quota completely invisibly.
    """
    from archaeon.cost import CostMeter

    monkeypatch.setattr(llm, "query", _fake_query_factory(
        [_FakeErroredResult(subtype)]))
    meter = CostMeter()
    classifier = llm.AgentClassifier("m", meter=meter, stage="verify")
    # no success message -> still no text (unchanged contract) ...
    assert classifier.ask("q") == ""
    # ... but the call is counted, and marked as a failure.
    assert len(meter.calls) == 1
    call = meter.calls[0]
    assert call.stage == "verify"
    assert call.usd == 0.31
    assert call.input_tokens == 900
    assert call.failed is True
    assert meter.failed_calls == 1


def test_errored_call_without_a_meter_is_still_a_noop(monkeypatch):
    monkeypatch.setattr(llm, "query", _fake_query_factory(
        [_FakeErroredResult("error_max_turns")]))
    assert llm.AgentClassifier("m").ask("q") == ""


def test_success_after_error_still_yields_the_result(monkeypatch):
    from archaeon.cost import CostMeter

    monkeypatch.setattr(llm, "query", _fake_query_factory(
        [_FakeErroredResult("error_during_execution"),
         _FakeCostedResult("YES")]))
    meter = CostMeter()
    assert llm.AgentClassifier("m", meter=meter,
                               stage="synthesize").ask("q") == "YES"
    assert len(meter.calls) == 2
    assert meter.failed_calls == 1
