"""Day 4: Bedrock Agent action group tools, the copilot worker's trace parsing, and copilot API routes."""
import importlib
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("boto3")

from score import TOTAL_FIELDS, score_trip  # noqa: E402
from synth import generate  # noqa: E402
from test_claims import FakeTable  # noqa: E402

API_DIR = Path(__file__).resolve().parent.parent / "lambdas" / "api"


class Table(FakeTable):
    def scan(self, FilterExpression, ExpressionAttributeValues, **_):
        wanted = set(ExpressionAttributeValues.values())
        return {"Items": [v for v in self.items.values() if v.get("entity") in wanted]}

    def query(self, KeyConditionExpression, ExpressionAttributeValues, **_):
        pk = list(ExpressionAttributeValues.values())[0]
        return {"Items": [v for (p, _), v in sorted(self.items.items()) if p == pk]}


class FakeLambda:
    def __init__(self):
        self.invocations = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.invocations.append((FunctionName, InvocationType, json.loads(Payload)))


@pytest.fixture()
def mods(monkeypatch):
    for k, v in {"TABLE_NAME": "t", "MODEL_ID": "m", "AGENT_ID": "AGENT1", "COPILOT_WORKER_FUNCTION": "worker"}.items():
        monkeypatch.setenv(k, v)
    for name in ("handler", "agent_tools", "copilot_worker"):
        sys.modules.pop(name, None)
    sys.path.insert(0, str(API_DIR))
    api = importlib.import_module("handler")
    tools = importlib.import_module("agent_tools")
    worker = importlib.import_module("copilot_worker")
    sys.path.remove(str(API_DIR))

    table = Table()
    data = generate(drivers=3, days=14, seed=9, crashes=0, false_alarms=0, end_date=date(2026, 10, 10))
    for p in data["profiles"]:
        table.put_item({**p, "sk": "PROFILE", "entity": "profile"})
    weeks = defaultdict(lambda: {f: 0 for f in TOTAL_FIELDS})
    for t in data["trips"]:
        s = score_trip(t)
        table.put_item({**s, "sk": f"TRIP#{s['start_time']}#{s['trip_id']}", "entity": "trip"})
        w = weeks[(t["driver_id"], s["week_id"])]
        for f in TOTAL_FIELDS:
            w[f] += s["totals"][f]
    for (d, wk), w in weeks.items():
        table.put_item({**w, "driver_id": d, "sk": f"WEEK#{wk}", "entity": "week", "week_id": wk})
    api.table = worker.table = table
    api.lambda_client = FakeLambda()
    yield api, tools, worker, table
    for name in ("handler", "agent_tools", "copilot_worker"):
        sys.modules.pop(name, None)


def _agent_event(function, **params):
    return {"messageVersion": "1.0", "actionGroup": "TelematicsData", "function": function,
            "parameters": [{"name": k, "type": "string", "value": v} for k, v in params.items()],
            "sessionAttributes": {"a": "1"}, "promptSessionAttributes": {}}


def _tool_body(resp):
    assert resp["messageVersion"] == "1.0"
    assert resp["response"]["actionGroup"] == "TelematicsData"
    return json.loads(resp["response"]["functionResponse"]["responseBody"]["TEXT"]["body"])


def test_list_drivers_tool(mods):
    _, tools, _, _ = mods
    resp = tools.handler(_agent_event("list_drivers"), None)
    body = _tool_body(resp)
    assert len(body["drivers"]) == 3 and body["drivers"][0]["driver_id"] == "DRV-0001"
    assert resp["sessionAttributes"] == {"a": "1"}


def test_trips_tool_respects_limit(mods):
    _, tools, _, _ = mods
    body = _tool_body(tools.handler(_agent_event("get_driver_trips", driver_id="DRV-0002", limit="4"), None))
    assert body["trip_count"] == 4
    assert body["trips"][0]["local_start"] > body["trips"][-1]["local_start"]  # newest first
    big = _tool_body(tools.handler(_agent_event("get_driver_trips", driver_id="DRV-0002", limit="500"), None))
    assert big["trip_count"] <= 25


