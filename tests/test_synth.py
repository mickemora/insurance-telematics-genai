from collections import defaultdict
from datetime import date
from statistics import mean

from score import TOTAL_FIELDS, score_from_totals, score_trip
from synth import generate

END = date(2026, 10, 10)


def _data(**kw):
    params = dict(drivers=12, days=21, seed=7, crashes=3, false_alarms=2, end_date=END)
    params.update(kw)
    return generate(**params)


def test_same_seed_same_data():
    assert _data() == _data()


def test_different_seed_different_data():
    assert _data()["trips"][0]["trip_id"] != _data(seed=8)["trips"][0]["trip_id"]


def test_trip_shape():
    trip = _data()["trips"][0]
    for field in ("trip_id", "driver_id", "vehicle_id", "start_time", "end_time",
                  "utc_offset_minutes", "sample_interval_s", "samples", "events"):
        assert field in trip
    assert trip["samples"][0].keys() == {"t", "lat", "lon", "spd", "lim"}
    assert trip["start_time"] < trip["end_time"]


def test_planted_events_match_manifest():
    data = _data()
    planted = data["manifest"]["planted_events"]
    assert sum(p["kind"] == "crash" for p in planted) == 3
    assert sum(p["kind"] == "false_alarm" for p in planted) == 2
    trips = {t["trip_id"]: t for t in data["trips"]}
    for p in planted:
        events = [e for e in trips[p["trip_id"]]["events"] if e["type"] == "possible_crash"]
        assert len(events) == 1
        ev = events[0]
        assert len(ev["high_res"]) == 31
        if p["kind"] == "crash":
            assert ev["speed_after_mph"] == 0 and ev["stationary_after_s"] > 0
            # nothing happens after the crash
            assert all(e["t"] <= ev["t"] for e in trips[p["trip_id"]]["events"])
        else:
            assert ev["speed_after_mph"] > 15 and ev["stationary_after_s"] == 0


def test_crashes_on_distinct_drivers():
    planted = _data()["manifest"]["planted_events"]
    assert len({p["driver_id"] for p in planted}) == len(planted)


def test_ground_truth_not_in_raw_data():
    data = _data()
    for p in data["profiles"]:
        assert "persona" not in p and "weekly_trend" not in p
    assert "persona" not in data["trips"][0]


def test_personas_separate_in_scores():
    data = _data(drivers=18, days=28)
    persona = {t["driver_id"]: t["persona"] for t in data["manifest"]["driver_truth"]}
    totals = defaultdict(lambda: {f: 0 for f in TOTAL_FIELDS})
    for trip in data["trips"]:
        t = score_trip(trip)["totals"]
        for f in TOTAL_FIELDS:
            totals[trip["driver_id"]][f] += t[f]
    by_persona = defaultdict(list)
    for driver_id, t in totals.items():
        by_persona[persona[driver_id]].append(score_from_totals(t)["score"])
    assert mean(by_persona["steady"]) > 85
    assert mean(by_persona["rusher"]) < 70
    assert mean(by_persona["steady"]) > mean(by_persona["distracted"])
