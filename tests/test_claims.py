"""Day 3: crash facts, triage, statement checks, FNOL drafting, workflow handlers, claims API."""
import importlib
import io
import json
import sys
from datetime import date
from pathlib import Path

import pytest

from crash import build_crash_facts, check_statement, find_crash_event, simulate_statement, triage
from synth import generate

ROOT = Path(__file__).resolve().parent.parent
MODEL = "us.anthropic.claude-haiku-5-5"


@pytest.fixture(scope="module")
def planted():
    data = generate(drivers=20, days=21, seed=42, crashes=4, false_alarms=3, end_date=date(2026, 10, 10))
    trips = {t["trip_id"]: t for t in data["trips"]}
    profiles = {p["driver_id"]: p for p in data["profiles"]}
    out = []
    for p in data["manifest"]["planted_events"]:
        trip = trips[p["trip_id"]]
        ev = next(e for e in trip["events"] if e["type"] == "possible_crash")
        crash = find_crash_event(trip, ev["t"])
        facts = build_crash_facts(trip, crash, profiles[trip["driver_id"]], "100 Main St, Plano, TX 75074")
        out.append({"kind": p["kind"], "scenario": p["scenario"], "trip": trip, "facts": facts,
                    "profile": profiles[trip["driver_id"]], "crash": crash})
    return data, out


# --------------------------------------------------------------------------- facts + triage

def test_triage_matches_ground_truth(planted):
    _, events = planted
    for e in events:
        label, reasons = triage(e["facts"])
        assert label == {"crash": "likely_crash", "false_alarm": "likely_false_alarm"}[e["kind"]], e["scenario"]
        assert reasons


def test_crash_facts_shape(planted):
    _, events = planted
    f = next(e for e in events if e["kind"] == "crash")["facts"]
    assert f["after_impact"]["speed_mph"] == 0 and f["after_impact"]["stationary_seconds"] > 0
    assert f["after_impact"]["trip_ended_at_crash_site"] is True
    assert f["location"]["address"].startswith("100 Main")
    assert f["timeline"][0]["what"] == "Trip started" and f["timeline"][-1]["what"] == "Trip ended"
    assert "score" not in json.dumps(f)  # the driving score must not leak into a claim


def test_rear_end_struck_has_no_braking(planted):
    _, events = planted
    e = next(e for e in events if e["scenario"] == "rear_end_struck")
    assert e["facts"]["impact"]["direction"] == "rear"
    assert e["facts"]["impact"]["braking_before_impact"] is False


# --------------------------------------------------------------------------- statement checks

def test_truthful_statements_are_consistent(planted):
    _, events = planted
    for e in (x for x in events if x["kind"] == "crash"):
        checks = check_statement(simulate_statement(e["facts"], True), e["facts"])
        assert checks and all(c["consistent"] for c in checks), (e["scenario"], checks)


def test_inconsistent_statements_are_flagged(planted):
    _, events = planted
    for e in (x for x in events if x["kind"] == "crash"):
        checks = check_statement(simulate_statement(e["facts"], False), e["facts"])
        assert any(not c["consistent"] for c in checks), (e["scenario"], checks)


def test_statement_check_details():
    facts = {"impact": {"speed_5s_before_mph": 30, "speed_at_impact_mph": 14, "direction": "front",
                        "direction_label": "the front", "braking_before_impact": True}}
    checks = {c["aspect"]: c for c in check_statement(
        "I didn't have time to brake and hit the car in front of me at 30 mph", facts)}
    assert checks["speed"]["consistent"] and checks["impact direction"]["consistent"]
    assert checks["braking"]["consistent"] is False
    assert check_statement(None, facts) == [] and check_statement("It was raining.", facts) == []


# --------------------------------------------------------------------------- drafting

class FakeBedrock:
    def __init__(self, draft=None, guardrail="NONE", fail=None):
        self.draft, self.guardrail, self.fail, self.calls = draft, guardrail, fail, 0

    def converse(self, **kw):
        self.calls += 1
        if self.fail:
            raise self.fail
        return {"output": {"message": {"content": [{"toolUse": {"name": "submit_claim_draft", "input": self.draft}}]}},
                "usage": {"inputTokens": 1500, "outputTokens": 300}}

    def apply_guardrail(self, **kw):
        return {"action": self.guardrail}


def _inputs(e, truthful=True, statement=True):
    from crash import check_statement as cs
    text = simulate_statement(e["facts"], truthful) if statement else None
    return {"crash_facts": e["facts"], "member_statement": text, "statement_checks": cs(text, e["facts"])}


