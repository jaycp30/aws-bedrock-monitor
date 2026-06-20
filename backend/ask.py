"""
"Ask" feature — a small Bedrock agent (Converse API + tool-use) that answers
natural-language questions about Bedrock usage and cost.

It reuses the dashboard's own data function as a tool, so answers are grounded in
real numbers (never hallucinated). Three layers keep it on-task:
  - tool set   → bounds what DATA it can reach (only usage),
  - system prompt → guides it to usage/cost topics,
  - Bedrock Guardrail → ENFORCES the boundary (blocks off-topic prompts).
"""

import os
import time

import boto3
from botocore.config import Config

# Haiku by default — cheap and plenty for this. Override per deploy/region.
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "jp.anthropic.claude-haiku-4-5-20251001-v1:0")
MAX_TURNS = int(os.environ.get("ASK_MAX_TURNS", "5"))
MAX_TOKENS = int(os.environ.get("ASK_MAX_TOKENS", "1600"))
ALLOWED_RANGES = {"today", "7d", "30d", "90d"}

# Guardrail (set by the SAM stack). When present, every Converse call is screened.
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

_BOTO_CFG = Config(retries={"max_attempts": 3, "mode": "standard"})

SYSTEM_PROMPT = (
    "You are the assistant for an AWS Bedrock usage & cost dashboard. "
    "Answer ONLY questions about this account's Bedrock usage: tokens, invocations, "
    "models, regions, IAM users/principals, prompt-cache usage, and cost (estimated and billed). "
    "Questions asking which user, IAM principal, or identity used Bedrock most are in scope. "
    "Also explain dashboard concepts when asked — e.g. prompt caching, cache "
    "read/write tokens, invocations, and estimated vs billed cost. "
    "Always use the get_usage tool to fetch real figures — never invent numbers. "
    "If a question is unrelated to Bedrock usage or cost, politely decline and steer "
    "the user back. Be concise and cite actual figures with units and currency. "
    "Only the most recent turns of the conversation are provided; if the user refers "
    "to something no longer visible, ask them to restate it."
)

TOOLS = [
    {
        "toolSpec": {
            "name": "get_usage",
            "description": (
                "Get Bedrock usage and cost for a time range, broken down by model and "
                "region and IAM user/principal: input/output/cache tokens, invocations, "
                "estimated cost (live prices) and billed cost (Cost Explorer)."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "range": {
                            "type": "string",
                            "enum": ["today", "7d", "30d", "90d"],
                            "description": "Time range to summarize.",
                        }
                    },
                    "required": ["range"],
                }
            },
        }
    }
]


def _log_json(event: str, **fields):
    """Emit compact structured logs for CloudWatch without logging prompts."""
    import json

    print(json.dumps({"event": event, **fields}, default=str))


def _safe_range(value: str | None, fallback: str = "7d") -> str:
    return value if isinstance(value, str) and value in ALLOWED_RANGES else fallback


def _content_text(blocks: list[dict]) -> str:
    return "".join(b.get("text", "") for b in blocks).strip()


def _content_keys(blocks: list[dict]) -> list[list[str]]:
    return [sorted(b.keys()) for b in blocks]


def _run_tool(name: str, tool_input: dict, default_range: str = "7d") -> dict:
    from app import get_usage_payload  # lazy import avoids circular import at load

    if name == "get_usage":
        requested_range = _safe_range(tool_input.get("range"), default_range)
        t0 = time.perf_counter()
        result = get_usage_payload({"range": requested_range})
        _log_json(
            "ask_tool_timing",
            tool=name,
            range=requested_range,
            duration_ms=round((time.perf_counter() - t0) * 1000, 2),
        )
        return result
    return {"error": f"unknown tool: {name}"}


def _converse_kwargs(messages, default_range: str = "7d"):
    system_prompt = (
        SYSTEM_PROMPT
        + f" The dashboard's currently selected range is {default_range}; "
        + "use that range when the user does not specify a different time range."
    )
    kwargs = {
        "modelId": MODEL_ID,
        "system": [{"text": system_prompt}],
        "messages": messages,
        "toolConfig": {"tools": TOOLS},
        "inferenceConfig": {"maxTokens": MAX_TOKENS, "temperature": 0.2},
    }
    if GUARDRAIL_ID:
        kwargs["guardrailConfig"] = {
            "guardrailIdentifier": GUARDRAIL_ID,
            "guardrailVersion": GUARDRAIL_VERSION,
            "trace": "disabled",
        }
    return kwargs


