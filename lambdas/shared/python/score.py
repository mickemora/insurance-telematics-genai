"""Rule-based driving score.

Deliberately simple and transparent: every point deducted is attributed to a
named factor in a ``penalties`` breakdown. That breakdown is what the Day 2
gen AI explainer turns into plain-English coaching, so it must stay
explainable rather than clever.

The same ``score_from_totals`` function scores a single trip and a whole week:
weekly scores are computed from summed exposure and event counters, not by
averaging trip scores.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

SCORE_MODEL_VERSION = "v1"

SPEEDING_MARGIN_MPH = 10      # "speeding" = more than this over the posted limit
MOVING_MPH = 5                # samples below this count as stopped
NIGHT_HOURS = range(0, 4)     # local midnight to 4 AM
MIN_EXPOSURE_MILES = 10.0     # floor for per-100-mile rates, so tiny trips don't explode

# factor -> (points per unit, cap, unit description)
WEIGHTS: dict[str, tuple[float, float, str]] = {
    "hard_braking": (2.0, 25.0, "per hard brake per 100 miles"),
    "rapid_acceleration": (1.5, 15.0, "per rapid acceleration per 100 miles"),
    "harsh_cornering": (1.5, 10.0, "per harsh corner per 100 miles"),
    "speeding": (1.0, 20.0, "per % of moving time >10 mph over the limit"),
    "phone_handling": (4.0, 25.0, "per minute of phone handling per driving hour"),
    "late_night": (0.25, 10.0, "per % of driving time between midnight and 4 AM"),
}

EVENT_TO_COUNTER = {
    "hard_brake": "hard_brake_count",
    "rapid_accel": "rapid_accel_count",
    "harsh_corner": "harsh_corner_count",
    "possible_crash": "possible_crash_count",
}

TOTAL_FIELDS = (
    "trips", "miles", "drive_seconds", "moving_seconds", "speeding_seconds",
    "night_seconds", "phone_seconds", "hard_brake_count", "rapid_accel_count",
    "harsh_corner_count", "possible_crash_count",
)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def week_id(d) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def band(score: float) -> str:
    if score >= 90:
        return "excellent"
    if score >= 75:
        return "good"
    if score >= 60:
        return "fair"
    return "needs_attention"


def trip_totals(trip: dict) -> dict[str, float]:
    """Exposure and event counters for one trip (the unit summed into weeks)."""
    samples = trip["samples"]
    interval = trip.get("sample_interval_s", 10)
    start = _parse(trip["start_time"])
    offset = timedelta(minutes=trip.get("utc_offset_minutes", 0))

    miles = sum(s["spd"] for s in samples) * interval / 3600.0
    moving = [s for s in samples if s["spd"] > MOVING_MPH]
    speeding = [s for s in moving if s["spd"] > s["lim"] + SPEEDING_MARGIN_MPH]
    night = [s for s in samples if (start + offset + timedelta(seconds=s["t"])).hour in NIGHT_HOURS]

    totals: dict[str, float] = {f: 0 for f in TOTAL_FIELDS}
    totals.update({
        "trips": 1,
        "miles": round(miles, 3),
        "drive_seconds": len(samples) * interval,
        "moving_seconds": len(moving) * interval,
        "speeding_seconds": len(speeding) * interval,
        "night_seconds": len(night) * interval,
    })
    for ev in trip.get("events", []):
        if ev["type"] == "phone_use":
            totals["phone_seconds"] += ev.get("duration_s", 0)
        elif ev["type"] in EVENT_TO_COUNTER:
            totals[EVENT_TO_COUNTER[ev["type"]]] += 1
    return totals


def score_from_totals(t: dict[str, Any]) -> dict[str, Any]:
    """Score 0-100 plus the per-factor rates and penalties behind it."""
    miles = float(t.get("miles", 0))
    exposure = max(miles, MIN_EXPOSURE_MILES)
    drive_s = float(t.get("drive_seconds", 0)) or 1.0
    moving_s = float(t.get("moving_seconds", 0)) or 1.0

    rates = {
        "hard_braking": float(t.get("hard_brake_count", 0)) / exposure * 100,
        "rapid_acceleration": float(t.get("rapid_accel_count", 0)) / exposure * 100,
        "harsh_cornering": float(t.get("harsh_corner_count", 0)) / exposure * 100,
        "speeding": float(t.get("speeding_seconds", 0)) / moving_s * 100,
        "phone_handling": (float(t.get("phone_seconds", 0)) / 60) / (drive_s / 3600),
        "late_night": float(t.get("night_seconds", 0)) / drive_s * 100,
    }
    penalties = {}
    for factor, (per_unit, cap, _) in WEIGHTS.items():
        penalties[factor] = round(min(cap, rates[factor] * per_unit), 1)
    score = round(max(0.0, 100.0 - sum(penalties.values())), 1)
    return {
        "score": score,
        "band": band(score),
        "rates": {k: round(v, 2) for k, v in rates.items()},
        "penalties": penalties,
        "model_version": SCORE_MODEL_VERSION,
    }


def score_trip(trip: dict) -> dict[str, Any]:
    """Everything the scoring Lambda stores for one trip."""
    totals = trip_totals(trip)
    scored = score_from_totals(totals)
    local_start = _parse(trip["start_time"]) + timedelta(minutes=trip.get("utc_offset_minutes", 0))
    crashes = [
        {k: v for k, v in ev.items() if k != "high_res"}
        for ev in trip.get("events", []) if ev["type"] == "possible_crash"
    ]
    return {
        "trip_id": trip["trip_id"],
        "driver_id": trip["driver_id"],
        "vehicle_id": trip.get("vehicle_id"),
        "start_time": trip["start_time"],
        "end_time": trip["end_time"],
        "local_start": local_start.strftime("%Y-%m-%dT%H:%M:%S"),
        "local_date": local_start.date().isoformat(),
        "week_id": week_id(local_start.date()),
        "miles": round(totals["miles"], 2),
        "duration_min": round(totals["drive_seconds"] / 60, 1),
        "totals": totals,
        "possible_crashes": crashes,
        **scored,
    }
