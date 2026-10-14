from datetime import date

import pytest

from score import WEIGHTS, band, score_from_totals, score_trip, trip_totals, week_id


def _trip(samples=None, events=None, start="2026-10-07T14:00:00Z", offset=-360):
    if samples is None:
        # 30 minutes at 60 mph in a 65 zone = 30 miles, no speeding
        samples = [{"t": i * 10, "lat": 0, "lon": 0, "spd": 60.0, "lim": 65} for i in range(180)]
    return {
        "trip_id": "trip-1", "driver_id": "DRV-0001", "vehicle_id": "VEH-0001",
        "start_time": start, "end_time": "2026-10-07T14:30:00Z", "utc_offset_minutes": offset,
        "sample_interval_s": 10, "samples": samples, "events": events or [],
    }


def test_clean_trip_scores_100():
    s = score_trip(_trip())
    assert s["score"] == 100.0
    assert s["band"] == "excellent"
    assert s["miles"] == pytest.approx(30.0, abs=0.01)
    assert all(v == 0 for v in s["penalties"].values())


def test_hard_brake_penalty_is_rate_based():
    events = [{"type": "hard_brake", "t": 100}] * 3  # 3 per 30 miles = 10 per 100 miles
    s = score_trip(_trip(events=events))
    assert s["rates"]["hard_braking"] == pytest.approx(10.0, abs=0.01)
    assert s["penalties"]["hard_braking"] == pytest.approx(10.0 * WEIGHTS["hard_braking"][0], abs=0.1)


def test_penalties_are_capped():
    events = [{"type": "hard_brake", "t": 100}] * 200
    s = score_trip(_trip(events=events))
    assert s["penalties"]["hard_braking"] == WEIGHTS["hard_braking"][1]


def test_short_trip_uses_minimum_exposure():
    samples = [{"t": i * 10, "lat": 0, "lon": 0, "spd": 30.0, "lim": 30} for i in range(12)]  # 1 mile
    s = score_trip(_trip(samples=samples, events=[{"type": "hard_brake", "t": 10}]))
    assert s["rates"]["hard_braking"] == pytest.approx(10.0)  # 1 per 10-mile floor


def test_speeding_share_of_moving_time():
    samples = [{"t": i * 10, "lat": 0, "lon": 0, "spd": 80.0 if i < 45 else 60.0, "lim": 65}
               for i in range(180)]
    s = score_trip(_trip(samples=samples))
    assert s["rates"]["speeding"] == pytest.approx(25.0)
    assert s["penalties"]["speeding"] == 20.0  # capped


def test_phone_minutes_per_hour():
    events = [{"type": "phone_use", "t": 50, "duration_s": 90}]  # 1.5 min in a 0.5 h trip
    s = score_trip(_trip(events=events))
    assert s["rates"]["phone_handling"] == pytest.approx(3.0)


def test_night_uses_local_time():
    # 07:00Z with UTC-6 is 01:00 local -> all night driving
    s = score_trip(_trip(start="2026-10-07T07:00:00Z"))
    assert s["rates"]["late_night"] == pytest.approx(100.0)
    assert s["local_start"] == "2026-10-07T01:00:00"
    # 14:00Z is 08:00 local -> none
    assert score_trip(_trip())["rates"]["late_night"] == 0


def test_possible_crash_not_penalized_and_high_res_stripped():
    crash = {"type": "possible_crash", "t": 900, "ts": "x", "impact_g": 6.0, "high_res": [{"g": -6}]}
    s = score_trip(_trip(events=[crash]))
    assert s["score"] == 100.0
    assert s["totals"]["possible_crash_count"] == 1
    assert "high_res" not in s["possible_crashes"][0]


def test_weekly_score_from_summed_totals_matches_combined_trip():
    t1 = trip_totals(_trip(events=[{"type": "hard_brake", "t": 1}]))
    t2 = trip_totals(_trip())
    week = {k: t1[k] + t2[k] for k in t1}
    assert week["trips"] == 2
    assert score_from_totals(week)["rates"]["hard_braking"] == pytest.approx(100 / 60, abs=0.01)


def test_week_id_and_band():
    assert week_id(date(2026, 10, 13)) == "2026-W42"
    assert week_id(date(2026, 1, 1)) == "2026-W01"
    assert [band(s) for s in (95, 80, 65, 40)] == ["excellent", "good", "fair", "needs_attention"]
