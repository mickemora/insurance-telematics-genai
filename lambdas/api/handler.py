"""Driver API (API Gateway REST, Lambda proxy integration).

GET /drivers                        all drivers with their latest weekly score
GET /drivers/{driver_id}/score      weekly scores, penalty breakdown, recent trips (no gen AI)
GET /drivers/{driver_id}/explanation[?refresh=true]
                                    gen AI explanation and coaching, cached per driver/week/model
GET  /claims                        claims opened by the crash workflow (Day 3)
GET  /claims/{claim_id}             one claim: crash facts, triage, statement, FNOL draft
POST /claims/{claim_id}/statement   member statement {"statement": "..."}; resumes the workflow
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

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
CLAIMS_PK = "CLAIMS"
MAX_STATEMENT_CHARS = 1000

table = boto3.resource("dynamodb").Table(TABLE)
bedrock = boto3.client(
    "bedrock-runtime",
    config=Config(read_timeout=25, connect_timeout=5, retries={"max_attempts": 2, "mode": "standard"}),
)
sfn = boto3.client("stepfunctions")


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


# --------------------------------------------------------------------------- claims (Day 3)

CLAIM_SUMMARY_FIELDS = ("claim_id", "status", "triage", "triage_label", "member_driver_id", "trip_id",
                        "created_at", "updated_at")


def _claim_summary(c: dict) -> dict:
    out = {k: c.get(k) for k in CLAIM_SUMMARY_FIELDS}
    f = c.get("crash_facts", {})
    out["member"] = f.get("member", {}).get("first_name")
    out["detected_at_local"] = f.get("detected_at_local")
    out["impact"] = {k: f.get("impact", {}).get(k) for k in ("force_g", "direction_label", "speed_at_impact_mph")}
    out["verdict"] = (c.get("draft") or {}).get("statement_assessment", {}).get("verdict")
    return out


def list_claims() -> dict:
    items, kwargs = [], {"KeyConditionExpression": "driver_id = :p", "ExpressionAttributeValues": {":p": CLAIMS_PK}}
    while True:
        resp = table.query(**kwargs)
        items += resp["Items"]
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    claims = sorted((_claim_summary(plain(i)) for i in items), key=lambda c: c["created_at"] or "", reverse=True)
    return _response(200, {"claims": claims, "count": len(claims)})


def get_claim(claim_id: str) -> dict:
    item = table.get_item(Key={"driver_id": CLAIMS_PK, "sk": claim_id}).get("Item")
    if not item:
        return _response(404, {"message": f"claim {claim_id} not found"})
    item = plain(item)
    item.pop("task_token", None)
    item.pop("driver_id", None)
    item.pop("sk", None)
    return _response(200, item)


def post_statement(claim_id: str, raw_body: str | None) -> dict:
    try:
        statement = str(json.loads(raw_body or "{}").get("statement", "")).strip()
    except (ValueError, AttributeError):
        return _response(400, {"message": 'body must be JSON like {"statement": "..."}'})
    if not statement:
        return _response(400, {"message": "statement is required"})
    if len(statement) > MAX_STATEMENT_CHARS:
        return _response(400, {"message": f"statement is limited to {MAX_STATEMENT_CHARS} characters"})

    key = {"driver_id": CLAIMS_PK, "sk": claim_id}
    item = table.get_item(Key=key).get("Item")
    if not item:
        return _response(404, {"message": f"claim {claim_id} not found"})
    if item.get("status") != "awaiting_statement" or not item.get("task_token"):
        return _response(409, {"message": f"claim is {item.get('status')}; it is not waiting for a statement"})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        # Save first (the draft step reads the statement from the table), then resume the workflow.
        table.update_item(
            Key=key,
            UpdateExpression="SET statement = :st, statement_received_at = :now, #s = :drafting, updated_at = :now",
            ConditionExpression="#s = :awaiting",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":st": statement, ":now": now, ":drafting": "drafting",
                                       ":awaiting": "awaiting_statement"},
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _response(409, {"message": "claim is no longer waiting for a statement"})
        raise
    try:
        sfn.send_task_success(taskToken=item["task_token"], output=json.dumps({"statement_received": True}))
    except ClientError as err:
        code = err.response["Error"]["Code"]
        if code in ("TaskTimedOut", "TaskDoesNotExist", "InvalidToken"):
            return _response(409, {"message": f"the workflow is no longer waiting ({code})"})
        raise
    return _response(202, {"claim_id": claim_id, "status": "drafting",
                            "message": "Statement received; the FNOL draft is being prepared."})


def handler(event, context):
    resource = event.get("resource", "")
    params = event.get("pathParameters") or {}
    query = event.get("queryStringParameters") or {}
    method = event.get("httpMethod")
    try:
        if method == "POST" and resource == "/claims/{claim_id}/statement":
            return post_statement(params["claim_id"], event.get("body"))
        if method != "GET":
            return _response(405, {"message": "method not allowed"})
        if resource == "/claims":
            return list_claims()
        if resource == "/claims/{claim_id}":
            return get_claim(params["claim_id"])
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
