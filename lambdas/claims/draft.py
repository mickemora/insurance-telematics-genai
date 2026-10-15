"""Draft a first notice of loss (FNOL) for an adjuster with Claude on Bedrock.

Same safety pattern as the score explainer:
1. forced tool output (structured draft),
2. code checks: numbers grounded in the crash facts or the member's own words,
   the statement verdict cannot contradict the deterministic statement checks,
   no fault / payout language,
3. Bedrock Guardrail on the output,
4. deterministic template draft if anything fails.

The model drafts; an adjuster decides. Nothing here determines fault or coverage.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from facts import allowed_numbers, number_is_grounded, numbers_in
from llm import ValidationError, call_tool, guardrail_passes, tool_spec

TOOL_NAME = "submit_claim_draft"
VERDICTS = ("consistent", "partially_consistent", "inconsistent", "no_statement")
GENERIC_NUMBERS = {911.0}
GENERIC_MAX = 15

SYSTEM_PROMPT = """You prepare first notice of loss (FNOL) drafts for an auto insurance claims adjuster. \
Your inputs are crash facts computed from the member's phone telematics, the member's own statement \
(which may be null), and deterministic statement_checks that compare the statement with the data.

Rules:
- Use ONLY the inputs. Never invent people, vehicles, injuries, damage, weather, or causes.
- Every number you write must appear in the crash facts or in the member's statement. Never compute new numbers.
- Never determine, suggest, or discuss fault, liability, blame, or who caused the crash.
- Never say whether anything is covered, approved, denied, or will be paid.
- Neutral, factual tone written for an adjuster. Refer to the member by first name.

