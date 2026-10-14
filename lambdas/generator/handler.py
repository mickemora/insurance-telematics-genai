"""Generator Lambda: writes synthetic trips to S3 and driver profiles to DynamoDB.

Invoke with an optional JSON payload, for example:

    {"drivers": 20, "days": 28, "seed": 42, "crashes": 3, "false_alarms": 2, "reset": true}

Raw trips land under raw/trips/, which triggers the scoring pipeline.
The ground-truth manifest lands under ground-truth/ and is never scored.
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal

import boto3

from synth import generate

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BUCKET = os.environ["RAW_BUCKET"]
TABLE = os.environ["TABLE_NAME"]
RAW_PREFIX = "raw/trips/"

s3 = boto3.client("s3")
table = boto3.resource("dynamodb").Table(TABLE)

LIMITS = {"drivers": (1, 200), "days": (1, 120), "crashes": (0, 20), "false_alarms": (0, 20)}


def _int_param(event: dict, name: str, default: int) -> int:
    value = int(event.get(name, default))
    lo, hi = LIMITS.get(name, (value, value))
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return value


def _to_dynamo(obj: dict) -> dict:
    """DynamoDB needs Decimal instead of float."""
    return json.loads(json.dumps(obj), parse_float=Decimal)


def trip_key(trip: dict) -> str:
    day = trip["start_time"][:10]
    return f"{RAW_PREFIX}driver_id={trip['driver_id']}/date={day}/{trip['trip_id']}.json"


def _reset() -> dict:
    """Delete previously generated raw trips and every table item."""
    deleted_objects = 0
    paginator = s3.get_paginator("list_objects_v2")
    for prefix in (RAW_PREFIX, "ground-truth/", "reference/"):
        for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            for i in range(0, len(keys), 1000):
                s3.delete_objects(Bucket=BUCKET, Delete={"Objects": keys[i:i + 1000], "Quiet": True})
                deleted_objects += len(keys[i:i + 1000])

    deleted_items = 0
    scan_kwargs = {"ProjectionExpression": "driver_id, sk"}
    with table.batch_writer() as batch:
        while True:
            resp = table.scan(**scan_kwargs)
            for item in resp.get("Items", []):
                batch.delete_item(Key={"driver_id": item["driver_id"], "sk": item["sk"]})
                deleted_items += 1
            if "LastEvaluatedKey" not in resp:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return {"deleted_objects": deleted_objects, "deleted_items": deleted_items}


def handler(event, context):
    event = event or {}
    drivers = _int_param(event, "drivers", 20)
    days = _int_param(event, "days", 28)
    crashes = _int_param(event, "crashes", 3)
    false_alarms = _int_param(event, "false_alarms", 2)
    seed = int(event.get("seed", 42))
    end_date = date.fromisoformat(event["end_date"]) if event.get("end_date") else None

    reset_stats = _reset() if event.get("reset") else None

    data = generate(drivers=drivers, days=days, seed=seed, crashes=crashes,
                    false_alarms=false_alarms, end_date=end_date)
    run_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    with table.batch_writer() as batch:
        for p in data["profiles"]:
            item = _to_dynamo(p)
            item["sk"] = "PROFILE"
            item["entity"] = "profile"
            batch.put_item(Item=item)

    def put_trip(trip: dict) -> None:
        s3.put_object(Bucket=BUCKET, Key=trip_key(trip), Body=json.dumps(trip).encode(),
                      ContentType="application/json")

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(put_trip, data["trips"]))

    manifest_key = f"ground-truth/run_{run_at}_seed{seed}.json"
    s3.put_object(Bucket=BUCKET, Key=manifest_key, Body=json.dumps(data["manifest"], indent=2).encode(),
                  ContentType="application/json")
    s3.put_object(Bucket=BUCKET, Key="reference/drivers.json",
                  Body=json.dumps(data["profiles"], indent=2).encode(), ContentType="application/json")

    result = {
        "drivers": len(data["profiles"]),
        "trips": len(data["trips"]),
        "planted_events": len(data["manifest"]["planted_events"]),
        "manifest_key": manifest_key,
        "reset": reset_stats,
    }
    logger.info(json.dumps(result))
    return result
