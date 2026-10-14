"""Scoring Lambda: S3 (raw trip) -> SQS -> this function -> DynamoDB.

For every new raw trip it:
1. scores the trip and stores a TRIP# item with the penalty breakdown;
2. adds the trip's counters to the driver's WEEK# item (atomic ADD);
3. publishes a PossibleCrashDetected event to EventBridge if the trip has one.

Steps 1 and 2 run in one DynamoDB transaction guarded by
attribute_not_exists, so a redelivered SQS message never double counts.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import unquote_plus

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from score import TOTAL_FIELDS, score_trip

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE = os.environ["TABLE_NAME"]
EVENT_BUS = os.environ.get("EVENT_BUS_NAME", "default")
EVENT_SOURCE = os.environ.get("EVENT_SOURCE", "telematics.scoring")

s3 = boto3.client("s3")
ddb = boto3.client("dynamodb")
events = boto3.client("events")
_ser = TypeSerializer()


def _av(value) -> dict:
    return _ser.serialize(json.loads(json.dumps(value), parse_float=Decimal))


def _write(scored: dict, raw_key: str) -> bool:
    """Returns False if this trip was already scored (duplicate delivery)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    trip_item = {
        "driver_id": scored["driver_id"],
        "sk": f"TRIP#{scored['start_time']}#{scored['trip_id']}",
        "entity": "trip",
        "raw_s3_key": raw_key,
        "scored_at": now,
        **{k: v for k, v in scored.items() if k != "driver_id"},
    }
    totals = scored["totals"]
    add_expr = ", ".join(f"{f} :{f}" for f in TOTAL_FIELDS)
    values = {f":{f}": _av(totals[f]) for f in TOTAL_FIELDS}
    values[":week"] = _av(scored["week_id"])
    values[":entity"] = _av("week")
    values[":now"] = _av(now)
    try:
        ddb.transact_write_items(TransactItems=[
            {"Put": {
                "TableName": TABLE,
                "Item": {k: _av(v) for k, v in trip_item.items() if v is not None},
                "ConditionExpression": "attribute_not_exists(sk)",
            }},
            {"Update": {
                "TableName": TABLE,
                "Key": {"driver_id": _av(scored["driver_id"]), "sk": _av(f"WEEK#{scored['week_id']}")},
                "UpdateExpression": f"SET week_id = :week, entity = :entity, updated_at = :now ADD {add_expr}",
                "ExpressionAttributeValues": values,
            }},
        ])
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] == "TransactionCanceledException":
            reasons = err.response.get("CancellationReasons", [])
            if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
                logger.info("duplicate trip %s skipped", scored["trip_id"])
                return False
        raise


def _publish_crashes(scored: dict, raw_key: str) -> None:
    entries = [{
        "Source": EVENT_SOURCE,
        "DetailType": "PossibleCrashDetected",
        "EventBusName": EVENT_BUS,
        "Detail": json.dumps({
            "driver_id": scored["driver_id"],
            "trip_id": scored["trip_id"],
            "raw_s3_key": raw_key,
            "crash": crash,
        }),
    } for crash in scored["possible_crashes"]]
    if entries:
        resp = events.put_events(Entries=entries)
        if resp.get("FailedEntryCount"):
            raise RuntimeError(f"EventBridge rejected {resp['FailedEntryCount']} crash event(s)")


def process_object(bucket: str, key: str) -> str:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    trip = json.loads(body)
    scored = score_trip(trip)
    if _write(scored, key):
        _publish_crashes(scored, key)
        return "scored"
    return "duplicate"


def handler(event, context):
    failures = []
    stats = {"scored": 0, "duplicate": 0, "ignored": 0}
    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            if body.get("Event") == "s3:TestEvent":
                stats["ignored"] += 1
                continue
            for s3rec in body.get("Records", []):
                bucket = s3rec["s3"]["bucket"]["name"]
                key = unquote_plus(s3rec["s3"]["object"]["key"])
                stats[process_object(bucket, key)] += 1
        except Exception:  # noqa: BLE001 - report and let SQS retry / DLQ
            logger.exception("failed to process message %s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    logger.info(json.dumps(stats))
    return {"batchItemFailures": failures}
