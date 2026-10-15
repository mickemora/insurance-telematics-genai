"""Turn a fact sheet into a coaching explanation with Claude on Amazon Bedrock.

Flow:
1. Converse API with a forced tool call, so the model must return structured
   JSON (headline, summary, tips) instead of free text.
2. Validate in code: every number must come from the facts, every tip must
   name a factor that actually cost points, no pricing or fault language.
3. ApplyGuardrail on the output (pricing promises, fault/legal, harmful content).
4. If any step fails, return a deterministic template explanation instead,
   and say why in the response, so the API never returns unchecked text.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from facts import FACTORS, MIN_POINTS, allowed_numbers, number_is_grounded, numbers_in

TOOL_NAME = "submit_explanation"
GENERIC_TIP_MAX = 15  # small numbers allowed in tips ("a 3-second following distance")

SYSTEM_PROMPT = """You are a friendly driving coach inside an auto insurer's safe-driving app. \
You explain a member's weekly driving score and give practical coaching.

Rules:
- Use ONLY the facts in the JSON you receive. Never invent trips, numbers, places, or causes.
- Every number you write must appear in the facts (rounding to a whole number is fine). Never compute new numbers.
- Never mention insurance premiums, prices, discounts, rates, savings, claims, fault, or legal matters. Talk only about driving habits.
- Speak directly to the driver ("you"). Use their first name at most once. Be warm and specific, never judgmental or alarming.
- headline: one short sentence (at most 12 words) about the score and the main reason.
- summary: 2 to 4 sentences, at most 80 words. Say whether the score went up, went down, or held steady \
(use "direction"), name the factor that cost the most points, and if a "pattern" is given, say when it tends to happen. \
If "strengths" is not empty, acknowledge one strength briefly.
- tips: 2 or 3 tips, most costly factor first. Each tip names one factor from "costly_factors" and is one or two \
practical sentences tied to when or how the issue happens. If "costly_factors" is empty, give 2 tips to keep up the \
good driving and use factor "general".
Return your answer only by calling the submit_explanation tool."""

TOOL_SPEC = {
    "toolSpec": {
        "name": TOOL_NAME,
        "description": "Submit the driver-facing score explanation and coaching tips.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {
                "headline": {"type": "string", "description": "At most 12 words."},
                "summary": {"type": "string", "description": "2-4 sentences, at most 80 words."},
                "tips": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "factor": {"type": "string", "enum": [*FACTORS, "general"]},
                            "tip": {"type": "string"},
                        },
                        "required": ["factor", "tip"],
                    },
                },
            },
            "required": ["headline", "summary", "tips"],
        }},
    }
}

# Belt and braces next to the Bedrock Guardrail: cheap, deterministic, and testable.
BANNED = re.compile(
    r"\b(premium|premiums|discount|discounts|price|prices|pricing|savings|save money|refund|"
    r"insurance rate|your rate|rates? (?:will|could|may) (?:go|increase|decrease)|"
    r"at fault|liable|liability|lawsuit|legal)\b",
    re.IGNORECASE,
)

TIP_LIBRARY = {
    "hard_braking": "Leave more room to the car ahead, about 3 seconds, so you can ease off the gas instead of braking hard.",
    "rapid_acceleration": "Pull away gently from lights and stop signs; a smooth start saves fuel and wear too.",
    "harsh_cornering": "Slow down before the turn, not in it, and keep a steady speed through the curve.",
    "speeding": "Set cruise control on highways, and give yourself a few extra minutes so you are not racing the clock.",
    "phone_handling": "Turn on Do Not Disturb While Driving, and set up music or navigation before you pull out.",
    "late_night": "Where you can, move late-night drives earlier in the evening when roads are better lit and drivers more alert.",
    "general": "Keep doing what you are doing: smooth starts, steady speeds, and the phone out of reach.",
}


class ValidationError(Exception):
    pass


# --------------------------------------------------------------------------- validation

def validate(explanation: dict, facts: dict) -> None:
    """Raise ValidationError if the model output is not grounded in the facts."""
    for key in ("headline", "summary", "tips"):
        if not explanation.get(key):
            raise ValidationError(f"missing {key}")
    tips = explanation["tips"]
    if not 2 <= len(tips) <= 3:
        raise ValidationError(f"expected 2-3 tips, got {len(tips)}")
    if len(explanation["headline"].split()) > 16:
        raise ValidationError("headline too long")
    if len(explanation["summary"].split()) > 110:
        raise ValidationError("summary too long")

    allowed = allowed_numbers(facts)
    for field in ("headline", "summary"):
        for n in numbers_in(explanation[field]):
            if not number_is_grounded(n, allowed):
                raise ValidationError(f"ungrounded number {n:g} in {field}")

    costly = set(facts.get("costly_factors") or [])
    for tip in tips:
        factor = tip.get("factor")
        if costly and factor not in costly:
            raise ValidationError(f"tip factor {factor!r} did not cost points")
        if not costly and factor != "general":
            raise ValidationError("no costly factors, tips must be 'general'")
        for n in numbers_in(tip.get("tip", "")):
            if n > GENERIC_TIP_MAX and not number_is_grounded(n, allowed):
                raise ValidationError(f"ungrounded number {n:g} in tip")

    text = " ".join([explanation["headline"], explanation["summary"], *(t["tip"] for t in tips)])
    hit = BANNED.search(text)
    if hit:
        raise ValidationError(f"banned phrase {hit.group(0)!r}")


# --------------------------------------------------------------------------- fallback

def fallback_explanation(facts: dict) -> dict:
    """Deterministic, always-safe explanation built straight from the facts."""
    week = facts["week"]
    costly = [f for f in facts["factors"] if f["points_lost"] >= MIN_POINTS]
    direction = facts["direction"]
    if direction in ("up", "down"):
        prev = facts["previous_week"]["score"]
        opener = f"Your score went {direction} to {week['score']:g} this week ({week['dates']}), from {prev:g}."
        headline = f"Your score went {direction} to {week['score']:g}"
    elif direction == "held steady":
        opener = f"Your score held steady at {week['score']:g} this week ({week['dates']})."
        headline = f"Your score held steady at {week['score']:g}"
    else:
        opener = f"Your first weekly score is {week['score']:g} ({week['dates']})."
        headline = f"Your weekly score is {week['score']:g}"

    parts = [opener]
    if costly:
        top = costly[0]
        headline += (f", mostly from {top['label']}" if direction == "down"
                     else f"; {top['label']} costs the most points")
        sep = "" if top["unit"].startswith("%") else " "
        parts.append(f"The biggest factor was {top['label']}, which cost {top['points_lost']:g} points "
                     f"({top['rate']:g}{sep}{top['unit']}).")
        pattern = facts.get("pattern")
        if pattern:
            parts.append(f"{pattern['share_of_factor_pct']}% of it happened on {pattern['when']}, "
                         f"which are {pattern['share_of_miles_pct']}% of your miles.")
        tips = [{"factor": f["factor"], "tip": TIP_LIBRARY[f["factor"]]} for f in costly[:3]]
        if len(tips) < 2:
            tips.append({"factor": tips[0]["factor"], "tip": TIP_LIBRARY["general"]})
    else:
        headline += ", nice work"
        tips = [{"factor": "general", "tip": TIP_LIBRARY["general"]},
                {"factor": "general", "tip": "Keep your phone out of reach while driving so it stays that way."}]
    if facts.get("strengths"):
        parts.append(f"Nice work on {facts['strengths'][0]}.")
    return {"headline": headline + ".", "summary": " ".join(parts), "tips": tips}


# --------------------------------------------------------------------------- Bedrock

def _call_model(bedrock, model_id: str, facts: dict) -> tuple[dict, dict]:
    resp = bedrock.converse(
        modelId=model_id,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": "Driver facts:\n" + json.dumps(facts, indent=1)}]}],
        toolConfig={"tools": [TOOL_SPEC], "toolChoice": {"tool": {"name": TOOL_NAME}}},
        inferenceConfig={"maxTokens": 800, "temperature": 0.3},
    )
    blocks = resp.get("output", {}).get("message", {}).get("content", [])
    tool = next((b["toolUse"] for b in blocks if "toolUse" in b), None)
    if not tool or tool.get("name") != TOOL_NAME:
        raise ValidationError(f"model did not call {TOOL_NAME} (stopReason={resp.get('stopReason')})")
    return tool["input"], resp.get("usage", {})


def _apply_guardrail(bedrock, guardrail_id: str, guardrail_version: str, explanation: dict) -> str:
    text = "\n".join([explanation["headline"], explanation["summary"], *(t["tip"] for t in explanation["tips"])])
    resp = bedrock.apply_guardrail(
        guardrailIdentifier=guardrail_id,
        guardrailVersion=guardrail_version,
        source="OUTPUT",
        content=[{"text": {"text": text}}],
    )
    return resp.get("action", "NONE")


def explain(facts: dict, bedrock, model_id: str, guardrail_id: str | None = None,
            guardrail_version: str | None = None) -> dict[str, Any]:
    """Return the explanation plus provenance (source, model, guardrail, usage, timing)."""
    started = time.time()
    meta: dict[str, Any] = {"source": "bedrock", "model_id": model_id, "guardrail": "not_configured",
                            "usage": {}, "fallback_reason": None}
    try:
        explanation, usage = _call_model(bedrock, model_id, facts)
        meta["usage"] = {k: usage.get(k) for k in ("inputTokens", "outputTokens") if k in usage}
        validate(explanation, facts)
        if guardrail_id:
            action = _apply_guardrail(bedrock, guardrail_id, guardrail_version or "DRAFT", explanation)
            meta["guardrail"] = "intervened" if action == "GUARDRAIL_INTERVENED" else "passed"
            if meta["guardrail"] == "intervened":
                raise ValidationError("Bedrock Guardrail intervened")
        explanation = {"headline": explanation["headline"].strip(), "summary": explanation["summary"].strip(),
                       "tips": [{"factor": t["factor"], "tip": t["tip"].strip()} for t in explanation["tips"]]}
    except ValidationError as err:
        meta.update(source="fallback", fallback_reason=str(err))
        explanation = fallback_explanation(facts)
    except Exception as err:  # noqa: BLE001 - Bedrock errors (access, throttling) degrade gracefully
        meta.update(source="fallback", fallback_reason=f"bedrock error: {type(err).__name__}: {err}"[:300])
        explanation = fallback_explanation(facts)
    meta["latency_ms"] = int((time.time() - started) * 1000)
    return {**explanation, **meta}
