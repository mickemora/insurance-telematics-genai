"""Copilot worker: runs one question through the Bedrock Agent (invoked async by the API).

Agents can take longer than API Gateway's 29-second limit, so the API stores a
job and returns 202; this worker calls InvokeAgent, collects the answer plus a
trace of every tool call, and writes the result back to the job item.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE = os.environ["TABLE_NAME"]
AGENT_ID = os.environ["AGENT_ID"]
AGENT_ALIAS_ID = os.environ.get("AGENT_ALIAS_ID", "TSTALIASID")
COPILOT_PK = "COPILOT"

table = boto3.resource("dynamodb").Table(TABLE)
agents = boto3.client("bedrock-agent-runtime", config=Config(read_timeout=120, retries={"max_attempts": 2}))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_agent(question: str, session_id: str, client=None) -> dict:
    """Invoke the agent and return answer, tool calls, token usage, guardrail action."""
    client = client or agents
    resp = client.invoke_agent(agentId=AGENT_ID, agentAliasId=AGENT_ALIAS_ID, sessionId=session_id,
                               inputText=question, enableTrace=True)
    answer, calls, usage, guardrail = [], [], {"inputTokens": 0, "outputTokens": 0}, "none"
    rationale = None
    for event in resp["completion"]:
        if "chunk" in event:
            answer.append(event["chunk"]["bytes"].decode("utf-8"))
            continue
        trace = event.get("trace", {}).get("trace", {})
        if "guardrailTrace" in trace and trace["guardrailTrace"].get("action") == "INTERVENED":
            guardrail = "intervened"
        orch = trace.get("orchestrationTrace", {})
        if "rationale" in orch:
            rationale = orch["rationale"].get("text")
        call = orch.get("invocationInput", {}).get("actionGroupInvocationInput")
        if call:
            calls.append({
                "function": call.get("function"),
                "parameters": {p["name"]: p.get("value") for p in call.get("parameters", [])},
                "why": rationale,
            })
            rationale = None
        observed = orch.get("observation", {}).get("actionGroupInvocationOutput")
        if observed and calls:
            text = observed.get("text", "")
            calls[-1]["result_chars"] = len(text)
            calls[-1]["result_preview"] = text[:240]
        meta = orch.get("modelInvocationOutput", {}).get("metadata", {}).get("usage")
        if meta:
            usage["inputTokens"] += meta.get("inputTokens", 0)
            usage["outputTokens"] += meta.get("outputTokens", 0)
    return {"answer": "".join(answer).strip(), "tool_calls": calls, "usage": usage, "guardrail": guardrail}


def handler(event, context):
    job_id = event["job_id"]
    key = {"driver_id": COPILOT_PK, "sk": job_id}
    job = table.get_item(Key=key).get("Item")
    if not job:
        logger.warning("job %s not found", job_id)
        return
    table.update_item(Key=key, UpdateExpression="SET #s = :s, started_at = :t",
                      ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "running", ":t": _now()})
    started = time.time()
    try:
        result = run_agent(job["question"], job["session_id"])
        values = {":s": "done", ":r": json.loads(json.dumps(result)), ":t": _now(),
                  ":l": int((time.time() - started) * 1000)}
        expr = "SET #s = :s, #r = :r, finished_at = :t, latency_ms = :l"
    except Exception as err:  # noqa: BLE001
        logger.exception("agent failed")
        values = {":s": "error", ":r": {"error": f"{type(err).__name__}: {err}"[:500]}, ":t": _now(),
                  ":l": int((time.time() - started) * 1000)}
        expr = "SET #s = :s, #r = :r, finished_at = :t, latency_ms = :l"
    table.update_item(Key=key, UpdateExpression=expr, ExpressionAttributeNames={"#s": "status", "#r": "result"},
                      ExpressionAttributeValues=values)
    logger.info(json.dumps({"job_id": job_id, "status": values[":s"], "latency_ms": values[":l"]}))
