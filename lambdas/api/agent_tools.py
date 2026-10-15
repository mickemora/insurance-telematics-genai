"""Action group Lambda for the Amazon Bedrock Agent copilot (Day 4).

Read-only tools over the same data the API serves. The agent calls them by
name with string parameters; each returns compact JSON so the agent's
context stays small.

Event and response formats follow Bedrock Agents "function details" action groups.
"""
from __future__ import annotations

import json
import logging

import handler as api  # same code asset as the API: reuse its data access

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_TRIPS = 25


def _body(resp: dict) -> dict:
    return json.loads(resp["body"])


def list_drivers(**_) -> dict:
    drivers = _body(api.list_drivers())["drivers"]
    return {"drivers": [{
        "driver_id": d["driver_id"], "name": d["name"], "metro": d["home_metro"],
        "latest_week": (d["latest_week"] or {}).get("week_id"),
        "score": (d["latest_week"] or {}).get("score"),
        "band": (d["latest_week"] or {}).get("band"),
        "change_vs_previous_week": d["score_change"],
    } for d in drivers]}


def get_driver_score(driver_id: str, **_) -> dict:
    resp = api.driver_score(driver_id)
    if resp["statusCode"] != 200:
        return {"error": _body(resp)["message"]}
    b = _body(resp)
    return {
        "driver_id": b["driver_id"], "name": b["name"], "vehicle": b["vehicle"], "metro": b["home_metro"],
        "weeks": [{"week_id": w["week_id"], "score": w["score"], "band": w["band"], "trips": w["trips"],
                   "miles": w["miles"], "points_lost": {k: v for k, v in w["penalties"].items() if v >= 0.5}}
                  for w in b["weeks"]],
    }


def get_driver_trips(driver_id: str, limit: str | int = 10, **_) -> dict:
    limit = max(1, min(MAX_TRIPS, int(limit or 10)))
    profile, _weeks, trips, _ = api._split(api._query_driver(driver_id))
    if not profile:
        return {"error": f"driver {driver_id} not found"}
    recent = sorted(trips, key=lambda t: t["sk"])[-limit:]
    rows = []
    for t in reversed(recent):
        t = api.plain(t)
        rows.append({
            "local_start": t["local_start"], "miles": t["miles"], "minutes": t.get("duration_min"),
            "score": t["score"],
            "points_lost": {k: v for k, v in t["penalties"].items() if v >= 0.5},
            "events": {k: t["totals"].get(k, 0) for k in ("hard_brake_count", "rapid_accel_count",
                                                          "harsh_corner_count", "possible_crash_count")},
            "phone_minutes": round(t["totals"].get("phone_seconds", 0) / 60, 1),
        })
    return {"driver_id": driver_id, "trip_count": len(rows), "trips": rows}


def get_score_explanation(driver_id: str, **_) -> dict:
    resp = api.driver_explanation(driver_id, refresh=False)
    if resp["statusCode"] != 200:
        return {"error": _body(resp)["message"]}
    b = _body(resp)
    return {"driver_id": driver_id, "week_id": b["week_id"], "headline": b["headline"], "summary": b["summary"],
            "tips": b["tips"], "source": b["source"]}


def list_claims(status: str | None = None, **_) -> dict:
    claims = _body(api.list_claims())["claims"]
    if status:
        claims = [c for c in claims if c["status"] == status]
    return {"claims": [{k: c[k] for k in ("claim_id", "status", "triage", "member", "member_driver_id",
                                          "detected_at_local", "impact", "verdict")} for c in claims]}


def get_claim(claim_id: str, **_) -> dict:
    resp = api.get_claim(claim_id)
    if resp["statusCode"] != 200:
        return {"error": _body(resp)["message"]}
    c = _body(resp)
    f = c["crash_facts"]
    draft = c.get("draft") or {}
    return {
        "claim_id": c["claim_id"], "status": c["status"], "triage": c["triage"],
        "triage_reasons": c["triage_reasons"], "member": f["member"], "detected_at_local": f["detected_at_local"],
        "location": f["location"].get("address"), "impact": f["impact"], "after_impact": f["after_impact"],
        "statement": c.get("statement"), "statement_checks": c.get("statement_checks"),
        "draft": {k: draft.get(k) for k in ("incident_summary", "statement_assessment", "adjuster_questions",
                                            "next_steps", "source")} if draft else None,
    }


TOOLS = {
    "list_drivers": list_drivers,
    "get_driver_score": get_driver_score,
    "get_driver_trips": get_driver_trips,
    "get_score_explanation": get_score_explanation,
    "list_claims": list_claims,
    "get_claim": get_claim,
}


def handler(event, context):
    function = event.get("function")
    params = {p["name"]: p.get("value") for p in event.get("parameters") or []}
    logger.info(json.dumps({"function": function, "parameters": params}))
    try:
        if function not in TOOLS:
            raise ValueError(f"unknown function {function}")
        result, state = TOOLS[function](**params), None
    except Exception as err:  # noqa: BLE001 - report back to the agent instead of failing the turn
        logger.exception("tool failed")
        result, state = {"error": f"{type(err).__name__}: {err}"}, "REPROMPT"
    function_response = {"responseBody": {"TEXT": {"body": json.dumps(result, default=str)}}}
    if state:
        function_response["responseState"] = state
    return {
        "messageVersion": "1.0",
        "response": {"actionGroup": event.get("actionGroup"), "function": function,
                     "functionResponse": function_response},
        "sessionAttributes": event.get("sessionAttributes", {}),
        "promptSessionAttributes": event.get("promptSessionAttributes", {}),
    }