Fields:
- incident_summary: 2 to 4 sentences: when and where, and what the data shows (impact direction and force, \
speed before and at impact, braking, whether the vehicle stopped).
- statement_assessment.verdict: "no_statement" if member_statement is null. Otherwise "consistent", \
"partially_consistent", or "inconsistent". You must not call it "consistent" if any statement_check is \
inconsistent. statement_assessment.explanation: 1 to 3 sentences that cite the specific data points.
- adjuster_questions: 2 to 4 specific questions the adjuster should ask to complete the claim \
(for example other vehicles or parties involved, injuries, photos, police report), including any \
inconsistency to clarify.
- next_steps: 2 to 4 practical next steps (for example confirm the member is safe, arrange a tow if the \
vehicle is not drivable, request photos, offer a rental).
Return the draft only by calling the submit_claim_draft tool."""

TOOL_SPEC = tool_spec(TOOL_NAME, "Submit the FNOL draft for the adjuster.", {
    "type": "object",
    "properties": {
        "incident_summary": {"type": "string"},
        "statement_assessment": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": list(VERDICTS)},
                "explanation": {"type": "string"},
            },
            "required": ["verdict", "explanation"],
        },
        "adjuster_questions": {"type": "array", "minItems": 2, "maxItems": 4, "items": {"type": "string"}},
        "next_steps": {"type": "array", "minItems": 2, "maxItems": 4, "items": {"type": "string"}},
    },
    "required": ["incident_summary", "statement_assessment", "adjuster_questions", "next_steps"],
})

BANNED = re.compile(
    r"\b(at fault|not at fault|fault|faulted|liable|liability|blame|blamed|negligen\w*|lawsuit|sue|"
    r"payout|pay out|will be paid|claim (?:is |was )?approved|denied|deny the claim|guarantee\w*)\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- validation

def validate_draft(draft: dict, inputs: dict) -> None:
    for key in ("incident_summary", "statement_assessment", "adjuster_questions", "next_steps"):
        if not draft.get(key):
            raise ValidationError(f"missing {key}")
    verdict = draft["statement_assessment"].get("verdict")
    if verdict not in VERDICTS:
        raise ValidationError(f"bad verdict {verdict!r}")
    statement = inputs.get("member_statement")
    if not statement and verdict != "no_statement":
        raise ValidationError("verdict must be no_statement when there is no statement")
    if statement and verdict == "no_statement":
        raise ValidationError("statement present but verdict is no_statement")
    if verdict == "consistent" and any(not c["consistent"] for c in inputs.get("statement_checks", [])):
        raise ValidationError("verdict 'consistent' contradicts the statement checks")
    for key in ("adjuster_questions", "next_steps"):
        if not 2 <= len(draft[key]) <= 4:
            raise ValidationError(f"{key} must have 2-4 items")
    if len(draft["incident_summary"].split()) > 120:
        raise ValidationError("summary too long")

    allowed = allowed_numbers(inputs)
    strict = [draft["incident_summary"], draft["statement_assessment"].get("explanation", "")]
    for text in strict:
        for n in numbers_in(text):
            if not number_is_grounded(n, allowed):
                raise ValidationError(f"ungrounded number {n:g}")
    for text in [*draft["adjuster_questions"], *draft["next_steps"]]:
        for n in numbers_in(text):
            if n > GENERIC_MAX and n not in GENERIC_NUMBERS and not number_is_grounded(n, allowed):
                raise ValidationError(f"ungrounded number {n:g} in a question or step")

    hit = BANNED.search(" ".join([*strict, *draft["adjuster_questions"], *draft["next_steps"]]))
    if hit:
        raise ValidationError(f"banned phrase {hit.group(0)!r}")


# --------------------------------------------------------------------------- fallback

def fallback_draft(inputs: dict) -> dict:
    f, statement, checks = inputs["crash_facts"], inputs.get("member_statement"), inputs.get("statement_checks", [])
    i, a, loc = f["impact"], f["after_impact"], f["location"]
    where = loc.get("address") or f"near {loc['lat']}, {loc['lon']}"
    braking = (f"Hard braking began {i['braking_started_seconds_before']} seconds before impact."
               if i["braking_before_impact"] else "No braking was detected before impact.")
    after = (f"The vehicle then stopped and stayed stationary for {a['stationary_seconds']} seconds."
             if a["stationary_seconds"] else f"The vehicle kept moving at {a['speed_mph']} mph afterwards.")
    summary = (f"On {f['detected_at_local']}, {where}, telematics recorded a {i['force_g']} g impact to "
               f"{i['direction_label']} of {f['member']['first_name']}'s {f['member']['vehicle']}. Speed was "
               f"{i['speed_5s_before_mph']} mph 5 seconds before and {i['speed_at_impact_mph']} mph at impact. "
               f"{braking} {after}")
    if not statement:
        verdict, explanation = "no_statement", "The member has not provided a statement yet."
    else:
        bad = [c for c in checks if not c["consistent"]]
        if bad:
            verdict = "inconsistent" if len(bad) == len(checks) else "partially_consistent"
            explanation = "; ".join(f"statement says {c['statement_says']}, data shows {c['data_shows']}"
                                    for c in bad) + "."
            explanation = explanation[0].upper() + explanation[1:]
        elif checks:
            verdict, explanation = "consistent", "The statement matches the data on " + ", ".join(
                c["aspect"] for c in checks) + "."
        else:
            verdict, explanation = "partially_consistent", "The statement does not mention details that can be checked against the data."
    questions = ["Were any other vehicles, people, or property involved? Please collect their details.",
                 "Was anyone injured, including passengers?"]
    if verdict in ("inconsistent", "partially_consistent") and statement:
        questions.append("Can the member walk through the moments before the impact to clarify the differences from the data?")
    questions.append("Was a police report filed, and are there photos of the scene and vehicles?")
    steps = ["Confirm the member and any passengers are safe.",
             "Ask whether the vehicle is drivable and arrange a tow if needed.",
             "Request photos of all vehicles and the scene."]
    return {"incident_summary": summary,
            "statement_assessment": {"verdict": verdict, "explanation": explanation},
            "adjuster_questions": questions[:4], "next_steps": steps}


# --------------------------------------------------------------------------- Bedrock

def draft_claim(inputs: dict, bedrock, model_id: str, guardrail_id: str | None = None,
                guardrail_version: str | None = None) -> dict[str, Any]:
    """inputs = {"crash_facts", "member_statement", "statement_checks"}."""
    started = time.time()
    meta: dict[str, Any] = {"source": "bedrock", "model_id": model_id, "guardrail": "not_configured",
                            "usage": {}, "fallback_reason": None}
    try:
        draft, meta["usage"] = call_tool(bedrock, model_id, SYSTEM_PROMPT,
                                         "Claim inputs:\n" + json.dumps(inputs, indent=1, default=str),
                                         TOOL_SPEC, max_tokens=1200, temperature=0.2)
        validate_draft(draft, inputs)
        if guardrail_id:
            texts = [draft["incident_summary"], draft["statement_assessment"]["explanation"],
                     *draft["adjuster_questions"], *draft["next_steps"]]
            ok = guardrail_passes(bedrock, guardrail_id, guardrail_version or "DRAFT", texts)
            meta["guardrail"] = "passed" if ok else "intervened"
            if not ok:
                raise ValidationError("Bedrock Guardrail intervened")
    except ValidationError as err:
        meta.update(source="fallback", fallback_reason=str(err))
        draft = fallback_draft(inputs)
    except Exception as err:  # noqa: BLE001
        meta.update(source="fallback", fallback_reason=f"bedrock error: {type(err).__name__}: {err}"[:300])
        draft = fallback_draft(inputs)
    meta["latency_ms"] = int((time.time() - started) * 1000)
    return {**draft, **meta}
