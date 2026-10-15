"""Deterministic facts for the score explainer.

The language model never calculates anything. This module turns DynamoDB
items (profile, WEEK#, TRIP#) into a small, pre-computed, pre-rounded fact
sheet. The model only puts these facts into words, and the validator checks
that every number it writes comes from here.

Pure Python: shared by the API Lambda (via the layer), tests, and demo.py.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from score import band, score_from_totals

MIN_TRIPS_FOR_WEEK = 5          # a trailing week with fewer trips is treated as "in progress"
STEADY_THRESHOLD = 3.0          # |score change| below this counts as "held steady"
MIN_POINTS = 0.5                # a factor must cost at least this much to be worth mentioning

FACTORS: dict[str, dict[str, str]] = {
    "hard_braking": {"label": "hard braking", "unit": "hard brakes per 100 miles",
                     "counter": "hard_brake_count", "kind": "events"},
    "rapid_acceleration": {"label": "rapid acceleration", "unit": "rapid accelerations per 100 miles",
                           "counter": "rapid_accel_count", "kind": "events"},
    "harsh_cornering": {"label": "harsh cornering", "unit": "harsh corners per 100 miles",
                        "counter": "harsh_corner_count", "kind": "events"},
    "speeding": {"label": "speeding", "unit": "% of moving time more than 10 mph over the limit",
                 "counter": "speeding_seconds", "kind": "seconds"},
    "phone_handling": {"label": "phone handling", "unit": "minutes of phone handling per driving hour",
                       "counter": "phone_seconds", "kind": "seconds"},
    "late_night": {"label": "late-night driving", "unit": "% of driving time between midnight and 4 AM",
                   "counter": "night_seconds", "kind": "seconds"},
}

# (label, start hour inclusive, end hour exclusive), local time
TIME_BUCKETS = [
    ("overnight (12-4 AM)", 0, 4),
    ("early morning (4-7 AM)", 4, 7),
    ("morning commute (7-10 AM)", 7, 10),
    ("midday (10 AM-4 PM)", 10, 16),
    ("evening commute (4-7 PM)", 16, 19),
    ("evening (7 PM-midnight)", 19, 24),
]

BAND_LABELS = {"excellent": "excellent", "good": "good", "fair": "fair", "needs_attention": "needs attention"}


# --------------------------------------------------------------------------- helpers

def plain(obj: Any) -> Any:
    """DynamoDB Decimals -> int/float, recursively."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [plain(v) for v in obj]
    return obj


def _r1(x: float) -> float:
    return round(float(x), 1)


def week_dates(week_id: str) -> str:
    year, wk = week_id.split("-W")
    start = date.fromisocalendar(int(year), int(wk), 1)
    end = date.fromisocalendar(int(year), int(wk), 7)
    if start.month == end.month:
        return f"{start:%b} {start.day}-{end.day}"
    return f"{start:%b} {start.day}-{end:%b} {end.day}"


def _bucket(hour: int) -> str:
    for label, lo, hi in TIME_BUCKETS:
        if lo <= hour < hi:
            return label
    return TIME_BUCKETS[-1][0]


def _trip_when(local_start: str) -> str:
    dt = datetime.strptime(local_start, "%Y-%m-%dT%H:%M:%S")
    hour12 = dt.hour % 12 or 12
    return f"{dt:%A} {hour12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def _factor_detail(factor: str, totals: dict) -> str:
    meta = FACTORS[factor]
    value = float(totals.get(meta["counter"], 0))
    if factor == "hard_braking":
        return f"{int(value)} hard brake{'s' if value != 1 else ''}"
    if factor == "rapid_acceleration":
        return f"{int(value)} rapid acceleration{'s' if value != 1 else ''}"
    if factor == "harsh_cornering":
        return f"{int(value)} harsh corner{'s' if value != 1 else ''}"
    if factor == "phone_handling":
        return f"{round(value / 60, 1)} minutes of phone handling"
    if factor == "speeding":
        moving = float(totals.get("moving_seconds", 0)) or 1
        return f"{round(value / moving * 100)}% of moving time speeding"
    return f"{round(value / 60)} minutes after midnight"


def select_weeks(week_items: Iterable[dict]) -> tuple[dict | None, dict | None]:
    """(current, previous). A trailing week with too few trips is in progress, so skip it."""
    weeks = sorted((plain(w) for w in week_items), key=lambda w: w["week_id"])
    if len(weeks) >= 2 and weeks[-1].get("trips", 0) < MIN_TRIPS_FOR_WEEK:
        weeks = weeks[:-1]
    if not weeks:
        return None, None
    return weeks[-1], (weeks[-2] if len(weeks) >= 2 else None)


# --------------------------------------------------------------------------- facts

