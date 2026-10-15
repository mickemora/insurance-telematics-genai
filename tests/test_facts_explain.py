"""Facts engine and explainer (fake Bedrock, no AWS calls)."""
import copy
from collections import defaultdict
from datetime import date

import pytest

from explain import ValidationError, explain, fallback_explanation, validate
from facts import (allowed_numbers, build_facts, number_is_grounded, select_weeks, week_dates)
from score import TOTAL_FIELDS, score_trip
from synth import generate

MODEL = "us.anthropic.claude-haiku-5-5"


@pytest.fixture(scope="module")
def dataset():
    data = generate(drivers=12, days=21, seed=11, end_date=date(2026, 10, 10))
    weeks = defaultdict(lambda: defaultdict(lambda: {f: 0 for f in TOTAL_FIELDS}))
    trips = defaultdict(list)
    for t in data["trips"]:
        s = score_trip(t)
        w = weeks[t["driver_id"]][s["week_id"]]
        w["week_id"] = s["week_id"]
        for f in TOTAL_FIELDS:
            w[f] += s["totals"][f]
        trips[t["driver_id"]].append({**s, "entity": "trip"})
    facts = {p["driver_id"]: build_facts(p, list(weeks[p["driver_id"]].values()), trips[p["driver_id"]])
             for p in data["profiles"]}
    return data, facts


def _good_explanation(facts):
    top = facts["factors"][0]
    return {
        "headline": f"Your score is {facts['week']['score']:.0f} this week",
        "summary": f"Your score {facts['direction']} this week. {top['label'].capitalize()} cost you "
                   f"{top['points_lost']} points.",
        "tips": [{"factor": f, "tip": "Ease off a little earlier and keep about 3 seconds of space."}
                 for f in facts["costly_factors"][:2]] or
                [{"factor": "general", "tip": "Keep it up."}, {"factor": "general", "tip": "Stay smooth."}],
    }


class FakeBedrock:
    def __init__(self, tool_input=None, guardrail_action="NONE", fail=None):
        self.tool_input, self.guardrail_action, self.fail = tool_input, guardrail_action, fail
        self.converse_calls, self.guardrail_calls = [], []

    def converse(self, **kw):
        self.converse_calls.append(kw)
        if self.fail:
            raise self.fail
        return {"output": {"message": {"content": [
            {"toolUse": {"toolUseId": "t1", "name": "submit_explanation", "input": self.tool_input}}]}},
            "stopReason": "tool_use", "usage": {"inputTokens": 900, "outputTokens": 180}}

    def apply_guardrail(self, **kw):
        self.guardrail_calls.append(kw)
        return {"action": self.guardrail_action}


# --------------------------------------------------------------------------- facts

def test_week_dates():
    assert week_dates("2026-W41") == "Oct 5-11"
    assert week_dates("2026-W40") == "Sep 28-Oct 4"


def test_select_weeks_skips_in_progress_week():
    weeks = [{"week_id": "2026-W40", "trips": 12}, {"week_id": "2026-W41", "trips": 14},
             {"week_id": "2026-W42", "trips": 2}]
    cur, prev = select_weeks(weeks)
    assert (cur["week_id"], prev["week_id"]) == ("2026-W41", "2026-W40")


def test_facts_shape(dataset):
    _, facts = dataset
    f = facts["DRV-0001"]
    assert f["week"]["score"] > 0 and f["previous_week"] is not None
    assert f["direction"] in ("up", "down", "held steady")
    assert [x["points_lost"] for x in f["factors"]] == sorted((x["points_lost"] for x in f["factors"]), reverse=True)
    assert set(f["costly_factors"]) <= {x["factor"] for x in f["factors"]}


def test_facts_no_previous_week():
    profile = {"driver_id": "D", "first_name": "A", "last_initial": "B"}
    week = {"week_id": "2026-W41", "trips": 6, "miles": 100, "drive_seconds": 7200, "moving_seconds": 7000,
            **{k: 0 for k in TOTAL_FIELDS if k not in ("trips", "miles", "drive_seconds", "moving_seconds")}}
    f = build_facts(profile, [week], [])
    assert f["direction"] == "first week" and f["score_change"] is None
    assert f["costly_factors"] == [] and f["week"]["score"] == 100


