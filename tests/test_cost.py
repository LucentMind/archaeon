import json

import pytest

from archaeon.cost import (
    ROUTE_VARS, CallCost, CostMeter, billing_route_overridden,
    call_cost_from_message, is_error_result, is_success_result,
    is_terminal_result)


class _Msg:
    """Minimal stand-in for the SDK's terminal ResultMessage.

    Only the attributes passed in exist, so a test can model an errored
    turn simply by omitting ``total_cost_usd``.
    """

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


def test_call_cost_from_message_extracts_dict_usage():
    msg = _Msg(total_cost_usd=0.25, num_turns=3, usage={
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 10,
    })
    assert call_cost_from_message(msg, "verify", "claude-sonnet-5") == \
        CallCost(stage="verify", model="claude-sonnet-5", usd=0.25,
                 input_tokens=1000, output_tokens=200,
                 cache_read_tokens=50, cache_creation_tokens=10,
                 num_turns=3)


def test_call_cost_is_zero_usd_when_total_cost_missing():
    # An errored/partial turn: no total_cost_usd, but the attempt counts.
    msg = _Msg(usage={"input_tokens": 10, "output_tokens": 2})
    c = call_cost_from_message(msg, "synthesize", "claude-sonnet-5")
    assert c.usd == 0.0
    assert c.input_tokens == 10
    assert c.cache_read_tokens == 0  # absent usage keys -> 0
    assert c.num_turns == 0


def test_call_cost_reads_attr_style_usage():
    class _Usage:
        input_tokens = 7
        output_tokens = 1
        cache_read_input_tokens = 4

    msg = _Msg(total_cost_usd=0.01, usage=_Usage(), num_turns=1)
    c = call_cost_from_message(msg, "cluster-label", "claude-haiku-4-5")
    assert (c.input_tokens, c.output_tokens, c.cache_read_tokens) == (7, 1, 4)
    assert c.cache_creation_tokens == 0


def test_call_cost_tolerates_missing_usage_entirely():
    c = call_cost_from_message(_Msg(total_cost_usd=0.5), "verify", "m")
    assert c.usd == 0.5
    assert (c.input_tokens, c.output_tokens) == (0, 0)


def test_meter_aggregates_by_stage_and_by_model():
    meter = CostMeter()
    meter.record(_Msg(total_cost_usd=0.10, num_turns=1,
                      usage={"input_tokens": 100, "output_tokens": 10}),
                 "synthesize", "claude-sonnet-5")
    meter.record(_Msg(total_cost_usd=0.05, num_turns=1,
                      usage={"input_tokens": 40, "output_tokens": 4}),
                 "verify", "claude-sonnet-5")
    meter.record(_Msg(total_cost_usd=0.01, num_turns=1,
                      usage={"input_tokens": 20, "output_tokens": 2}),
                 "cluster-label", "claude-haiku-4-5")

    assert len(meter.calls) == 3
    assert meter.total_usd == pytest.approx(0.16)

    by_stage = meter.by_stage()
    assert set(by_stage) == {"synthesize", "verify", "cluster-label"}
    assert by_stage["synthesize"] == {"usd": 0.10, "calls": 1,
                                      "input_tokens": 100,
                                      "output_tokens": 10,
                                      "cache_read_tokens": 0}

    by_model = meter.by_model()
    assert by_model["claude-sonnet-5"]["calls"] == 2
    assert by_model["claude-sonnet-5"]["usd"] == pytest.approx(0.15)
    assert by_model["claude-haiku-4-5"]["calls"] == 1


def test_summary_dict_has_stable_keys_and_flags_unbilled():
    meter = CostMeter()
    meter.record(_Msg(total_cost_usd=0.123456789, num_turns=1,
                      usage={"input_tokens": 10, "output_tokens": 1}),
                 "verify", "claude-sonnet-5")
    d = meter.summary_dict("synthesize")

    assert set(d) == {"command", "generated_at", "total_usd", "calls",
                      "failed_calls", "billed", "note", "by_stage",
                      "by_model"}
    assert d["command"] == "synthesize"
    assert d["total_usd"] == 0.1235  # rounded to 4dp
    assert d["calls"] == 1
    assert d["failed_calls"] == 0
    assert d["billed"] is False
    assert "subscription auth" in d["note"]
    # cli.py json.dumps() this straight to disk, so it must round-trip.
    assert json.loads(json.dumps(d))["total_usd"] == 0.1235


def test_format_summary_labels_cost_as_api_equivalent():
    meter = CostMeter()
    meter.record(_Msg(total_cost_usd=1.5, num_turns=1,
                      usage={"input_tokens": 900, "output_tokens": 90}),
                 "verify", "claude-sonnet-5")
    text = meter.format_summary("synthesize")
    assert "API-equivalent" in text
    assert "not billed" in text
    assert "$1.5000" in text
    assert "verify" in text
    assert "in 900" in text and "out 90" in text


def test_empty_meter_is_a_valid_zero_record():
    meter = CostMeter()
    assert meter.total_usd == 0.0
    assert meter.summary_dict("synthesize")["calls"] == 0
    assert meter.summary_dict("synthesize")["failed_calls"] == 0
    assert meter.summary_dict("synthesize")["by_stage"] == {}
    assert "$0.0000" in meter.format_summary("synthesize")