def _pattern(factor: str, trips: list[dict]) -> dict | None:
    """When does the top factor happen, compared with when the driver drives?"""
    if factor == "late_night":
        return None  # the factor already is a time of day
    counter = FACTORS[factor]["counter"]
    qty: dict[str, float] = defaultdict(float)
    miles: dict[str, float] = defaultdict(float)
    for t in trips:
        dt = datetime.strptime(t["local_start"], "%Y-%m-%dT%H:%M:%S")
        key = f"{'weekend' if dt.weekday() >= 5 else 'weekday'} {_bucket(dt.hour)}"
        qty[key] += float(t["totals"].get(counter, 0))
        miles[key] += float(t.get("miles", 0))
    total_q, total_m = sum(qty.values()), sum(miles.values())
    enough = total_q >= (3 if FACTORS[factor]["kind"] == "events" else 120)
    if not enough or total_m <= 0:
        return None
    key = max(qty, key=lambda k: qty[k])
    share, mile_share = qty[key] / total_q, miles[key] / total_m
    if share < 0.4 or share - mile_share < 0.15:
        return None
    return {
        "factor": factor,
        "when": f"{key} trips",
        "share_of_factor_pct": round(share * 100),
        "share_of_miles_pct": round(mile_share * 100),
    }


def build_facts(profile: dict, week_items: Iterable[dict], trip_items: Iterable[dict]) -> dict | None:
    """The complete fact sheet for one driver, or None if there is no scored week yet."""
    profile = plain(profile)
    current, previous = select_weeks(week_items)
    if not current:
        return None
    cur = score_from_totals(current)
    prev = score_from_totals(previous) if previous else None
    week_trips = [plain(t) for t in trip_items if t.get("week_id") == current["week_id"]]

    factors = []
    for name, meta in FACTORS.items():
        row = {
            "factor": name,
            "label": meta["label"],
            "points_lost": _r1(cur["penalties"][name]),
            "rate": _r1(cur["rates"][name]),
            "unit": meta["unit"],
        }
        if prev:
            row["points_lost_previous_week"] = _r1(prev["penalties"][name])
            row["rate_previous_week"] = _r1(prev["rates"][name])
            delta = row["points_lost"] - row["points_lost_previous_week"]
            row["trend"] = "worse" if delta >= 1 else "better" if delta <= -1 else "about the same"
        factors.append(row)
    factors.sort(key=lambda r: -r["points_lost"])
    costly = [f for f in factors if f["points_lost"] >= MIN_POINTS]

    facts: dict[str, Any] = {
        "driver": {
            "first_name": profile.get("first_name"),
            "vehicle": " ".join(str(profile["vehicle"][k]) for k in ("year", "make", "model"))
            if profile.get("vehicle") else None,
            "home_metro": profile.get("home_metro"),
        },
        "week": {
            "id": current["week_id"],
            "dates": week_dates(current["week_id"]),
            "score": cur["score"],
            "band": BAND_LABELS[band(cur["score"])],
            "trips": int(current.get("trips", 0)),
            "miles": round(float(current.get("miles", 0))),
        },
        "previous_week": None,
        "score_change": None,
        "direction": "first week",
        "factors": factors,
        "costly_factors": [f["factor"] for f in costly],
        "strengths": [f["label"] for f in factors if f["points_lost"] < MIN_POINTS],
        "pattern": _pattern(costly[0]["factor"], week_trips) if costly else None,
        "worst_trips": [],
    }
    if prev:
        change = _r1(cur["score"] - prev["score"])
        facts["previous_week"] = {
            "id": previous["week_id"],
            "dates": week_dates(previous["week_id"]),
            "score": prev["score"],
            "band": BAND_LABELS[band(prev["score"])],
        }
        facts["score_change"] = change
        facts["direction"] = ("up" if change >= STEADY_THRESHOLD else
                              "down" if change <= -STEADY_THRESHOLD else "held steady")
        moved = max(factors, key=lambda r: abs(r["points_lost"] - r["points_lost_previous_week"]))
        diff = _r1(moved["points_lost"] - moved["points_lost_previous_week"])
        if abs(diff) >= 1:
            facts["biggest_change"] = {"factor": moved["factor"], "label": moved["label"],
                                       "points_lost_change": diff}

    worst = sorted((t for t in week_trips if float(t.get("miles", 0)) >= 2), key=lambda t: float(t["score"]))
    for t in worst[:2]:
        if float(t["score"]) >= 90:
            break
        main = max(t["penalties"], key=lambda k: float(t["penalties"][k]))
        facts["worst_trips"].append({
            "when": _trip_when(t["local_start"]),
            "miles": _r1(t["miles"]),
            "score": _r1(t["score"]),
            "main_issue": FACTORS[main]["label"],
            "detail": _factor_detail(main, t["totals"]),
        })
    return facts


def facts_hash(facts: dict) -> str:
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- numbers

_NUM = re.compile(r"\d+(?:\.\d+)?")


def allowed_numbers(facts: dict) -> set[float]:
    """Every number that appears anywhere in the facts, as values or inside strings."""
    found: set[float] = set()

    def walk(v: Any) -> None:
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            found.add(abs(float(v)))
        elif isinstance(v, str):
            found.update(float(n) for n in _NUM.findall(v))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(facts)
    return found


def numbers_in(text: str) -> list[float]:
    return [float(n) for n in _NUM.findall(text)]


def number_is_grounded(x: float, allowed: set[float]) -> bool:
    """True if x matches a fact number, allowing rounding to the nearest whole number."""
    return any(abs(x - a) <= 0.51 for a in allowed)
