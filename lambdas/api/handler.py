"""Driver API (API Gateway REST, Lambda proxy integration).

GET /drivers                        all drivers with their latest weekly score
GET /drivers/{driver_id}/score      weekly scores, penalty breakdown, recent trips (no gen AI)
GET /drivers/{driver_id}/explanation[?refresh=true]
                                    gen AI explanation and coaching, cached per driver/week/model
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.config import Config

from explain import explain
from facts import build_facts, facts_hash, plain, select_weeks
from score import score_from_totals

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE = os.environ["TABLE_NAME"]
MODEL_ID = os.environ["MODEL_ID"]
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID") or None
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION") or None
RECENT_TRIPS = 10

table = boto3.resource("dynamodb").Table(TABLE)
bedrock = boto3.client(
    "bedrock-runtime",
    config=Config(read_timeout=25, connect_timeout=5, retries={"max_attempts": 2, "mode": "standard"}),
)


# --------------------------------------------------------------------------- data access

def _query_driver(driver_id: str) -> list[dict]:
    items, kwargs = [], {"KeyConditionExpression": "driver_id = :d",
                         "ExpressionAttributeValues": {":d": driver_id}}
    while True:
        resp = table.query(**kwargs)
        items += resp["Items"]
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _split(items: list[dict]) -> tuple[dict | None, list[dict], list[dict], dict]:
    profile, weeks, trips, cached = None, [], [], {}
    for it in items:
        entity = it.get("entity")
        if entity == "profile":
            profile = it
        elif entity == "week":
            weeks.append(it)
        elif entity == "trip":
            trips.append(it)
        elif entity == "explanation":
            cached[it["sk"]] = it
    return profile, weeks, trips, cached


def _response(status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(plain(body), default=str),
    }


def _week_summary(week: dict) -> dict:
    s = score_from_totals(plain(week))
    return {"week_id": week["week_id"], "score": s["score"], "band": s["band"],
            "trips": int(week.get("trips", 0)), "miles": round(float(week.get("miles", 0)), 1),
            "penalties": s["penalties"], "rates": s["rates"]}


def _driver_card(profile: dict, weeks: list[dict]) -> dict:
    current, previous = select_weeks(weeks)
    card = {
        "driver_id": profile["driver_id"],
        "name": f"{profile['first_name']} {profile['last_initial']}.",
        "vehicle": profile.get("vehicle"),
        "home_metro": profile.get("home_metro"),
        "latest_week": None,
        "score_change": None,
    }
    if current:
        card["latest_week"] = _week_summary(current)
        if previous:
            card["score_change"] = round(card["latest_week"]["score"] - score_from_totals(plain(previous))["score"], 1)
    return card


# --------------------------------------------------------------------------- routes

def list_drivers() -> dict:
    items, kwargs = [], {"FilterExpression": "entity IN (:p, :w)",
                         "ExpressionAttributeValues": {":p": "profile", ":w": "week"}}
    while True:
        resp = table.scan(**kwargs)
        items += resp["Items"]
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    profiles = {i["driver_id"]: i for i in items if i["entity"] == "profile"}
    weeks: dict[str, list] = {}
    for i in items:
        if i["entity"] == "week":
            weeks.setdefault(i["driver_id"], []).append(i)
    drivers = [_driver_card(p, weeks.get(d, [])) for d, p in sorted(profiles.items())]
    return _response(200, {"drivers": drivers, "count": len(drivers)})


def driver_score(driver_id: str) -> dict:
    profile, weeks, trips, _ = _split(_query_driver(driver_id))
    if not profile:
        return _response(404, {"message": f"driver {driver_id} not found"})
    body = _driver_card(profile, weeks)
    body["weeks"] = [_week_summary(w) for w in sorted(weeks, key=lambda w: w["week_id"])]
    recent = sorted(trips, key=lambda t: t["sk"])[-RECENT_TRIPS:]
    body["recent_trips"] = [{
        "trip_id": t["trip_id"], "local_start": t["local_start"], "miles": t["miles"],
        "duration_min": t.get("duration_min"), "score": t["score"], "band": t["band"],
        "penalties": t["penalties"], "possible_crash": bool(t.get("possible_crashes")),
    } for t in reversed(recent)]
    return _response(200, body)


def driver_explanation(driver_id: str, refresh: bool) -> dict:
    profile, weeks, trips, cached = _split(_query_driver(driver_id))
    if not profile:
        return _response(404, {"message": f"driver {driver_id} not found"})
    facts = build_facts(profile, weeks, trips)
    if not facts:
        return _response(404, {"message": f"no scored weeks yet for {driver_id}"})

    digest = facts_hash(facts)
    sk = f"EXPLAIN#{facts['week']['id']}"
    hit = cached.get(sk)
    if hit and not refresh and hit.get("facts_hash") == digest and hit.get("model_id") == MODEL_ID:
        body = json.loads(hit["payload"])
        body["cached"] = True
        return _response(200, body)

    result = explain(facts, bedrock, MODEL_ID, GUARDRAIL_ID, GUARDRAIL_VERSION)
    body = {
        "driver_id": driver_id,
        "week_id": facts["week"]["id"],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **result,
        "facts": facts,
        "cached": False,
    }
    logger.info(json.dumps({"driver_id": driver_id, "source": result["source"], "guardrail": result["guardrail"],
                            "fallback_reason": result["fallback_reason"], "latency_ms": result["latency_ms"],
                            "usage": result["usage"]}))
    if result["source"] == "bedrock":  # don't cache fallbacks; retry Bedrock next time
        table.put_item(Item={
            "driver_id": driver_id, "sk": sk, "entity": "explanation", "week_id": facts["week"]["id"],
            "model_id": MODEL_ID, "facts_hash": digest, "payload": json.dumps(body, default=str),
            "generated_at": body["generated_at"],
        })
    return _response(200, body)


def handler(event, context):
    resource = event.get("resource", "")
    params = event.get("pathParameters") or {}
    query = event.get("queryStringParameters") or {}
    try:
        if event.get("httpMethod") != "GET":
            return _response(405, {"message": "method not allowed"})
        if resource == "/drivers":
            return list_drivers()
        if resource == "/drivers/{driver_id}/score":
            return driver_score(params["driver_id"])
        if resource == "/drivers/{driver_id}/explanation":
            refresh = str(query.get("refresh", "")).lower() in ("1", "true", "yes")
            return driver_explanation(params["driver_id"], refresh)
        return _response(404, {"message": f"no route for {resource}"})
    except Exception:  # noqa: BLE001
        logger.exception("unhandled error")
        return _response(500, {"message": "internal error"})