def test_format_summary_names_the_command():
    # The same meter API serves `synthesize` and `cluster`; a transcript
    # containing both must not carry two indistinguishable cost blocks.
    meter = CostMeter()
    assert "synthesize" in meter.format_summary("synthesize").splitlines()[0]
    assert "cluster" in meter.format_summary("cluster").splitlines()[0]


def test_format_summary_is_pure_ascii():
    # click.echo would raise UnicodeEncodeError under a cp1252 locale with
    # stdout redirected -- after claims are saved, before run_cost.json.
    meter = CostMeter()
    meter.record(_Msg(subtype="success", total_cost_usd=1.5, num_turns=1,
                      usage={"input_tokens": 9, "output_tokens": 1}),
                 "verify", "m")
    meter.format_summary("synthesize").encode("ascii")
    meter.summary_dict("synthesize")["note"].encode("ascii")


# --- terminal-subtype classification (the single source of truth) ---------

def test_success_subtype_is_terminal_and_not_an_error():
    msg = _Msg(subtype="success")
    assert is_success_result(msg) is True
    assert is_error_result(msg) is False
    assert is_terminal_result(msg) is True


@pytest.mark.parametrize("subtype",
                         ["error_max_turns", "error_during_execution"])
def test_error_subtypes_are_terminal(subtype):
    msg = _Msg(subtype=subtype)
    assert is_success_result(msg) is False
    assert is_error_result(msg) is True
    assert is_terminal_result(msg) is True


def test_non_terminal_message_is_neither():
    msg = _Msg(subtype="init")
    assert is_terminal_result(msg) is False
    assert is_error_result(msg) is False
    # a message with no subtype at all (e.g. AssistantMessage)
    assert is_terminal_result(_Msg()) is False


def test_errored_call_is_counted_and_flagged_failed():
    # error_max_turns / error_during_execution still carry the cost fields:
    # the quota was burned, so the call must not be invisible.
    msg = _Msg(subtype="error_max_turns", total_cost_usd=0.31, num_turns=4,
               usage={"input_tokens": 900, "output_tokens": 0})
    c = call_cost_from_message(msg, "verify", "claude-sonnet-5")
    assert c.failed is True
    assert c.usd == 0.31
    assert c.input_tokens == 900


def test_successful_call_is_not_flagged_failed():
    c = call_cost_from_message(_Msg(subtype="success", total_cost_usd=0.1),
                              "verify", "m")
    assert c.failed is False


def test_summary_counts_failed_calls_separately():
    meter = CostMeter()
    meter.record(_Msg(subtype="success", total_cost_usd=0.10, num_turns=1,
                      usage={"input_tokens": 100, "output_tokens": 10}),
                 "verify", "m")
    assert meter.failed_calls == 0
    assert "fail" not in meter.format_summary("synthesize")

    meter.record(_Msg(subtype="error_during_execution", total_cost_usd=0.05,
                      num_turns=4, usage={"input_tokens": 40}),
                 "verify", "m")
    assert meter.failed_calls == 1
    d = meter.summary_dict("synthesize")
    assert d["calls"] == 2
    assert d["failed_calls"] == 1
    assert d["total_usd"] == 0.15
    text = meter.format_summary("synthesize")
    assert "1 of 2 calls failed" in text


# --- billing route honesty ------------------------------------------------

def test_no_route_vars_means_subscription_wording(monkeypatch):
    for var in ROUTE_VARS:
        monkeypatch.delenv(var, raising=False)
    assert billing_route_overridden() is False
    meter = CostMeter()
    meter.record(_Msg(subtype="success", total_cost_usd=0.5, num_turns=1,
                      usage={"input_tokens": 1, "output_tokens": 1}),
                 "verify", "m")
    d = meter.summary_dict("synthesize")
    assert d["billed"] is False
    assert "subscription auth" in d["note"]
    assert "not billed" in d["note"]
    text = meter.format_summary("synthesize")
    assert "API-equivalent" in text
    assert "not billed" in text
    assert "unknown" not in text


@pytest.mark.parametrize("var", ROUTE_VARS)
def test_route_var_marks_billing_unknown(monkeypatch, var):
    # Bedrock/Vertex/base-URL routing is invisible to _cli_auth_env, which
    # strips only ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN. Asserting
    # "not billed" on such a run would be a lie in the dangerous direction.
    monkeypatch.setenv(var, "1")
    assert billing_route_overridden() is True
    meter = CostMeter()
    meter.record(_Msg(subtype="success", total_cost_usd=0.5, num_turns=1,
                      usage={"input_tokens": 1, "output_tokens": 1}),
                 "verify", "m")
    d = meter.summary_dict("synthesize")
    assert d["billed"] is None
    assert json.loads(json.dumps(d))["billed"] is None  # JSON null
    assert "unknown" in d["note"]
    assert "not billed" not in d["note"]
    # both surfaces must agree about billing
    text = meter.format_summary("synthesize")
    assert "unknown" in text
    assert "not billed" not in text


@pytest.mark.parametrize("var", ROUTE_VARS)
def test_empty_route_var_is_not_a_route(monkeypatch, var):
    monkeypatch.setenv(var, "")
    assert billing_route_overridden() is False
    assert CostMeter().summary_dict("synthesize")["billed"] is False
