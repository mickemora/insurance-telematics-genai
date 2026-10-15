"""Driver API Lambda with an in-memory table and fake Bedrock."""
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

API_DIR = str(Path(__file__).resolve().parent.parent / "lambdas" / "api")


class FakeTable:
    def __init__(self, items):
        self.items = {(i["driver_id"], i["sk"]): i for i in items}
        self.puts = []

    def query(self, KeyConditionExpression, ExpressionAttributeValues, **_):
        d = ExpressionAttributeValues[":d"]
        return {"Items": [v for (pk, _), v in sorted(self.items.items()) if pk == d]}

    def scan(self, FilterExpression, ExpressionAttributeValues, **_):
        wanted = set(ExpressionAttributeValues.values())
        return {"Items": [v for v in self.items.values() if v.get("entity") in wanted]}

    def put_item(self, Item):
        self.puts.append(Item)
        self.items[(Item["driver_id"], Item["sk"])] = Item


class FakeBedrock:
    def __init__(self):
        self.calls = 0

    def converse(self, **kw):
        self.calls += 1
        facts = json.loads(kw["messages"][0]["content"][0]["text"].split("\n", 1)[1])
        tips = [{"factor": f, "tip": "Leave a bit more space."} for f in facts["costly_factors"][:2]]
        if len(tips) < 2:
            tips = [{"factor": "general", "tip": "Keep it up."}] * 2
        return {"output": {"message": {"content": [{"toolUse": {"name": "submit_explanation", "input": {
            "headline": f"Score {facts['week']['score']}", "summary": "Steady week overall.", "tips": tips}}}]}},
            "usage": {"inputTokens": 1, "outputTokens": 1}}

    def apply_guardrail(self, **kw):
        return {"action": "NONE"}


@pytest.fixture()
def api(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "tbl")
    monkeypatch.setenv("MODEL_ID", "us.anthropic.claude-haiku-5-5")
    monkeypatch.setenv("GUARDRAIL_ID", "gr")
    monkeypatch.setenv("GUARDRAIL_VERSION", "1")
    sys.modules.pop("handler", None)
    sys.path.insert(0, API_DIR)
    mod = importlib.import_module("handler")

    data = generate(drivers=4, days=14, seed=5, crashes=0, false_alarms=0, end_date=date(2026, 10, 10))
    items = [{**p, "sk": "PROFILE", "entity": "profile"} for p in data["profiles"]]
    weeks = defaultdict(lambda: {f: 0 for f in TOTAL_FIELDS})
    for t in data["trips"]:
        s = score_trip(t)
        items.append({**s, "sk": f"TRIP#{s['start_time']}#{s['trip_id']}", "entity": "trip"})
        w = weeks[(t["driver_id"], s["week_id"])]
        for f in TOTAL_FIELDS:
            w[f] += s["totals"][f]
    for (d, wk), w in weeks.items():
        items.append({**w, "driver_id": d, "sk": f"WEEK#{wk}", "entity": "week", "week_id": wk})
    mod.table = FakeTable(items)
    mod.bedrock = FakeBedrock()
    yield mod
    sys.modules.pop("handler", None)
    sys.path.remove(API_DIR)


def _get(mod, resource, driver_id=None, query=None):
    event = {"httpMethod": "GET", "resource": resource,
             "pathParameters": {"driver_id": driver_id} if driver_id else None,
             "queryStringParameters": query}
    resp = mod.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_list_drivers(api):
    status, body = _get(api, "/drivers")
    assert status == 200 and body["count"] == 4
    assert body["drivers"][0]["latest_week"]["score"] > 0


def test_score(api):
    status, body = _get(api, "/drivers/{driver_id}/score", "DRV-0002")
    assert status == 200 and len(body["recent_trips"]) == 10 and body["weeks"]


def test_unknown_driver(api):
    assert _get(api, "/drivers/{driver_id}/score", "DRV-9999")[0] == 404
    assert _get(api, "/drivers/{driver_id}/explanation", "DRV-9999")[0] == 404


def test_explanation_is_cached_until_refresh(api):
    status, first = _get(api, "/drivers/{driver_id}/explanation", "DRV-0002")
    assert status == 200 and first["source"] == "bedrock" and first["cached"] is False
    assert first["facts"]["week"]["id"] == first["week_id"]
    _, second = _get(api, "/drivers/{driver_id}/explanation", "DRV-0002")
    assert second["cached"] is True and api.bedrock.calls == 1
    _, third = _get(api, "/drivers/{driver_id}/explanation", "DRV-0002", {"refresh": "true"})
    assert third["cached"] is False and api.bedrock.calls == 2