# The browser carries the conversation (the Lambda stays stateless); we only
# accept the most recent turns to bound token cost per request.
MAX_HISTORY_TURNS = 12


def ask(question: str, region: str | None = None, default_range: str = "7d") -> str:
    """Single-question entrypoint (CLI / legacy clients)."""
    return ask_chat([{"role": "user", "text": question}], region=region, default_range=default_range)


def ask_chat(history: list[dict], region: str | None = None, default_range: str = "7d") -> str:
    """Multi-turn entrypoint: history is [{role: user|assistant, text: str}, ...],
    oldest first, ending with the user's latest message."""
    default_range = _safe_range(default_range)
    bedrock = boto3.client(
        "bedrock-runtime",
        region_name=region or os.environ.get("AWS_REGION"),
        config=_BOTO_CFG,
    )
    # Build the Converse message list, but GUARD ONLY THE LATEST USER TURN.
    #
    # Bedrock applies the Guardrail to every content block UNLESS we use input
    # tagging — when any block is wrapped in `guardContent`, only the tagged
    # content is screened. We resend recent history on each request, so without
    # this a prior turn that tripped the Guardrail (e.g. a prompt-injection
    # attempt) would be re-scanned and re-blocked on every later request,
    # stonewalling otherwise-valid questions until that turn scrolls out of the
    # window. Tagging only the newest user turn screens fresh input while
    # leaving past turns as inert context. (Model *output* is screened
    # regardless of input tagging.)
    recent = history[-MAX_HISTORY_TURNS:]
    last_user_idx = max(
        (i for i, t in enumerate(recent) if t["role"] == "user"),
        default=-1,
    )
    messages = []
    for i, turn in enumerate(recent):
        guard_this_turn = i == last_user_idx and bool(GUARDRAIL_ID)
        block = (
            {"guardContent": {"text": {"text": turn["text"]}}}
            if guard_this_turn
            else {"text": turn["text"]}
        )
        messages.append({"role": turn["role"], "content": [block]})

    for turn_index in range(MAX_TURNS):
        converse_t0 = time.perf_counter()
        resp = bedrock.converse(**_converse_kwargs(messages, default_range))
        converse_ms = round((time.perf_counter() - converse_t0) * 1000, 2)
        stop = resp.get("stopReason")
        _log_json(
            "ask_converse_timing",
            turn=turn_index + 1,
            stop_reason=stop,
            duration_ms=converse_ms,
        )
        out_msg = resp["output"]["message"]
        messages.append(out_msg)

        # Guardrail blocked the prompt or response — return its safe message.
        if stop == "guardrail_intervened":
            text = _content_text(out_msg["content"])
            return text or "That request was blocked by the usage policy. Ask me about your Bedrock usage or cost instead."

        if stop != "tool_use":
            text = _content_text(out_msg["content"])
            if text:
                return text
            _log_json(
                "ask_empty_answer",
                stop_reason=stop,
                content_keys=_content_keys(out_msg.get("content", [])),
            )
            return "I could not generate a text answer for that request. Try asking again or narrowing the time range."

        tool_results = []
        for block in out_msg["content"]:
            if "toolUse" in block:
                tu = block["toolUse"]
                result = _run_tool(tu["name"], tu.get("input", {}), default_range)
                tool_results.append(
                    {"toolResult": {"toolUseId": tu["toolUseId"], "content": [{"json": result}]}}
                )
        messages.append({"role": "user", "content": tool_results})

    return "I couldn't complete that within the allowed steps. Try a more specific question."


if __name__ == "__main__":
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "Summarize my Bedrock usage this week."
    region = sys.argv[2] if len(sys.argv) > 2 else "ap-northeast-1"
    os.environ.setdefault("REGIONS", region)
    print(ask(q, region=region))