def test_rusher_top_factor_is_driving_style(dataset):
    data, facts = dataset
    for t in data["manifest"]["driver_truth"]:
        if t["persona"] == "rusher":
            assert facts[t["driver_id"]]["costly_factors"][0] in ("hard_braking", "rapid_acceleration", "speeding")
        if t["persona"] == "distracted":
            assert "phone_handling" in facts[t["driver_id"]]["costly_factors"][:2]


def test_grounding_allows_rounding_only():
    allowed = allowed_numbers({"a": 46.7, "b": "Oct 5-11"})
    assert number_is_grounded(47, allowed) and number_is_grounded(46.7, allowed)
    assert number_is_grounded(11, allowed)
    assert not number_is_grounded(52, allowed)


# --------------------------------------------------------------------------- validation

def test_fallback_always_validates(dataset):
    _, facts = dataset
    for f in facts.values():
        validate(fallback_explanation(f), f)


def test_validate_rejects_invented_number(dataset):
    f = dataset[1]["DRV-0002"]
    bad = _good_explanation(f)
    bad["summary"] += " You braked hard 987 times."
    with pytest.raises(ValidationError, match="ungrounded number 987"):
        validate(bad, f)


def test_validate_rejects_pricing_language(dataset):
    f = dataset[1]["DRV-0002"]
    bad = _good_explanation(f)
    bad["tips"][0]["tip"] = "Smooth braking could lower your premium."
    with pytest.raises(ValidationError, match="banned phrase"):
        validate(bad, f)


def test_validate_rejects_tip_on_factor_that_cost_nothing(dataset):
    f = copy.deepcopy(dataset[1]["DRV-0002"])
    f["costly_factors"] = ["hard_braking"]
    bad = _good_explanation(f)
    bad["tips"] = [{"factor": "hard_braking", "tip": "x"}, {"factor": "phone_handling", "tip": "y"}]
    with pytest.raises(ValidationError, match="did not cost points"):
        validate(bad, f)


# --------------------------------------------------------------------------- explain()

def test_explain_happy_path(dataset):
    f = dataset[1]["DRV-0002"]
    fake = FakeBedrock(tool_input=_good_explanation(f))
    out = explain(f, fake, MODEL, "gr-123", "1")
    assert out["source"] == "bedrock" and out["guardrail"] == "passed"
    assert out["usage"] == {"inputTokens": 900, "outputTokens": 180}
    call = fake.converse_calls[0]
    assert call["modelId"] == MODEL
    assert call["toolConfig"]["toolChoice"] == {"tool": {"name": "submit_explanation"}}
    assert fake.guardrail_calls[0]["source"] == "OUTPUT"


def test_explain_falls_back_when_guardrail_intervenes(dataset):
    f = dataset[1]["DRV-0002"]
    out = explain(f, FakeBedrock(_good_explanation(f), guardrail_action="GUARDRAIL_INTERVENED"), MODEL, "gr", "1")
    assert out["source"] == "fallback" and out["guardrail"] == "intervened"
    assert out["headline"] == fallback_explanation(f)["headline"]


def test_explain_falls_back_on_bedrock_error(dataset):
    f = dataset[1]["DRV-0002"]
    out = explain(f, FakeBedrock(fail=RuntimeError("AccessDeniedException")), MODEL)
    assert out["source"] == "fallback" and "AccessDeniedException" in out["fallback_reason"]


def test_explain_falls_back_on_ungrounded_output(dataset):
    f = dataset[1]["DRV-0002"]
    bad = _good_explanation(f)
    bad["headline"] = "You lost 999 points"
    out = explain(f, FakeBedrock(bad), MODEL, "gr", "1")
    assert out["source"] == "fallback" and "999" in out["fallback_reason"]
