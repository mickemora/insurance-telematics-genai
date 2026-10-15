"""Small helpers around the Bedrock Converse and ApplyGuardrail APIs.

Shared by the score explainer (Day 2) and the claim drafter (Day 3).
"""
from __future__ import annotations

import json
from typing import Any


class ValidationError(Exception):
    """Model output failed a grounding or safety check; callers fall back to a template."""


def tool_spec(name: str, description: str, schema: dict) -> dict:
    return {"toolSpec": {"name": name, "description": description, "inputSchema": {"json": schema}}}


def call_tool(bedrock, model_id: str, system: str, user_text: str, spec: dict,
              max_tokens: int = 800, temperature: float = 0.3) -> tuple[dict, dict]:
    """Force the model to answer through one tool; return (tool input, usage)."""
    name = spec["toolSpec"]["name"]
    resp = bedrock.converse(
        modelId=model_id,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": user_text}]}],
        toolConfig={"tools": [spec], "toolChoice": {"tool": {"name": name}}},
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )
    blocks = resp.get("output", {}).get("message", {}).get("content", [])
    tool = next((b["toolUse"] for b in blocks if "toolUse" in b), None)
    if not tool or tool.get("name") != name:
        raise ValidationError(f"model did not call {name} (stopReason={resp.get('stopReason')})")
    usage = resp.get("usage", {})
    return tool["input"], {k: usage[k] for k in ("inputTokens", "outputTokens") if k in usage}


def guardrail_passes(bedrock, guardrail_id: str, guardrail_version: str, texts: list[str]) -> bool:
    resp = bedrock.apply_guardrail(
        guardrailIdentifier=guardrail_id,
        guardrailVersion=guardrail_version,
        source="OUTPUT",
        content=[{"text": {"text": "\n".join(t for t in texts if t)}}],
    )
    return resp.get("action", "NONE") != "GUARDRAIL_INTERVENED"


def as_prompt_json(obj: Any) -> str:
    return json.dumps(obj, indent=1, default=str)
