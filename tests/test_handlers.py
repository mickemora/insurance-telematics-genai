"""Lambda handler tests with in-memory fakes (no AWS calls)."""
import importlib
import io
import json
import sys
from datetime import date

import pytest

pytest.importorskip("boto3")
from botocore.exceptions import ClientError  # noqa: E402

from synth import generate  # noqa: E402


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


class FakeDynamo:
    def __init__(self):
        self.items = {}
        self.week_adds = []

    def transact_write_items(self, TransactItems):
        put = TransactItems[0]["Put"]
        key = (put["Item"]["driver_id"]["S"], put["Item"]["sk"]["S"])
        if key in self.items:
            raise ClientError(
                {"Error": {"Code": "TransactionCanceledException", "Message": "x"},
                 "CancellationReasons": [{"Code": "ConditionalCheckFailed"}, {"Code": "None"}]},
                "TransactWriteItems",
            )
        self.items[key] = put["Item"]
        self.week_adds.append(TransactItems[1]["Update"])


class FakeEvents:
    def __init__(self):
        self.entries = []

    def put_events(self, Entries):
        self.entries += Entries
        return {"FailedEntryCount": 0}


@pytest.fixture()
def scoring(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "tbl")
    sys.modules.pop("handler", None)
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "lambdas" / "scoring"))
    mod = importlib.import_module("handler")
    data = generate(drivers=6, days=7, seed=3, crashes=1, false_alarms=1, end_date=date(2026, 10, 10))
    objects = {f"raw/trips/{t['trip_id']}.json": json.dumps(t).encode() for t in data["trips"]}
    mod.s3 = FakeS3(objects)
    mod.ddb = FakeDynamo()
    mod.events = FakeEvents()
    yield mod, data, objects
    sys.modules.pop("handler", None)
    sys.path.pop(0)


def _sqs_event(keys):
    return {"Records": [
        {"messageId": f"m{i}", "body": json.dumps(
            {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": k}}}]})}
        for i, k in enumerate(keys)
    ]}


def test_scores_every_trip_and_publishes_crashes(scoring):
    mod, data, objects = scoring
    resp = mod.handler(_sqs_event(list(objects)), None)
    assert resp == {"batchItemFailures": []}
    assert len(mod.ddb.items) == len(data["trips"])
    assert len(mod.events.entries) == 2  # one real crash + one false alarm
    detail = json.loads(mod.events.entries[0]["Detail"])
    assert detail["crash"]["impact_g"] > 0 and "high_res" not in detail["crash"]


def test_duplicate_delivery_is_idempotent(scoring):
    mod, data, objects = scoring
    keys = list(objects)[:5]
    mod.handler(_sqs_event(keys), None)
    adds_before = len(mod.ddb.week_adds)
    resp = mod.handler(_sqs_event(keys), None)
    assert resp == {"batchItemFailures": []}
    assert len(mod.ddb.week_adds) == adds_before


def test_s3_test_event_ignored_and_bad_message_reported(scoring):
    mod, _, _ = scoring
    event = {"Records": [
        {"messageId": "t", "body": json.dumps({"Event": "s3:TestEvent"})},
        {"messageId": "bad", "body": json.dumps(
            {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": "missing.json"}}}]})},
    ]}
    assert mod.handler(event, None) == {"batchItemFailures": [{"itemIdentifier": "bad"}]}