def _draft(e, verdict):
    i = e["facts"]["impact"]
    return {
        "incident_summary": f"The data shows a {i['force_g']} g impact to {i['direction_label']} at "
                            f"{i['speed_at_impact_mph']} mph.",
        "statement_assessment": {"verdict": verdict, "explanation": "Compared with the telematics data."},
        "adjuster_questions": ["Were other vehicles involved?", "Was anyone injured?"],
        "next_steps": ["Confirm the member is safe.", "Request photos of the vehicles."],
    }


def test_fallback_drafts_always_validate(planted):
    from draft import fallback_draft, validate_draft
    _, events = planted
    for e in events:
        for truthful in (True, False):
            for statement in (True, False):
                inputs = _inputs(e, truthful, statement)
                validate_draft(fallback_draft(inputs), inputs)


def test_draft_happy_path(planted):
    from draft import draft_claim
    e = next(x for x in planted[1] if x["kind"] == "crash")
    out = draft_claim(_inputs(e, True), FakeBedrock(_draft(e, "consistent")), MODEL, "gr", "1")
    assert out["source"] == "bedrock" and out["guardrail"] == "passed"
    assert out["statement_assessment"]["verdict"] == "consistent"


def test_draft_cannot_call_inconsistent_statement_consistent(planted):
    from draft import draft_claim
    e = next(x for x in planted[1] if x["kind"] == "crash")
    out = draft_claim(_inputs(e, False), FakeBedrock(_draft(e, "consistent")), MODEL, "gr", "1")
    assert out["source"] == "fallback" and "contradicts" in out["fallback_reason"]
    assert out["statement_assessment"]["verdict"] in ("inconsistent", "partially_consistent")


def test_draft_rejects_fault_language_and_missing_statement_rules(planted):
    from draft import draft_claim
    e = next(x for x in planted[1] if x["kind"] == "crash")
    bad = _draft(e, "consistent")
    bad["incident_summary"] += " The other driver was at fault."
    assert draft_claim(_inputs(e, True), FakeBedrock(bad), MODEL)["source"] == "fallback"
    out = draft_claim(_inputs(e, statement=False), FakeBedrock(_draft(e, "consistent")), MODEL)
    assert out["source"] == "fallback" and out["statement_assessment"]["verdict"] == "no_statement"


def test_draft_guardrail_and_errors_fall_back(planted):
    from draft import draft_claim
    e = next(x for x in planted[1] if x["kind"] == "crash")
    assert draft_claim(_inputs(e), FakeBedrock(_draft(e, "consistent"), guardrail="GUARDRAIL_INTERVENED"),
                       MODEL, "gr", "1")["guardrail"] == "intervened"
    out = draft_claim(_inputs(e), FakeBedrock(fail=RuntimeError("ThrottlingException")), MODEL)
    assert out["source"] == "fallback" and "ThrottlingException" in out["fallback_reason"]


# --------------------------------------------------------------------------- workflow handlers + API

pytest.importorskip("boto3")
from botocore.exceptions import ClientError  # noqa: E402


class FakeTable:
    """Just enough DynamoDB for the claims code paths."""

    def __init__(self):
        self.items = {}

    def _k(self, key):
        return (key["driver_id"], key["sk"])

    def put_item(self, Item, ConditionExpression=None):
        k = self._k(Item)
        if ConditionExpression and k in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "PutItem")
        self.items[k] = json.loads(json.dumps(Item, default=float))

    def get_item(self, Key):
        item = self.items.get(self._k(Key))
        return {"Item": json.loads(json.dumps(item))} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ExpressionAttributeNames=None,
                    ConditionExpression=None):
        names = ExpressionAttributeNames or {}
        item = self.items[self._k(Key)]
        if ConditionExpression:
            lhs, rhs = [x.strip() for x in ConditionExpression.split("=")]
            if item.get(names.get(lhs, lhs)) != ExpressionAttributeValues[rhs]:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "Update")
        set_part, _, remove_part = UpdateExpression.partition("REMOVE")
        for assign in set_part.replace("SET", "", 1).split(","):
            if "=" in assign:
                attr, val = [x.strip() for x in assign.split("=")]
                item[names.get(attr, attr)] = json.loads(json.dumps(ExpressionAttributeValues[val], default=float))
        for attr in filter(None, (a.strip() for a in remove_part.split(","))):
            item.pop(attr, None)

    def query(self, KeyConditionExpression, ExpressionAttributeValues, **_):
        pk = list(ExpressionAttributeValues.values())[0]
        return {"Items": [v for (p, _), v in sorted(self.items.items()) if p == pk]}


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}


class FakeSfn:
    def __init__(self):
        self.success = []

    def send_task_success(self, taskToken, output):
        self.success.append(taskToken)