def test_score_tool_and_unknown_driver(mods):
    _, tools, _, _ = mods
    body = _tool_body(tools.handler(_agent_event("get_driver_score", driver_id="DRV-0003"), None))
    assert body["weeks"] and "points_lost" in body["weeks"][0]
    assert "error" in _tool_body(tools.handler(_agent_event("get_driver_score", driver_id="DRV-9999"), None))


def test_unknown_function_reprompts(mods):
    _, tools, _, _ = mods
    resp = tools.handler(_agent_event("delete_everything"), None)
    assert resp["response"]["functionResponse"]["responseState"] == "REPROMPT"


class FakeAgentRuntime:
    def invoke_agent(self, **kw):
        self.kw = kw

        def stream():
            yield {"trace": {"trace": {"orchestrationTrace": {"rationale": {"text": "Find the driver first."}}}}}
            yield {"trace": {"trace": {"orchestrationTrace": {"invocationInput": {"actionGroupInvocationInput": {
                "actionGroupName": "TelematicsData", "function": "get_driver_trips",
                "parameters": [{"name": "driver_id", "type": "string", "value": "DRV-0020"}]}}}}}}
            yield {"trace": {"trace": {"orchestrationTrace": {"observation": {
                "actionGroupInvocationOutput": {"text": '{"trips": []}'}}}}}}
            yield {"trace": {"trace": {"orchestrationTrace": {"modelInvocationOutput": {
                "metadata": {"usage": {"inputTokens": 1000, "outputTokens": 50}}}}}}}
            yield {"chunk": {"bytes": "Victor had ".encode()}}
            yield {"chunk": {"bytes": "a rough week.".encode()}}
        return {"completion": stream()}


def test_worker_parses_answer_and_tool_calls(mods):
    _, _, worker, _ = mods
    client = FakeAgentRuntime()
    out = worker.run_agent("How is DRV-0020 doing?", "s-1", client=client)
    assert out["answer"] == "Victor had a rough week."
    assert out["tool_calls"] == [{"function": "get_driver_trips", "parameters": {"driver_id": "DRV-0020"},
                                  "why": "Find the driver first.", "result_chars": 13, "result_preview": '{"trips": []}'}]
    assert out["usage"] == {"inputTokens": 1000, "outputTokens": 50}
    assert client.kw["agentId"] == "AGENT1" and client.kw["agentAliasId"] == "TSTALIASID"
    assert client.kw["enableTrace"] is True


def _api(api, method, resource, body=None, job_id=None):
    resp = api.handler({"httpMethod": method, "resource": resource,
                        "pathParameters": {"job_id": job_id} if job_id else None,
                        "queryStringParameters": None, "body": json.dumps(body) if body else None}, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_copilot_job_lifecycle(mods, monkeypatch):
    api, _, worker, table = mods
    status, body = _api(api, "POST", "/copilot", {"question": "How is DRV-0020 doing?", "session_id": "s-abc"})
    assert status == 202 and body["session_id"] == "s-abc"
    job_id = body["job_id"]
    fn, kind, payload = api.lambda_client.invocations[0]
    assert (fn, kind, payload) == ("worker", "Event", {"job_id": job_id})
    assert _api(api, "GET", "/copilot/{job_id}", job_id=job_id)[1]["status"] == "queued"

    monkeypatch.setattr(worker, "agents", FakeAgentRuntime())
    worker.handler({"job_id": job_id}, None)
    status, done = _api(api, "GET", "/copilot/{job_id}", job_id=job_id)
    assert status == 200 and done["status"] == "done"
    assert done["answer"] == "Victor had a rough week." and done["tool_calls"][0]["function"] == "get_driver_trips"


def test_copilot_validation(mods):
    api, _, _, _ = mods
    assert _api(api, "POST", "/copilot", {"question": ""})[0] == 400
    assert _api(api, "POST", "/copilot", {"question": "x" * 1001})[0] == 400
    assert _api(api, "POST", "/copilot", {"question": "hi", "session_id": "bad id!"})[0] == 400
    assert _api(api, "GET", "/copilot/{job_id}", job_id="nope")[0] == 404
