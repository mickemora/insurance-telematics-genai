#!/usr/bin/env python3
"""Demo helper for the telematics stack.

    python scripts/demo.py generate            # invoke the generator Lambda, wait for scoring
    python scripts/demo.py scores              # weekly score per driver
    python scripts/demo.py trips DRV-0002      # a driver's recent trips with penalty breakdown
    python scripts/demo.py crashes             # trips that raised a possible crash
    python scripts/demo.py local               # same pipeline offline, no AWS needed

AWS commands use your normal AWS credentials/region (AWS_PROFILE, AWS_REGION)
and look up resource names from the CloudFormation stack outputs.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lambdas" / "generator"))
sys.path.insert(0, str(ROOT / "lambdas" / "scoring"))

from score import TOTAL_FIELDS, score_from_totals, score_trip  # noqa: E402
from synth import generate as synth_generate  # noqa: E402

STACK_NAME = "InsuranceTelematicsGenAI"


# --------------------------------------------------------------------------- AWS helpers

def _boto3():
    try:
        import boto3
    except ImportError:
        sys.exit("boto3 is required for AWS commands: pip install -r requirements-dev.txt")
    return boto3


def stack_outputs() -> dict[str, str]:
    cfn = _boto3().client("cloudformation")
    try:
        stack = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"][0]
    except Exception as err:  # noqa: BLE001
        sys.exit(f"Could not read stack {STACK_NAME}: {err}\nDeploy it first with `cdk deploy`.")
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def _query_driver(table, driver_id: str) -> list[dict]:
    items, kwargs = [], {"KeyConditionExpression": "driver_id = :d",
                         "ExpressionAttributeValues": {":d": driver_id}}
    while True:
        resp = table.query(**kwargs)
        items += resp["Items"]
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _scan(table, **kwargs) -> list[dict]:
    items = []
    while True:
        resp = table.scan(**kwargs)
        items += resp["Items"]
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


# --------------------------------------------------------------------------- printing

def print_weekly(rows: dict[str, dict], names: dict[str, str], extra: dict[str, str] | None = None) -> None:
    """rows: driver_id -> {week_id: totals}."""
    weeks = sorted({w for per in rows.values() for w in per})
    header = f"{'driver':<10} {'name':<11}" + "".join(f"{w:>10}" for w in weeks) + "  top factor (latest week)"
    if extra:
        header += "   [truth]"
    print(header)
    print("-" * len(header))
    for driver_id in sorted(rows):
        per = rows[driver_id]
        cells, latest = "", None
        for w in weeks:
            if w in per:
                latest = score_from_totals(per[w])
                cells += f"{latest['score']:>10.1f}"
            else:
                cells += f"{'-':>10}"
        top = ""
        if latest:
            factor, pts = max(latest["penalties"].items(), key=lambda kv: kv[1])
            top = f"{factor} (-{pts:.0f})" if pts >= 1 else "none"
        line = f"{driver_id:<10} {names.get(driver_id, ''):<11}{cells}  {top:<26}"
        if extra:
            line += f" {extra.get(driver_id, '')}"
        print(line)


def print_trip(item: dict) -> None:
    penalties = {k: float(v) for k, v in item["penalties"].items() if float(v) >= 0.5}
    pen = ", ".join(f"{k} -{v:.0f}" for k, v in sorted(penalties.items(), key=lambda kv: -kv[1])) or "none"
    crash = "  POSSIBLE CRASH" if item.get("possible_crashes") else ""
    print(f"{item['local_start']}  {float(item['miles']):6.1f} mi  score {float(item['score']):5.1f}  [{pen}]{crash}")


# --------------------------------------------------------------------------- commands

def cmd_generate(args) -> None:
    boto3 = _boto3()
    out = stack_outputs()
    payload = {"drivers": args.drivers, "days": args.days, "seed": args.seed,
               "crashes": args.crashes, "false_alarms": args.false_alarms, "reset": not args.no_reset}
    print(f"Invoking {out['GeneratorFunctionName']} with {json.dumps(payload)} ...")
    resp = boto3.client("lambda").invoke(FunctionName=out["GeneratorFunctionName"],
                                         Payload=json.dumps(payload).encode())
    result = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        sys.exit(f"Generator failed: {json.dumps(result, indent=2)}")
    print(json.dumps(result, indent=2))

    if args.no_wait:
        return
    sqs = boto3.client("sqs")
    print("Waiting for the scoring queue to drain", end="", flush=True)
    idle = 0
    for _ in range(180):
        attrs = sqs.get_queue_attributes(
            QueueUrl=out["TripQueueUrl"],
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        pending = int(attrs["ApproximateNumberOfMessages"]) + int(attrs["ApproximateNumberOfMessagesNotVisible"])
        idle = idle + 1 if pending == 0 else 0
        if idle >= 3:  # stable for a few polls (S3 notifications can trail the generator)
            print(" done.")
            break
        print(".", end="", flush=True)
        time.sleep(5)
    else:
        print("\nStill scoring; run `python scripts/demo.py scores` in a minute.")
    dlq = sqs.get_queue_attributes(QueueUrl=out["DeadLetterQueueUrl"],
                                   AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]
    if int(dlq["ApproximateNumberOfMessages"]):
        print(f"WARNING: {dlq['ApproximateNumberOfMessages']} message(s) in the dead-letter queue; "
              "check the scoring function logs.")


def cmd_scores(args) -> None:
    table = _boto3().resource("dynamodb").Table(stack_outputs()["TableName"])
    items = _scan(table, FilterExpression="entity IN (:p, :w)",
                  ExpressionAttributeValues={":p": "profile", ":w": "week"})
    rows: dict[str, dict] = defaultdict(dict)
    names: dict[str, str] = {}
    for it in items:
        if it["entity"] == "profile":
            names[it["driver_id"]] = f"{it['first_name']} {it['last_initial']}."
        else:
            rows[it["driver_id"]][it["week_id"]] = it
    if not rows:
        sys.exit("No weekly scores yet. Run `python scripts/demo.py generate` first.")
    print_weekly(rows, names)


def cmd_trips(args) -> None:
    table = _boto3().resource("dynamodb").Table(stack_outputs()["TableName"])
    items = _query_driver(table, args.driver_id)
    profile = next((i for i in items if i["sk"] == "PROFILE"), None)
    if not profile:
        sys.exit(f"No driver {args.driver_id}")
    v = profile["vehicle"]
    print(f"{profile['first_name']} {profile['last_initial']}. ({args.driver_id}), "
          f"{v['year']} {v['make']} {v['model']}, {profile['home_metro']}")
    trips = sorted((i for i in items if i["entity"] == "trip"), key=lambda i: i["sk"])[-args.limit:]
    for t in trips:
        print_trip(t)


def cmd_crashes(args) -> None:
    table = _boto3().resource("dynamodb").Table(stack_outputs()["TableName"])
    items = _scan(table, FilterExpression="entity = :t AND size(possible_crashes) > :z",
                  ExpressionAttributeValues={":t": "trip", ":z": 0})
    if not items:
        print("No possible crashes detected.")
    for it in sorted(items, key=lambda i: i["start_time"]):
        for c in it["possible_crashes"]:
            print(f"{it['driver_id']}  {c['ts']}  impact {float(c['impact_g']):.1f}g "
                  f"{c['impact_direction']:<5}  {float(c['speed_mph']):.0f}->{float(c['speed_after_mph']):.0f} mph  "
                  f"stationary {int(c['stationary_after_s'])}s  confidence {float(c['detector_confidence']):.2f}")
            print(f"           raw: {it['raw_s3_key']}")


def cmd_local(args) -> None:
    data = synth_generate(drivers=args.drivers, days=args.days, seed=args.seed,
                          crashes=args.crashes, false_alarms=args.false_alarms)
    out = ROOT / args.out
    (out / "raw").mkdir(parents=True, exist_ok=True)
    rows: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: {f: 0 for f in TOTAL_FIELDS}))
    crashes = []
    for trip in data["trips"]:
        (out / "raw" / f"{trip['driver_id']}_{trip['start_time'][:10]}_{trip['trip_id'][:8]}.json").write_text(
            json.dumps(trip))
        scored = score_trip(trip)
        week = rows[trip["driver_id"]][scored["week_id"]]
        for f in TOTAL_FIELDS:
            week[f] += scored["totals"][f]
        crashes += [(trip["driver_id"], c) for c in scored["possible_crashes"]]
    (out / "drivers.json").write_text(json.dumps(data["profiles"], indent=2))
    (out / "ground_truth.json").write_text(json.dumps(data["manifest"], indent=2))

    names = {p["driver_id"]: f"{p['first_name']} {p['last_initial']}." for p in data["profiles"]}
    truth = {t["driver_id"]: f"{t['persona']}, trend {t['weekly_trend']:+.2f}/wk"
             for t in data["manifest"]["driver_truth"]}
    print(f"{len(data['trips'])} trips for {len(data['profiles'])} drivers written to {out}\n")
    print_weekly(rows, names, extra=truth)
    print("\nPossible crashes detected:")
    planted = {p["trip_id"]: p for p in data["manifest"]["planted_events"]}
    for driver_id, c in crashes:
        print(f"  {driver_id} {c['ts']} impact {c['impact_g']}g, {c['speed_mph']}->{c['speed_after_mph']} mph, "
              f"stationary {c['stationary_after_s']}s")
    print(f"  (ground truth: {sum(p['kind'] == 'crash' for p in planted.values())} real, "
          f"{sum(p['kind'] == 'false_alarm' for p in planted.values())} false alarms)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def gen_args(sp):
        sp.add_argument("--drivers", type=int, default=20)
        sp.add_argument("--days", type=int, default=28)
        sp.add_argument("--seed", type=int, default=42)
        sp.add_argument("--crashes", type=int, default=3)
        sp.add_argument("--false-alarms", type=int, default=2)

    g = sub.add_parser("generate", help="invoke the deployed generator and wait for scoring")
    gen_args(g)
    g.add_argument("--no-reset", action="store_true", help="keep previously generated data")
    g.add_argument("--no-wait", action="store_true")
    g.set_defaults(func=cmd_generate)

    sub.add_parser("scores", help="weekly score per driver").set_defaults(func=cmd_scores)

    t = sub.add_parser("trips", help="a driver's recent trips")
    t.add_argument("driver_id")
    t.add_argument("--limit", type=int, default=15)
    t.set_defaults(func=cmd_trips)

    sub.add_parser("crashes", help="trips with a possible crash").set_defaults(func=cmd_crashes)

    lo = sub.add_parser("local", help="run generator + scoring offline")
    gen_args(lo)
    lo.add_argument("--out", default="local_data")
    lo.set_defaults(func=cmd_local)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