def _load(module, directory, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop(module, None)
    sys.path.insert(0, str(directory))
    try:
        return importlib.import_module(module)
    finally:
        sys.path.remove(str(directory))


@pytest.fixture()
def workflow(planted, monkeypatch):
    env = {"TABLE_NAME": "tbl", "RAW_BUCKET": "raw", "MODEL_ID": MODEL, "GUARDRAIL_ID": "gr", "GUARDRAIL_VERSION": "1"}
    handlers = _load("handlers", ROOT / "lambdas" / "claims", monkeypatch, env)
    api = _load("handler", ROOT / "lambdas" / "api", monkeypatch, env)
    table = FakeTable()
    _, events = planted
    for e in events:
        table.put_item({**e["profile"], "sk": "PROFILE"})
    handlers.table = api.table = table
    handlers._clients.update({
        "s3": FakeS3({f"raw/{e['trip']['trip_id']}.json": e["trip"] for e in events}),
        "bedrock-runtime": FakeBedrock(fail=RuntimeError("offline")),
        "geo-places": None,  # reverse geocode fails gracefully
    })
    api.sfn = FakeSfn()
    yield handlers, api, table, events
    sys.modules.pop("handlers", None)
    sys.modules.pop("handler", None)


def _detail(e):
    crash = {k: v for k, v in e["crash"].items() if k != "high_res"}
    return {"detail": {"driver_id": e["trip"]["driver_id"], "trip_id": e["trip"]["trip_id"],
                       "raw_s3_key": f"raw/{e['trip']['trip_id']}.json", "crash": crash},
            "execution_id": "arn:exec:1"}


def _api(api, method, resource, claim_id=None, body=None):
    resp = api.handler({"httpMethod": method, "resource": resource,
                        "pathParameters": {"claim_id": claim_id} if claim_id else None,
                        "queryStringParameters": None, "body": json.dumps(body) if body else None}, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_full_claim_flow(workflow):
    handlers, api, table, events = workflow
    crash = next(e for e in events if e["kind"] == "crash")
    out = handlers.enrich(_detail(crash), None)
    assert out["triage"] == "likely_crash" and out["duplicate"] is False
    assert handlers.enrich(_detail(crash), None)["duplicate"] is True  # idempotent

    cid = out["claim_id"]
    handlers.checkin({"task_token": "tok-1", "claim_id": cid, "execution_id": "arn:exec:1"}, None)
    status, claim = _api(api, "GET", "/claims/{claim_id}", cid)
    assert status == 200 and claim["status"] == "awaiting_statement" and "task_token" not in claim

    statement = simulate_statement(claim["crash_facts"], truthful=False)
    status, body = _api(api, "POST", "/claims/{claim_id}/statement", cid, {"statement": statement})
    assert status == 202 and api.sfn.success == ["tok-1"]
    assert _api(api, "POST", "/claims/{claim_id}/statement", cid, {"statement": "again"})[0] == 409

    stale = handlers.draft({"claim_id": cid, "execution_id": "arn:exec:OLD"}, None)
    assert stale["status"] == "claim_missing_or_superseded"  # an older run must not touch the claim
    result = handlers.draft({"claim_id": cid, "execution_id": "arn:exec:1"}, None)
    assert result["status"] == "draft_ready_for_adjuster"
    _, claim = _api(api, "GET", "/claims/{claim_id}", cid)
    assert claim["draft"]["source"] == "fallback"  # Bedrock is "offline" in this test
    assert claim["draft"]["statement_assessment"]["verdict"] in ("inconsistent", "partially_consistent")
    assert any(not c["consistent"] for c in claim["statement_checks"])

    status, listing = _api(api, "GET", "/claims")
    assert status == 200 and listing["claims"][0]["claim_id"] == cid


def test_false_alarm_closed(workflow):
    handlers, api, table, events = workflow
    alarm = next(e for e in events if e["kind"] == "false_alarm")
    out = handlers.enrich(_detail(alarm), None)
    assert out["triage"] == "likely_false_alarm"
    handlers.close_false_alarm({"claim_id": out["claim_id"]}, None)
    _, claim = _api(api, "GET", "/claims/{claim_id}", out["claim_id"])
    assert claim["status"] == "closed_false_alarm" and "jolt" in claim["member_message"]


def test_statement_validation(workflow):
    _, api, _, _ = workflow
    assert _api(api, "POST", "/claims/{claim_id}/statement", "CLM-NOPE", {"statement": "hi"})[0] == 404
    assert _api(api, "POST", "/claims/{claim_id}/statement", "CLM-NOPE", {"statement": ""})[0] == 400
    assert _api(api, "POST", "/claims/{claim_id}/statement", "CLM-NOPE", {"statement": "x" * 1001})[0] == 400



def test_claim_id_is_stable(workflow):
    handlers = workflow[0]
    assert handlers.claim_id_for("1a2b3c4d-5e6f-4000-8000-000000000000") == "CLM-1A2B3C4D"
