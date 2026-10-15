#!/usr/bin/env python3
"""Demo helper for the telematics stack.

    python scripts/demo.py generate            # invoke the generator Lambda, wait for scoring
    python scripts/demo.py scores              # weekly score per driver
    python scripts/demo.py trips DRV-0002      # a driver's recent trips with penalty breakdown
    python scripts/demo.py crashes             # trips that raised a possible crash
    python scripts/demo.py explain DRV-0020    # Day 2: gen AI score explanation via the API
    python scripts/demo.py eval                # Day 2: explain every driver, check against ground truth
    python scripts/demo.py claims              # Day 3: claims opened by the crash workflow
    python scripts/demo.py claim CLM-1A2B3C4D  # Day 3: one claim with its FNOL draft
    python scripts/demo.py statement CLM-1A2B3C4D [--inconsistent | --text "..."]
    python scripts/demo.py eval-claims         # Day 3: triage + statement checks vs ground truth
    python scripts/demo.py site                # Day 4: web app URL and passcode user
    python scripts/demo.py ask "Summarize DRV-0020's last 10 trips"   # Day 4: Bedrock Agent copilot
    python scripts/demo.py verify-clean        # after `cdk destroy`: anything left behind?
    python scripts/demo.py local               # same pipeline offline, no AWS needed

AWS commands use your normal AWS credentials/region (AWS_PROFILE, AWS_REGION)
and look up resource names from the CloudFormation stack outputs.
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lambdas" / "generator"))
sys.path.insert(0, str(ROOT / "lambdas" / "shared" / "python"))

from crash import simulate_statement  # noqa: E402
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


_API: dict[str, str] = {}


def api_get(path: str, query: dict | None = None) -> dict:
    return api_request("GET", path, query)


def api_post(path: str, body: dict) -> dict:
    return api_request("POST", path, body=body)


def api_request(method: str, path: str, query: dict | None = None, body: dict | None = None,
                allow: tuple[int, ...] = ()) -> dict:
    """Call an API route with the demo API key (looked up once from the stack)."""
    if not _API:
        out = stack_outputs()
        if "ApiUrl" not in out:
            sys.exit("The stack has no API yet. Run `cdk deploy` to add the Day 2 resources.")
        key = _boto3().client("apigateway").get_api_key(apiKey=out["ApiKeyId"], includeValue=True)["value"]
        _API.update(url=out["ApiUrl"].rstrip("/"), key=key)
    url = f"{_API['url']}{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"x-api-key": _API["key"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        text = err.read().decode()[:300]
        if err.code in allow:
            return {"_status": err.code, **json.loads(text or "{}")}
        sys.exit(f"{method} {path} failed: HTTP {err.code} {text}")


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


def print_explanation(e: dict, show_facts: bool = False) -> None:
    f = e["facts"]
    prev = f.get("previous_week")
    change = f" ({f['score_change']:+g} vs {prev['dates']})" if prev else ""
    print(f"{e['driver_id']}  {f['driver']['first_name']}, {f['driver']['vehicle']}  |  "
          f"week {f['week']['dates']}: score {f['week']['score']:g}{change}\n")
    print(f"  {e['headline']}\n")
    print(textwrap.fill(e["summary"], 88, initial_indent="  ", subsequent_indent="  "))
    print()
    for i, t in enumerate(e["tips"], 1):
        print(textwrap.fill(f"{i}. [{t['factor']}] {t['tip']}", 88, initial_indent="  ", subsequent_indent="     "))
    usage = e.get("usage") or {}
    print(f"\n  source={e['source']}  model={e['model_id']}  guardrail={e['guardrail']}  "
          f"tokens={usage.get('inputTokens', '-')}/{usage.get('outputTokens', '-')}  "
          f"latency={e.get('latency_ms', '-')}ms  cached={e.get('cached')}")
    if e.get("fallback_reason"):
        print(f"  fallback reason: {e['fallback_reason']}")
    if show_facts:
        print("\nFacts sent to the model:\n" + json.dumps(f, indent=2))


def cmd_explain(args) -> None:
    e = api_get(f"/drivers/{args.driver_id}/explanation", {"refresh": "true"} if args.refresh else None)
    print_explanation(e, args.facts)


# Which factors should show up in coaching for each hidden persona.
EXPECTED_FACTORS = {
    "rusher": {"hard_braking", "rapid_acceleration", "speeding"},
    "distracted": {"phone_handling"},
    "night_owl": {"late_night", "speeding", "rapid_acceleration"},
    "new_driver": {"harsh_cornering", "hard_braking"},
}
DIRECTION_WORDS = {
    "up": ("up", "improv", "rose", "climb", "higher", "better", "gain"),
    "down": ("down", "drop", "fell", "slip", "lower", "decline", "dip"),
    "held steady": ("steady", "stable", "similar", "consistent", "same", "held"),
}


def _latest_ground_truth() -> dict:
    s3 = _boto3().client("s3")
    bucket = stack_outputs()["RawBucketName"]
    objs = s3.list_objects_v2(Bucket=bucket, Prefix="ground-truth/").get("Contents", [])
    if not objs:
        sys.exit("No ground-truth manifest found. Run `python scripts/demo.py generate` first.")
    latest = max(objs, key=lambda o: o["LastModified"])
    return json.loads(s3.get_object(Bucket=bucket, Key=latest["Key"])["Body"].read())


def cmd_eval(args) -> None:
    truth = {t["driver_id"]: t for t in _latest_ground_truth()["driver_truth"]}
    drivers = api_get("/drivers")["drivers"]
    if args.limit:
        drivers = drivers[: args.limit]
    print(f"Explaining {len(drivers)} drivers (refresh={not args.cached}) ...\n")
    header = f"{'driver':<9} {'persona':<11} {'score':>6} {'dir':<11} {'tip factors':<40} {'persona':<8} {'wording':<8} {'source':<9} {'guard':<10} {'ms':>6}"
    print(header)
    print("-" * len(header))
    rows = []
    for d in drivers:
        e = api_get(f"/drivers/{d['driver_id']}/explanation", None if args.cached else {"refresh": "true"})
        f = e["facts"]
        persona = truth.get(d["driver_id"], {}).get("persona", "?")
        tip_factors = [t["factor"] for t in e["tips"]]
        expected = EXPECTED_FACTORS.get(persona)
        persona_ok = None if expected is None else bool(expected & set(tip_factors))
        words = DIRECTION_WORDS.get(f["direction"])
        wording_ok = None if not words else any(w in e["summary"].lower() for w in words)
        rows.append({"source": e["source"], "guardrail": e["guardrail"], "persona_ok": persona_ok,
                     "wording_ok": wording_ok, "latency": e.get("latency_ms") or 0, "usage": e.get("usage") or {},
                     "reason": e.get("fallback_reason")})
        mark = {True: "ok", False: "MISS", None: "n/a"}
        print(f"{d['driver_id']:<9} {persona:<11} {f['week']['score']:>6.1f} {f['direction']:<11} "
              f"{', '.join(tip_factors):<40} {mark[persona_ok]:<8} {mark[wording_ok]:<8} {e['source']:<9} "
              f"{e['guardrail']:<10} {e.get('latency_ms', 0):>6}")

    def pct(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return f"{sum(vals)}/{len(vals)}" if vals else "n/a"

    bedrock_rows = [r for r in rows if r["source"] == "bedrock"]
    print(f"\nGenerated by the model:      {len(bedrock_rows)}/{len(rows)} (others fell back to the template)")
    print(f"Guardrail interventions:     {sum(r['guardrail'] == 'intervened' for r in rows)}")
    print(f"Tips match hidden persona:   {pct('persona_ok')}  (steady drivers excluded)")
    print(f"Summary states direction:    {pct('wording_ok')}")
    if bedrock_rows:
        lat = sorted(r["latency"] for r in bedrock_rows)
        tokens_in = sum(r["usage"].get("inputTokens", 0) for r in bedrock_rows)
        tokens_out = sum(r["usage"].get("outputTokens", 0) for r in bedrock_rows)
        print(f"Latency (model + checks):    median {lat[len(lat) // 2]} ms, max {lat[-1]} ms")
        print(f"Tokens:                      {tokens_in:,} in / {tokens_out:,} out")
    for r, d in zip(rows, drivers):
        if r["reason"]:
            print(f"  fallback {d['driver_id']}: {r['reason']}")


# --------------------------------------------------------------------------- Day 3: claims

def _wrap(text: str, indent: str = "  ", width: int = 92) -> str:
    return textwrap.fill(text, width, initial_indent=indent, subsequent_indent=indent)


def print_claim(c: dict) -> None:
    f = c["crash_facts"]
    i, loc = f["impact"], f["location"]
    print(f"{c['claim_id']}  status={c['status']}  triage={c['triage_label']}  "
          f"member={f['member']['first_name']} {f['member']['last_initial']}. ({c['member_driver_id']}, "
          f"{f['member']['vehicle']})")
    where = loc.get("address") or f"{loc['lat']}, {loc['lon']}"
    print(f"  {f['detected_at_local']}  |  {where}")
    print(f"  triage reasons: {'; '.join(c['triage_reasons'])}\n")
    print("  Timeline (telematics):")
    for row in f["timeline"]:
        print(f"    {row['when']:<22} {row['what']}")
    if c.get("member_message"):
        print("\n  Message to member:")
        print(_wrap(c["member_message"], "    "))
    if c.get("statement"):
        print("\n  Member statement:")
        print(_wrap(f'"{c["statement"]}"', "    "))
    for chk in c.get("statement_checks") or []:
        mark = "ok  " if chk["consistent"] else "DIFF"
        print(f"    [{mark}] {chk['aspect']}: says {chk['statement_says']}; data shows {chk['data_shows']}")
    d = c.get("draft")
    if not d:
        if c["status"] == "awaiting_statement":
            print(f"\n  Waiting for the member's statement (until {c.get('statement_deadline')}).")
            print(f"  Try: python scripts/demo.py statement {c['claim_id']}   (add --inconsistent to test the cross-check)")
        return
    print("\n  FNOL draft for the adjuster")
    print(_wrap(d["incident_summary"], "    "))
    sa = d["statement_assessment"]
    print(f"\n    Statement vs data: {sa['verdict'].replace('_', ' ').upper()}")
    print(_wrap(sa["explanation"], "      "))
    print("\n    Questions for the adjuster:")
    for q in d["adjuster_questions"]:
        print(_wrap(f"- {q}", "      "))
    print("    Next steps:")
    for st in d["next_steps"]:
        print(_wrap(f"- {st}", "      "))
    usage = d.get("usage") or {}
    print(f"\n    source={d['source']}  model={d['model_id']}  guardrail={d['guardrail']}  "
          f"tokens={usage.get('inputTokens', '-')}/{usage.get('outputTokens', '-')}  latency={d.get('latency_ms')}ms")
    if d.get("fallback_reason"):
        print(f"    fallback reason: {d['fallback_reason']}")


def cmd_claims(args) -> None:
    claims = api_get("/claims")["claims"]
    if not claims:
        print("No claims yet. Crashes from `make generate` open claims within about a minute.")
        return
    print(f"{'claim':<13} {'status':<25} {'triage':<19} {'member':<9} {'detected':<26} {'impact':<34} verdict")
    for c in claims:
        imp = c["impact"]
        impact = f"{imp['force_g']} g, {imp['direction_label']}, {imp['speed_at_impact_mph']} mph"
        print(f"{c['claim_id']:<13} {c['status']:<25} {c['triage_label']:<19} {c['member'] or '':<9} "
              f"{c['detected_at_local'] or '':<26} {impact:<34} {c['verdict'] or '-'}")


def cmd_claim(args) -> None:
    print_claim(api_get(f"/claims/{args.claim_id}"))


def _wait_for_status(claim_id: str, statuses: set[str], timeout_s: int = 120) -> dict:
    deadline = time.time() + timeout_s
    while True:
        c = api_get(f"/claims/{claim_id}")
        if c["status"] in statuses or time.time() > deadline:
            return c
        time.sleep(3)


def cmd_statement(args) -> None:
    claim = api_get(f"/claims/{args.claim_id}")
    text = args.text or simulate_statement(claim["crash_facts"], truthful=not args.inconsistent)
    print(f'Sending statement: "{text}"')
    resp = api_request("POST", f"/claims/{args.claim_id}/statement", body={"statement": text}, allow=(409,))
    if resp.get("_status") == 409:
        sys.exit(resp.get("message"))
    print("Waiting for the FNOL draft", end="", flush=True)
    claim = _wait_for_status(args.claim_id, {"draft_ready_for_adjuster"})
    print("\n")
    print_claim(claim)


def cmd_eval_claims(args) -> None:
    manifest = _latest_ground_truth()
    planted = {p["trip_id"]: p for p in manifest["planted_events"]}
    print(f"Ground truth: {sum(p['kind'] == 'crash' for p in planted.values())} real crashes, "
          f"{sum(p['kind'] == 'false_alarm' for p in planted.values())} false alarms.")
    print("Waiting for the workflow to open claims", end="", flush=True)
    deadline = time.time() + 180
    while True:
        claims = api_get("/claims")["claims"]
        settled = [c for c in claims if c["status"] != "detected"]
        if len(settled) >= len(planted) or time.time() > deadline:
            break
        print(".", end="", flush=True)
        time.sleep(5)
    print(f" {len(claims)} claims.\n")

    expected_triage = {"crash": "likely_crash", "false_alarm": "likely_false_alarm"}
    triage_ok = 0
    for c in claims:
        kind = planted.get(c["trip_id"], {}).get("kind", "not planted")
        ok = expected_triage.get(kind) == c["triage"]
        triage_ok += ok
        print(f"  {c['claim_id']}  truth={kind:<12} triage={c['triage']:<19} {'ok' if ok else 'MISS'}")
    print(f"Triage matches ground truth: {triage_ok}/{len(claims)}\n")

    waiting = sorted((c for c in claims if c["status"] == "awaiting_statement"), key=lambda c: c["claim_id"])
    if not waiting:
        print("No claims are waiting for a statement, so the statement cross-check was skipped.")
        print("Run `make generate` for fresh claims, then `make eval-claims` within the statement window.")
        return
    sent = []
    for n, c in enumerate(waiting):
        truthful = n % 2 == 0
        facts = api_get(f"/claims/{c['claim_id']}")["crash_facts"]
        text = simulate_statement(facts, truthful=truthful)
        api_post(f"/claims/{c['claim_id']}/statement", {"statement": text})
        sent.append((c["claim_id"], truthful))
    print(f"Sent {len(sent)} statements (alternating truthful / inconsistent). Waiting for drafts ...\n")

    rows = []
    for claim_id, truthful in sent:
        c = _wait_for_status(claim_id, {"draft_ready_for_adjuster"}, timeout_s=180)
        d = c.get("draft") or {}
        verdict = d.get("statement_assessment", {}).get("verdict", "none")
        ok = verdict == "consistent" if truthful else verdict in ("inconsistent", "partially_consistent")
        rows.append((claim_id, truthful, verdict, ok, d.get("source"), d.get("guardrail"), d.get("latency_ms"),
                     d.get("fallback_reason")))
    print(f"{'claim':<13} {'statement':<13} {'verdict':<21} {'check':<6} {'source':<9} {'guardrail':<11} ms")
    for claim_id, truthful, verdict, ok, source, guard, ms, _ in rows:
        print(f"{claim_id:<13} {'truthful' if truthful else 'inconsistent':<13} {verdict:<21} "
              f"{'ok' if ok else 'MISS':<6} {source or '-':<9} {guard or '-':<11} {ms or '-'}")
    print(f"\nVerdict matches the statement we sent: {sum(r[3] for r in rows)}/{len(rows)}")
    print(f"Drafted by the model:                  {sum(r[4] == 'bedrock' for r in rows)}/{len(rows)}")
    for r in rows:
        if r[7]:
            print(f"  fallback {r[0]}: {r[7]}")


# --------------------------------------------------------------------------- Day 4

def cmd_site(args) -> None:
    out = stack_outputs()
    if "SiteUrl" not in out:
        sys.exit("The stack has no web app yet. Run `cdk deploy` to add the Day 4 resources.")
    print(f"Web app:  {out['SiteUrl']}")
    print(f"User:     {out['SiteUser']}")
    print("Password: the site_password you deployed with (default: telematics-demo)")


def cmd_ask(args) -> None:
    session = args.session or f"cli-{int(time.time())}"
    job = api_post("/copilot", {"question": args.question, "session_id": session})
    print(f"Asking the copilot (session {session})", end="", flush=True)
    deadline = time.time() + 180
    while True:
        r = api_get(f"/copilot/{job['job_id']}")
        if r["status"] in ("done", "error") or time.time() > deadline:
            break
        print(".", end="", flush=True)
        time.sleep(2)
    print("\n")
    if r["status"] != "done":
        sys.exit(f"Copilot {r['status']}: {r.get('error', 'no answer yet')}")
    print(r["answer"])
    print(f"\n{len(r['tool_calls'])} lookups, {r['latency_ms'] / 1000:.1f} s, "
          f"{r['usage'].get('inputTokens', 0):,} in / {r['usage'].get('outputTokens', 0):,} out tokens, "
          f"guardrail {r['guardrail']}")
    for n, c in enumerate(r["tool_calls"], 1):
        params = ", ".join(f"{k}={v}" for k, v in (c.get("parameters") or {}).items())
        print(f"  {n}. {c['function']}({params})")
        if c.get("why"):
            print(_wrap(c["why"], "     ", 92))
    print(f"\nFollow up in the same conversation with: --session {session}")


def cmd_verify_clean(args) -> None:
    """After `cdk destroy`: list anything still tagged with this project, plus leftover log groups."""
    boto3 = _boto3()
    cfn = boto3.client("cloudformation")
    try:
        status = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"][0]["StackStatus"]
        print(f"Stack {STACK_NAME} still exists ({status}). Run `cdk destroy` first.")
    except Exception:  # noqa: BLE001
        print(f"Stack {STACK_NAME}: gone")
    tagging = boto3.client("resourcegroupstaggingapi")
    leftovers = []
    for page in tagging.get_paginator("get_resources").paginate(
            TagFilters=[{"Key": "project", "Values": ["insurance-telematics-genai"]}]):
        leftovers += [r["ResourceARN"] for r in page["ResourceTagMappingList"]]
    print(f"Resources tagged project=insurance-telematics-genai: {len(leftovers)}")
    for arn in leftovers:
        note = "  (Secrets Manager deletes this after its recovery window)" if ":secretsmanager:" in arn else ""
        print(f"  {arn}{note}")
    logs_client = boto3.client("logs")
    groups = []
    for prefix in (f"/aws/lambda/{STACK_NAME}", f"/aws/events/{STACK_NAME}", f"/aws/vendedlogs/states/{STACK_NAME}"):
        for page in logs_client.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
            groups += [g["logGroupName"] for g in page["logGroups"]]
    print(f"Log groups left by CDK helper functions: {len(groups)}")
    for g in groups:
        print(f"  {g}")
    if groups:
        print("  These are created by CDK's own helper Lambdas (auto-delete, bucket deployment) and cost nothing")
        print("  at rest. Remove them with: aws logs delete-log-group --log-group-name <name>")
    print("\nThe CDK bootstrap stack (CDKToolkit) is shared by all CDK apps and is intentionally kept.")


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

    ex = sub.add_parser("explain", help="gen AI score explanation for a driver")
    ex.add_argument("driver_id")
    ex.add_argument("--refresh", action="store_true", help="regenerate instead of using the cache")
    ex.add_argument("--facts", action="store_true", help="also print the facts sent to the model")
    ex.set_defaults(func=cmd_explain)

    ev = sub.add_parser("eval", help="explain every driver and check against the ground truth")
    ev.add_argument("--limit", type=int, default=0)
    ev.add_argument("--cached", action="store_true", help="reuse cached explanations")
    ev.set_defaults(func=cmd_eval)

    sub.add_parser("claims", help="claims opened by the crash workflow").set_defaults(func=cmd_claims)
    cl = sub.add_parser("claim", help="one claim with its FNOL draft")
    cl.add_argument("claim_id")
    cl.set_defaults(func=cmd_claim)
    st = sub.add_parser("statement", help="send a member statement and wait for the draft")
    st.add_argument("claim_id")
    st.add_argument("--inconsistent", action="store_true", help="send a statement that contradicts the data")
    st.add_argument("--text", help="your own statement text")
    st.set_defaults(func=cmd_statement)
    sub.add_parser("eval-claims", help="check triage and statement cross-checks against ground truth"
                   ).set_defaults(func=cmd_eval_claims)

    sub.add_parser("site", help="web app URL and user").set_defaults(func=cmd_site)
    ak = sub.add_parser("ask", help="ask the Bedrock Agent copilot a question")
    ak.add_argument("question")
    ak.add_argument("--session", help="continue an earlier conversation")
    ak.set_defaults(func=cmd_ask)
    sub.add_parser("verify-clean", help="after cdk destroy: list anything left behind"
                   ).set_defaults(func=cmd_verify_clean)

    lo = sub.add_parser("local", help="run generator + scoring offline")
    gen_args(lo)
    lo.add_argument("--out", default="local_data")
    lo.set_defaults(func=cmd_local)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
