"""Synthetic telematics data generator.

Pure Python with no AWS dependencies, so it runs the same way inside the
generator Lambda, from scripts/demo.py on a laptop, and under pytest.

What it produces mirrors what a phone-based telematics SDK typically uploads:

* a trip header (driver, vehicle, start/end time),
* coarse GPS/speed samples every SAMPLE_INTERVAL_S seconds,
* discrete on-device events (hard brake, rapid acceleration, harsh cornering,
  phone handling, possible crash) with a high-resolution window for crashes.

Each driver gets a hidden *persona* and *trend* that shape their behaviour.
Those hidden attributes, plus which "possible crash" events are real, are
returned in a manifest (ground truth) that is kept separate from the raw data,
so later features (score explainer, crash triage) can be checked against it.
"""
from __future__ import annotations

import math
import random
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

SAMPLE_INTERVAL_S = 10
SDK_VERSION = "3.4.0"

# Standard-time UTC offsets (DST ignored on purpose; this is synthetic data).
METROS = [
    {"name": "San Antonio, TX", "lat": 29.4241, "lon": -98.4936, "utc_offset_minutes": -360},
    {"name": "Plano, TX", "lat": 33.0198, "lon": -96.6989, "utc_offset_minutes": -360},
    {"name": "Norfolk, VA", "lat": 36.8508, "lon": -76.2859, "utc_offset_minutes": -300},
    {"name": "San Diego, CA", "lat": 32.7157, "lon": -117.1611, "utc_offset_minutes": -480},
    {"name": "Colorado Springs, CO", "lat": 38.8339, "lon": -104.8214, "utc_offset_minutes": -420},
    {"name": "Fayetteville, NC", "lat": 35.0527, "lon": -78.8784, "utc_offset_minutes": -300},
]

FIRST_NAMES = [
    "Maria", "James", "Aisha", "Daniel", "Priya", "Marcus", "Elena", "Tyrell",
    "Grace", "Luis", "Hannah", "Kenji", "Sofia", "Andre", "Nadia", "Caleb",
    "Rosa", "Omar", "Leah", "Victor", "Imani", "Ethan", "Mei", "Jordan",
]
LAST_INITIALS = "ABCDEFGHJKLMNPRSTVW"

VEHICLES = [
    ("Toyota", "RAV4"), ("Ford", "F-150"), ("Honda", "Civic"), ("Chevrolet", "Silverado"),
    ("Tesla", "Model 3"), ("Jeep", "Wrangler"), ("Subaru", "Outback"), ("Hyundai", "Tucson"),
    ("Nissan", "Altima"), ("Kia", "Telluride"),
]

# Event rates are per 100 miles; phone is minutes of handling per driving hour;
# speeding_share is the steady-state share of time spent >10 mph over the limit.
PERSONAS: dict[str, dict[str, Any]] = {
    "steady": dict(hard_brake=0.8, rapid_accel=0.5, harsh_corner=0.4, speeding_share=0.02,
                   phone_min_per_hr=0.1, night_prob=0.03, trips_per_day=(1, 3), highway_share=0.4),
    "rusher": dict(hard_brake=7.0, rapid_accel=6.0, harsh_corner=2.0, speeding_share=0.20,
                   phone_min_per_hr=0.5, night_prob=0.05, trips_per_day=(2, 4), highway_share=0.6),
    "distracted": dict(hard_brake=3.0, rapid_accel=1.5, harsh_corner=1.0, speeding_share=0.05,
                       phone_min_per_hr=4.0, night_prob=0.05, trips_per_day=(1, 3), highway_share=0.4),
    "night_owl": dict(hard_brake=2.0, rapid_accel=2.5, harsh_corner=1.2, speeding_share=0.12,
                      phone_min_per_hr=0.6, night_prob=0.45, trips_per_day=(1, 3), highway_share=0.5),
    "new_driver": dict(hard_brake=5.0, rapid_accel=2.0, harsh_corner=4.5, speeding_share=0.06,
                       phone_min_per_hr=1.0, night_prob=0.08, trips_per_day=(0, 2), highway_share=0.15),
}

