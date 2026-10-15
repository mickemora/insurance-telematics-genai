"""Claims workflow task handlers (one asset, four Lambda functions).

Step Functions runs:  enrich -> Choice(triage) -> close_false_alarm
                                              \\-> checkin (waits for the member) -> draft

Claims live in the shared table under the partition key "CLAIMS" (sk = claim id),
so listing claims is a single query.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from crash import TRIAGE_LABELS, build_crash_facts, check_statement, find_crash_event, triage
from draft import draft_claim
from facts import plain

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE = os.environ["TABLE_NAME"]
CLAIMS_PK = "CLAIMS"
STATEMENT_TIMEOUT_MIN = int(os.environ.get("STATEMENT_TIMEOUT_MINUTES", "30"))

table = boto3.resource("dynamodb").Table(TABLE)
_clients: dict = {}


def _client(name: str, **kw):
    if name not in _clients:
        _clients[name] = boto3.client(name, **kw)
    return _clients[name]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ddb(obj):
    return json.loads(json.dumps(obj, default=str), parse_float=Decimal)


def _key(claim_id: str) -> dict:
    return {"driver_id": CLAIMS_PK, "sk": claim_id}


def claim_id_for(trip_id: str) -> str:
    return "CLM-" + trip_id.replace("-", "")[:8].upper()


def _load_owned(event: dict) -> dict | None:
    """The claim, unless it is missing or now belongs to a newer workflow run."""
    claim = plain(table.get_item(Key=_key(event["claim_id"])).get("Item"))
    if not claim:
        return None
    if event.get("execution_id") and claim.get("execution_id") not in (None, event["execution_id"]):
        logger.info("claim %s belongs to a newer execution; skipping", event["claim_id"])
        return None
    return claim


def _reverse_geocode(lat: float, lon: float) -> str | None:
    """Street address from Amazon Location (Places API v2). Best effort."""
    try:
        resp = _client("geo-places").reverse_geocode(
            QueryPosition=[lon, lat], MaxResults=1, IntendedUse="Storage", Language="en")
        items = resp.get("ResultItems", [])
        if items:
            return items[0].get("Address", {}).get("Label") or items[0].get("Title")
    except Exception as err:  # noqa: BLE001 - an address is nice to have, never required
        logger.warning("reverse geocode failed: %s", err)
    return None


# --------------------------------------------------------------------------- tasks

def enrich(event, context):
    """Input: {"detail": <PossibleCrashDetected detail>, "execution_id": ...}."""
    detail = event["detail"]
    claim_id = claim_id_for(detail["trip_id"])
    trip = json.loads(_client("s3").get_object(Bucket=os.environ["RAW_BUCKET"], Key=detail["raw_s3_key"])["Body"].read())
    crash = find_crash_event(trip, detail["crash"]["t"])
    profile = plain(table.get_item(Key={"driver_id": detail["driver_id"], "sk": "PROFILE"}).get("Item"))
    address = _reverse_geocode(float(crash["lat"]), float(crash["lon"]))
    facts = build_crash_facts(trip, crash, profile, address)
    label, reasons = triage(facts)
    now = _now()
    item = {
        **_key(claim_id), "entity": "claim", "claim_id": claim_id, "member_driver_id": detail["driver_id"],
        "trip_id": detail["trip_id"], "raw_s3_key": detail["raw_s3_key"], "execution_id": event.get("execution_id"),
        "status": "detected", "triage": label, "triage_label": TRIAGE_LABELS[label], "triage_reasons": reasons,
        "crash_facts": facts, "created_at": now, "updated_at": now,
    }
    try:
        table.put_item(Item=_ddb(item), ConditionExpression="attribute_not_exists(sk)")
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info("claim %s already exists; skipping duplicate event", claim_id)
            return {"claim_id": claim_id, "triage": label, "duplicate": True}
        raise
    logger.info(json.dumps({"claim_id": claim_id, "triage": label, "address": address}))
    return {"claim_id": claim_id, "triage": label, "duplicate": False}


def close_false_alarm(event, context):
    claim = _load_owned(event)
    if not claim:
        return {"claim_id": event["claim_id"], "status": "claim_missing_or_superseded"}
    when = claim["crash_facts"]["detected_at_local"]
    message = (f"We noticed a sudden jolt from your phone on {when}, but your trip continued normally, "
               "so we haven't opened a claim. If something did happen, just reply and we'll help right away.")
    table.update_item(
        Key=_key(event["claim_id"]),
        UpdateExpression="SET #s = :s, member_message = :m, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "closed_false_alarm", ":m": message, ":u": _now()},
    )
    return {"claim_id": event["claim_id"], "status": "closed_false_alarm"}


def checkin(event, context):
    """waitForTaskToken task: store the token; the API resumes the workflow when the member answers."""
    claim = _load_owned(event)
    if not claim:
        raise RuntimeError(f"claim {event['claim_id']} not found or superseded")
    f = claim["crash_facts"]
    where = f["location"].get("address") or "your location"
    message = (f"{f['member']['first_name']}, it looks like you may have been in a collision on "
               f"{f['detected_at_local']} near {where}. Are you okay? If anyone is hurt, call 911 first. "
               "When you're ready, tell us in a sentence or two what happened and we'll start your claim.")
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=STATEMENT_TIMEOUT_MIN)).strftime("%Y-%m-%dT%H:%M:%SZ")
    table.update_item(
        Key=_key(event["claim_id"]),
        UpdateExpression="SET #s = :s, task_token = :t, member_message = :m, statement_deadline = :d, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "awaiting_statement", ":t": event["task_token"], ":m": message,
                                   ":d": deadline, ":u": _now()},
    )
    return {"claim_id": event["claim_id"], "status": "awaiting_statement"}


def draft(event, context):
    claim = _load_owned(event)
    if not claim:
        return {"claim_id": event["claim_id"], "status": "claim_missing_or_superseded"}
    statement = claim.get("statement")
    inputs = {
        "crash_facts": claim["crash_facts"],
        "member_statement": statement,
        "statement_checks": check_statement(statement, claim["crash_facts"]),
    }
    bedrock = _client("bedrock-runtime", config=Config(read_timeout=50, retries={"max_attempts": 2, "mode": "standard"}))
    result = draft_claim(inputs, bedrock, os.environ["MODEL_ID"],
                         os.environ.get("GUARDRAIL_ID") or None, os.environ.get("GUARDRAIL_VERSION") or None)
    table.update_item(
        Key=_key(event["claim_id"]),
        UpdateExpression="SET #s = :s, draft = :d, statement_checks = :c, drafted_at = :u, updated_at = :u "
                         "REMOVE task_token",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "draft_ready_for_adjuster", ":d": _ddb(result),
                                   ":c": _ddb(inputs["statement_checks"]), ":u": _now()},
    )
    logger.info(json.dumps({"claim_id": event["claim_id"], "source": result["source"],
                            "verdict": result["statement_assessment"]["verdict"], "guardrail": result["guardrail"],
                            "fallback_reason": result["fallback_reason"], "latency_ms": result["latency_ms"]}))
    return {"claim_id": event["claim_id"], "status": "draft_ready_for_adjuster", "source": result["source"],
            "verdict": result["statement_assessment"]["verdict"]}
