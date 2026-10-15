"""Crash facts, triage, and statement checks for the claims workflow.

Pure Python. Everything the claim drafter may say about the crash is computed
here from the raw trip (including the 1 Hz window around the impact). The
driver's driving score is deliberately NOT included: it should not influence
how a claim is described.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

STOPPED_MPH = 2.0            # "stopped" after impact
MIN_STATIONARY_S = 60        # must stay stopped this long to look like a real crash
MIN_IMPACT_G = 3.0
KEPT_MOVING_MPH = 10.0       # still moving at this speed afterwards -> likely false alarm
PRE_BRAKE_G = -0.45          # deceleration that counts as braking in the high-res window

DIRECTION_LABELS = {
    "front": "the front",
    "rear": "the rear",
    "left": "the driver's side",
    "right": "the passenger side",
}

TRIAGE_LABELS = {
    "likely_crash": "likely crash",
    "likely_false_alarm": "likely false alarm",
    "needs_review": "needs review",
}


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _clock(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def find_crash_event(trip: dict, t: int | float) -> dict:
    """The possible_crash event (with its high-res window) at trip offset t."""
    for ev in trip.get("events", []):
        if ev["type"] == "possible_crash" and int(ev["t"]) == int(t):
            return ev
    raise KeyError(f"no possible_crash at t={t} in trip {trip.get('trip_id')}")


# --------------------------------------------------------------------------- facts

def build_crash_facts(trip: dict, crash: dict, profile: dict | None, address: str | None = None) -> dict:
    offset = timedelta(minutes=trip.get("utc_offset_minutes", 0))
    start_local = _parse(trip["start_time"]) + offset
    end_local = _parse(trip["end_time"]) + offset
    impact_local = start_local + timedelta(seconds=crash["t"])
    interval = trip.get("sample_interval_s", 10)

    hr = {int(p["t_offset_s"]): p for p in crash.get("high_res", [])}
    speed_before = float(hr[-5]["spd"]) if -5 in hr else float(crash["speed_mph"])
    speed_at_impact = float(hr[0]["spd"]) if 0 in hr else float(crash["speed_mph"])
    brake_offsets = [o for o in range(-5, 0) if o in hr and float(hr[o]["g"]) <= PRE_BRAKE_G]
    braked = bool(brake_offsets)

    samples = trip.get("samples", [])
    before = [s for s in samples if s["t"] <= crash["t"]]
    miles_into = sum(float(s["spd"]) for s in before) * interval / 3600.0
    limit = min(samples, key=lambda s: abs(s["t"] - crash["t"]))["lim"] if samples else None

    profile = profile or {}
    vehicle = profile.get("vehicle") or {}
    direction = crash.get("impact_direction", "unknown")
    stationary_s = int(crash.get("stationary_after_s", 0))
    speed_after = float(crash.get("speed_after_mph", 0))

    facts: dict[str, Any] = {
        "claim_type": "auto collision detected by phone telematics",
        "member": {
            "first_name": profile.get("first_name"),
            "last_initial": profile.get("last_initial"),
            "policy_id": profile.get("policy_id"),
            "vehicle": " ".join(str(vehicle[k]) for k in ("year", "make", "model") if k in vehicle) or None,
        },
        "detected_at_local": f"{impact_local:%A, %b} {impact_local.day} at {_clock(impact_local)}",
        "location": {
            "address": address,
            "metro": profile.get("home_metro"),
            "lat": round(float(crash["lat"]), 5),
            "lon": round(float(crash["lon"]), 5),
        },
        "trip": {
            "started_local": _clock(start_local),
            "minutes_into_trip": round(crash["t"] / 60),
            "miles_into_trip": round(miles_into, 1),
        },
        "impact": {
            "force_g": round(float(crash["impact_g"]), 1),
            "direction": direction,
            "direction_label": DIRECTION_LABELS.get(direction, direction),
            "speed_5s_before_mph": round(speed_before),
            "speed_at_impact_mph": round(speed_at_impact),
            "posted_limit_mph": limit,
            "braking_before_impact": braked,
            "braking_started_seconds_before": -min(brake_offsets) if braked else None,
        },
        "after_impact": {
            "speed_mph": round(speed_after),
            "stationary_seconds": stationary_s,
            "trip_ended_at_crash_site": stationary_s > 0 and end_local - impact_local <= timedelta(
                seconds=stationary_s + interval),
        },
        "detector_confidence": float(crash.get("detector_confidence", 0)),
    }
    facts["timeline"] = _timeline(facts, start_local, impact_local, end_local)
    return facts


def _timeline(f: dict, start: datetime, impact: datetime, end: datetime) -> list[dict]:
    i, a = f["impact"], f["after_impact"]
    rows = [{"when": _clock(start), "what": "Trip started"}]
    rows.append({"when": "5 s before impact", "what": f"Traveling at {i['speed_5s_before_mph']} mph"})
    if i["braking_before_impact"]:
        rows.append({"when": f"{i['braking_started_seconds_before']} s before impact",
                     "what": "Hard braking detected"})
    else:
        rows.append({"when": "Before impact", "what": "No braking detected"})
    rows.append({"when": f"{_clock(impact)} (impact)",
                 "what": f"{i['force_g']} g impact to {i['direction_label']} at {i['speed_at_impact_mph']} mph"})
    if a["stationary_seconds"]:
        rows.append({"when": "After impact",
                     "what": f"Vehicle stopped and stayed stationary for {a['stationary_seconds']} seconds"})
    else:
        rows.append({"when": "After impact", "what": f"Vehicle kept moving at {a['speed_mph']} mph"})
    rows.append({"when": _clock(end), "what": "Trip ended"})
    return rows


# --------------------------------------------------------------------------- triage

def triage(facts: dict) -> tuple[str, list[str]]:
    """Rule-based, explainable triage. Returns (label, reasons)."""
    i, a = facts["impact"], facts["after_impact"]
    if a["speed_mph"] >= KEPT_MOVING_MPH and a["stationary_seconds"] == 0:
        return "likely_false_alarm", [
            f"vehicle kept moving at {a['speed_mph']} mph right after the spike",
            "no stop after the event",
            f"detector confidence {facts['detector_confidence']}",
        ]
    if a["speed_mph"] <= STOPPED_MPH and a["stationary_seconds"] >= MIN_STATIONARY_S and i["force_g"] >= MIN_IMPACT_G:
        return "likely_crash", [
            f"{i['force_g']} g impact to {i['direction_label']}",
            f"vehicle stopped and stayed stationary for {a['stationary_seconds']} seconds",
            f"detector confidence {facts['detector_confidence']}",
        ]
    return "needs_review", ["signals are mixed; a person should review the trip"]


# --------------------------------------------------------------------------- statement checks

_SPEED = re.compile(r"(\d+(?:\.\d+)?)\s*(?:mph|miles per hour|miles an hour)", re.IGNORECASE)
_STOPPED = re.compile(r"\bI was (?:stopped|parked|stationary|standing still|not moving|sitting at)|"
                      r"\bI wasn'?t moving|\bwhile I was (?:stopped|parked)", re.IGNORECASE)
_DIRECTIONS = {
    "rear": re.compile(r"rear-?ended|from behind|behind me|back of my car|hit me from the back", re.IGNORECASE),
    "front": re.compile(r"car in front|car ahead|in front of me|front of my car|front end", re.IGNORECASE),
    "side": re.compile(r"t-?boned|side of my car|driver'?s? side|passenger side", re.IGNORECASE),
}
_NO_BRAKE = re.compile(r"(?:didn'?t|did not|couldn'?t|could not|no time to|never) (?:have time to )?brake",
                       re.IGNORECASE)
_BRAKE = re.compile(r"\b(?:braked|hit the brakes|slammed on the brakes|brak(?:e|ing) hard)\b", re.IGNORECASE)


def check_statement(statement: str | None, facts: dict) -> list[dict]:
    """Deterministic comparisons between what the member says and the data.

    Only aspects the statement actually mentions are checked.
    """
    if not statement:
        return []
    i = facts["impact"]
    checks = []
    before, at = i["speed_5s_before_mph"], i["speed_at_impact_mph"]
    data_speed = f"{before} mph 5 seconds before impact, {at} mph at impact"

    m = _SPEED.search(statement)
    if m:
        claimed = float(m.group(1))
        ok = any(abs(claimed - d) <= max(8.0, 0.3 * d) for d in (before, at))
        checks.append({"aspect": "speed", "statement_says": f"about {claimed:g} mph",
                       "data_shows": data_speed, "consistent": ok})
    elif _STOPPED.search(statement):
        checks.append({"aspect": "speed", "statement_says": "stopped / not moving",
                       "data_shows": data_speed, "consistent": at <= 5})

    claimed_dirs = [d for d, rx in _DIRECTIONS.items() if rx.search(statement)]
    if len(claimed_dirs) == 1:
        actual = "side" if i["direction"] in ("left", "right") else i["direction"]
        checks.append({"aspect": "impact direction", "statement_says": f"impact to the {claimed_dirs[0]}",
                       "data_shows": f"impact to {i['direction_label']}", "consistent": claimed_dirs[0] == actual})

    if _NO_BRAKE.search(statement):
        claimed_brake = False
    elif _BRAKE.search(statement):
        claimed_brake = True
    else:
        claimed_brake = None
    if claimed_brake is not None:
        checks.append({"aspect": "braking", "statement_says": "braked" if claimed_brake else "did not brake",
                       "data_shows": "hard braking before impact" if i["braking_before_impact"]
                       else "no braking before impact",
                       "consistent": claimed_brake == i["braking_before_impact"]})
    return checks


def simulate_statement(facts: dict, truthful: bool = True) -> str:
    """Demo helper: a member statement that matches (or contradicts) the data."""
    i = facts["impact"]
    before, at, braked = i["speed_5s_before_mph"], i["speed_at_impact_mph"], i["braking_before_impact"]
    if not truthful:
        if at >= 12:
            return "I was only going about 5 mph and someone hit me from behind. There was nothing I could do."
        return "I was going about 35 mph when the front of my car hit the car ahead. I braked hard but it was too late."
    if i["direction"] == "rear":
        if at <= 5:
            return "I was stopped at a red light when the car behind me rear-ended me. I'm a bit shaken but okay."
        return f"I was driving about {before} mph when someone hit me from behind."
    if i["direction"] in ("left", "right"):
        side = "driver's side" if i["direction"] == "left" else "passenger side"
        brake = " I braked hard but couldn't avoid it." if braked else ""
        return (f"Another car came out of the intersection and hit the {side} of my car. "
                f"I was going about {before} mph.{brake}")
    brake = "I hit the brakes" if braked else "I didn't have time to brake"
    return (f"Traffic ahead slowed suddenly. {brake}, and the front of my car hit the car ahead. "
            f"I was going about {before} mph.")