ROAD_LIMITS = {"residential": (25, 30), "arterial": (40, 45), "highway": (65, 70)}

CRASH_SCENARIOS = {
    # impact_direction, typical speed before impact (mph), whether driver braked first
    "rear_end_striking": {"impact_direction": "front", "speed": (18, 40), "pre_brake": True},
    "rear_end_struck": {"impact_direction": "rear", "speed": (0, 8), "pre_brake": False},
    "intersection_side": {"impact_direction": "left", "speed": (15, 35), "pre_brake": True},
    "single_vehicle": {"impact_direction": "front", "speed": (25, 50), "pre_brake": False},
}


# --------------------------------------------------------------------------- helpers

def _poisson(rng: random.Random, lam: float) -> int:
    """Knuth's algorithm; fine for the small lambdas used here."""
    if lam <= 0:
        return 0
    if lam > 30:  # normal approximation for large lambda
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    threshold, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= threshold:
            return k
        k += 1


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _move(lat: float, lon: float, heading_deg: float, miles: float) -> tuple[float, float]:
    h = math.radians(heading_deg)
    dlat = miles * math.cos(h) / 69.0
    dlon = miles * math.sin(h) / (69.0 * max(0.2, math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon


# --------------------------------------------------------------------------- drivers

def make_drivers(rng: random.Random, n: int, end_date: date) -> tuple[list[dict], list[dict]]:
    """Return (public profiles, hidden ground truth) for n drivers."""
    persona_cycle = ["steady", "rusher", "distracted", "night_owl", "new_driver", "steady"]
    profiles, truth = [], []
    for i in range(n):
        driver_id = f"DRV-{i + 1:04d}"
        persona = persona_cycle[i % len(persona_cycle)]
        # A few drivers clearly improve or decline week over week (good explainer stories).
        if i % 7 == 2:
            trend = -0.22  # improving
        elif i % 7 == 5:
            trend = 0.25  # declining
        else:
            trend = round(rng.uniform(-0.05, 0.05), 3)
        metro = METROS[i % len(METROS)]
        make, model = rng.choice(VEHICLES)
        home_lat = metro["lat"] + rng.uniform(-0.12, 0.12)
        home_lon = metro["lon"] + rng.uniform(-0.12, 0.12)
        profiles.append({
            "driver_id": driver_id,
            "first_name": FIRST_NAMES[i % len(FIRST_NAMES)],
            "last_initial": rng.choice(LAST_INITIALS),
            "policy_id": f"POL-{rng.randint(100000, 999999)}",
            "vehicle": {
                "vehicle_id": f"VEH-{i + 1:04d}",
                "year": rng.randint(2015, 2026),
                "make": make,
                "model": model,
            },
            "home_metro": metro["name"],
            "utc_offset_minutes": metro["utc_offset_minutes"],
            "home": {"lat": round(home_lat, 5), "lon": round(home_lon, 5)},
            "enrolled_on": (end_date - timedelta(days=rng.randint(60, 900))).isoformat(),
        })
        truth.append({"driver_id": driver_id, "persona": persona, "weekly_trend": trend})
    return profiles, truth


# --------------------------------------------------------------------------- trips

def _route_plan(rng: random.Random, highway: bool) -> list[tuple[str, float]]:
    if highway:
        total = rng.uniform(9, 35)
        return [("residential", 0.4), ("arterial", 1.5), ("highway", total - 3.8),
                ("arterial", 1.5), ("residential", 0.4)]
    total = rng.uniform(2.0, 10.0)
    return [("residential", 0.5), ("arterial", total - 1.0), ("residential", 0.5)]


def _trip_starts(rng: random.Random, persona: dict, n: int) -> list[float]:
    """Local start hours for the day's trips, sorted."""
    hours = []
    for k in range(n):
        if rng.random() < persona["night_prob"]:
            # late night: 23:00-03:30 (values >=24 roll into the next day)
            hours.append(rng.uniform(23.0, 27.5))
        elif k == 0:
            hours.append(rng.uniform(6.5, 9.0))  # morning commute
        elif k == 1:
            hours.append(rng.uniform(16.0, 18.5))  # evening commute
        else:
            hours.append(rng.uniform(10.0, 21.0))  # errands
    return sorted(hours)


def _build_trip(rng: random.Random, profile: dict, persona: dict, factor: float,
                start_utc: datetime) -> dict:
    highway = rng.random() < persona["highway_share"]
    plan = _route_plan(rng, highway)
    limits = {road: rng.choice(opts) for road, opts in ROAD_LIMITS.items()}

    lat = profile["home"]["lat"] + rng.uniform(-0.03, 0.03)
    lon = profile["home"]["lon"] + rng.uniform(-0.03, 0.03)
    heading = rng.uniform(0, 360)

    speeding_share = min(0.6, persona["speeding_share"] * factor)
    p_exit = 0.25
    p_enter = p_exit * speeding_share / max(1e-6, 1 - speeding_share)
    speeding = False

    samples: list[dict] = []
    t = 0
    speed = 0.0
    dt_hr = SAMPLE_INTERVAL_S / 3600.0
    for road, miles in plan:
        limit = limits[road]
        remaining = miles
        while remaining > 0:
            speeding = (rng.random() > p_exit) if speeding else (rng.random() < p_enter)
            target = limit + (rng.uniform(11, 22) if speeding else rng.gauss(-3, 3))
            if rng.random() < 0.04:  # traffic light / congestion dip
                target = rng.uniform(0, limit * 0.4)
            speed = max(0.0, 0.5 * speed + 0.5 * target)  # smooth acceleration
            samples.append({"t": t, "lat": round(lat, 5), "lon": round(lon, 5),
                            "spd": round(speed, 1), "lim": limit})
            step = speed * dt_hr
            lat, lon = _move(lat, lon, heading, step)
            heading = (heading + rng.gauss(0, 12)) % 360
            remaining -= max(step, 0.002)  # always make progress even when stopped
            t += SAMPLE_INTERVAL_S
    # park
    samples.append({"t": t, "lat": round(lat, 5), "lon": round(lon, 5), "spd": 0.0,
                    "lim": limits["residential"]})

    miles = sum(s["spd"] * dt_hr for s in samples)
    hours = t / 3600.0
    moving_idx = [i for i, s in enumerate(samples) if s["spd"] > 5] or [0]

    events: list[dict] = []

    def add_event(kind: str, **extra: Any) -> None:
        idx = rng.choice(moving_idx)
        s = samples[idx]
        ev = {"type": kind, "t": s["t"], "ts": _iso(start_utc + timedelta(seconds=s["t"])),
              "lat": s["lat"], "lon": s["lon"], "speed_mph": s["spd"]}
        ev.update(extra)
        events.append(ev)

    for _ in range(_poisson(rng, persona["hard_brake"] * factor * miles / 100)):
        add_event("hard_brake", peak_g=round(-rng.uniform(0.35, 0.65), 2))
    for _ in range(_poisson(rng, persona["rapid_accel"] * factor * miles / 100)):
        add_event("rapid_accel", peak_g=round(rng.uniform(0.30, 0.50), 2))
    for _ in range(_poisson(rng, persona["harsh_corner"] * factor * miles / 100)):
        add_event("harsh_corner", peak_lateral_g=round(rng.uniform(0.35, 0.60), 2))
    phone_minutes = persona["phone_min_per_hr"] * factor * hours
    for _ in range(_poisson(rng, phone_minutes)):  # one session per minute; sessions avg ~1 min
        add_event("phone_use", duration_s=int(rng.uniform(15, 120)),
                  interaction=rng.choice(["handheld_call", "screen_tap", "texting"]))
    events.sort(key=lambda e: e["t"])

    end_utc = start_utc + timedelta(seconds=t)
    return {
        "schema_version": 1,
        "trip_id": _uuid(rng),
        "driver_id": profile["driver_id"],
        "vehicle_id": profile["vehicle"]["vehicle_id"],
        "source": "phone_sdk",
        "sdk_version": SDK_VERSION,
        "start_time": _iso(start_utc),
        "end_time": _iso(end_utc),
        "utc_offset_minutes": profile["utc_offset_minutes"],
        "sample_interval_s": SAMPLE_INTERVAL_S,
        "samples": samples,
        "events": events,
    }


# --------------------------------------------------------------------------- crashes

def _high_res_window(rng: random.Random, speed_before: float, pre_brake: bool,
                     impact_g: float, stops: bool, speed_after: float) -> list[dict]:
    """1 Hz window from -20 s to +10 s around the impact."""
    window = []
    for off in range(-20, 11):
        if off < 0:
            spd = speed_before
            g = rng.gauss(0, 0.03)
            if pre_brake and off >= -2:
                spd = max(0.0, speed_before - 8 * (off + 3))
                g = -rng.uniform(0.5, 0.8)
        elif off == 0:
            spd = speed_before if not pre_brake else max(0.0, speed_before - 16)
            g = -impact_g
        else:
            spd = 0.0 if stops else speed_after
            g = rng.gauss(0, 0.03)
        window.append({"t_offset_s": off, "spd": round(max(0.0, spd), 1), "g": round(g, 2)})
    return window


def _plant_crash(rng: random.Random, trip: dict, scenario_name: str) -> dict:
    sc = CRASH_SCENARIOS[scenario_name]
    samples = trip["samples"]
    idx = int(len(samples) * rng.uniform(0.55, 0.85))
    speed_before = round(rng.uniform(*sc["speed"]), 1)
    impact_g = round(rng.uniform(3.5, 9.0), 1)
    s = samples[idx]
    s["spd"] = speed_before
    start = datetime.strptime(trip["start_time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    # Vehicle stops at the impact point and stays there for a few minutes, then the trip ends.
    stopped = []
    for k in range(1, 19):  # 3 minutes stationary
        stopped.append({"t": s["t"] + k * SAMPLE_INTERVAL_S, "lat": s["lat"], "lon": s["lon"],
                        "spd": 0.0, "lim": s["lim"]})
    trip["samples"] = samples[: idx + 1] + stopped
    trip["events"] = [e for e in trip["events"] if e["t"] < s["t"]]
    trip["events"].append({
        "type": "possible_crash",
        "t": s["t"],
        "ts": _iso(start + timedelta(seconds=s["t"])),
        "lat": s["lat"],
        "lon": s["lon"],
        "speed_mph": speed_before,
        "impact_g": impact_g,
        "impact_direction": sc["impact_direction"],
        "speed_after_mph": 0.0,
        "stationary_after_s": 18 * SAMPLE_INTERVAL_S,
        "detector_confidence": round(rng.uniform(0.80, 0.97), 2),
        "high_res": _high_res_window(rng, speed_before, sc["pre_brake"], impact_g, True, 0.0),
    })
    trip["end_time"] = _iso(start + timedelta(seconds=trip["samples"][-1]["t"]))
    return {"trip_id": trip["trip_id"], "driver_id": trip["driver_id"], "kind": "crash",
            "scenario": scenario_name, "ts": trip["events"][-1]["ts"]}


def _plant_false_alarm(rng: random.Random, trip: dict) -> dict:
    """Phone dropped in the footwell: a spike in g-force but the car keeps moving."""
    samples = trip["samples"]
    idx = int(len(samples) * rng.uniform(0.3, 0.7))
    s = samples[idx]
    start = datetime.strptime(trip["start_time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    speed = max(20.0, s["spd"])
    impact_g = round(rng.uniform(2.5, 4.5), 1)
    trip["events"].append({
        "type": "possible_crash",
        "t": s["t"],
        "ts": _iso(start + timedelta(seconds=s["t"])),
        "lat": s["lat"],
        "lon": s["lon"],
        "speed_mph": round(speed, 1),
        "impact_g": impact_g,
        "impact_direction": rng.choice(["front", "rear", "left", "right"]),
        "speed_after_mph": round(speed - rng.uniform(0, 3), 1),
        "stationary_after_s": 0,
        "detector_confidence": round(rng.uniform(0.35, 0.65), 2),
        "high_res": _high_res_window(rng, speed, False, impact_g, False, speed - 1),
    })
    trip["events"].sort(key=lambda e: e["t"])
    return {"trip_id": trip["trip_id"], "driver_id": trip["driver_id"], "kind": "false_alarm",
            "scenario": "phone_drop", "ts": trip["events"][-1]["ts"]}


# --------------------------------------------------------------------------- entry point

def generate(drivers: int = 20, days: int = 28, seed: int = 42, crashes: int = 3,
             false_alarms: int = 2, end_date: date | None = None) -> dict:
    """Generate a full synthetic dataset.

    Returns {"profiles": [...], "trips": [...], "manifest": {...}}.
    The same arguments always produce the same data.
    """
    if drivers < 1 or days < 1:
        raise ValueError("drivers and days must be >= 1")
    rng = random.Random(seed)
    # Default to two days ago (UTC) so every trip, including late-night trips that
    # roll past midnight in US time zones, is safely in the past.
    end_date = end_date or (datetime.now(timezone.utc).date() - timedelta(days=2))
    profiles, truth = make_drivers(rng, drivers, end_date)
    truth_by_id = {t["driver_id"]: t for t in truth}

    trips: list[dict] = []
    for profile in profiles:
        t = truth_by_id[profile["driver_id"]]
        persona = PERSONAS[t["persona"]]
        offset = timedelta(minutes=profile["utc_offset_minutes"])
        for d in range(days):
            day = end_date - timedelta(days=days - 1 - d)
            week_idx = d // 7
            factor = max(0.15, 1.0 + t["weekly_trend"] * week_idx)
            lo, hi = persona["trips_per_day"]
            n = rng.randint(lo, hi)
            if day.weekday() >= 5:  # weekends: fewer trips
                n = max(0, n - 1)
            for start_hour in _trip_starts(rng, persona, n):
                local_start = datetime.combine(day, time(0), tzinfo=timezone.utc) + timedelta(hours=start_hour)
                start_utc = local_start - offset
                trips.append(_build_trip(rng, profile, persona, factor, start_utc))

    # Plant crashes in the most recent week, on distinct drivers, on trips long enough to matter.
    planted: list[dict] = []
    recent_cut = _iso(datetime.combine(end_date - timedelta(days=6), time(0), tzinfo=timezone.utc))
    candidates = [tr for tr in trips if tr["start_time"] >= recent_cut and len(tr["samples"]) > 40]
    rng.shuffle(candidates)
    used_drivers: set[str] = set()
    scenarios = list(CRASH_SCENARIOS)
    for tr in candidates:
        if len([p for p in planted if p["kind"] == "crash"]) >= crashes:
            break
        if tr["driver_id"] in used_drivers:
            continue
        planted.append(_plant_crash(rng, tr, scenarios[len(planted) % len(scenarios)]))
        used_drivers.add(tr["driver_id"])
    for tr in candidates:
        if len([p for p in planted if p["kind"] == "false_alarm"]) >= false_alarms:
            break
        if tr["driver_id"] in used_drivers:
            continue
        planted.append(_plant_false_alarm(rng, tr))
        used_drivers.add(tr["driver_id"])

    trips.sort(key=lambda tr: (tr["driver_id"], tr["start_time"]))
    manifest = {
        "seed": seed,
        "drivers": drivers,
        "days": days,
        "end_date": end_date.isoformat(),
        "trip_count": len(trips),
        "driver_truth": truth,
        "planted_events": planted,
    }
    return {"profiles": profiles, "trips": trips, "manifest": manifest}
