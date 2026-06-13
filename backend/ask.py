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

import boto3
from botocore.config import Config

# Haiku by default — cheap and plenty for this. Override per deploy/region.
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "jp.anthropic.claude-haiku-4-5-20251001-v1:0")
MAX_TURNS = int(os.environ.get("ASK_MAX_TURNS", "5"))
MAX_TOKENS = int(os.environ.get("ASK_MAX_TOKENS", "1600"))

# Guardrail (set by the SAM stack). When present, every Converse call is screened.
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

_BOTO_CFG = Config(retries={"max_attempts": 3, "mode": "standard"})

SYSTEM_PROMPT = (
    "You are the assistant for an AWS Bedrock usage & cost dashboard. "
    "Answer ONLY questions about this account's Bedrock usage: tokens, invocations, "
    "models, regions, prompt-cache usage, and cost (estimated and billed). "
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
                "region: input/output/cache tokens, invocations, estimated cost (live "
                "prices) and billed cost (Cost Explorer)."
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


def _run_tool(name: str, tool_input: dict) -> dict:
    from app import build_payload  # lazy import avoids circular import at load

    if name == "get_usage":
        return build_payload({"range": tool_input.get("range", "7d")})
    return {"error": f"unknown tool: {name}"}


def _converse_kwargs(messages):
    kwargs = {
        "modelId": MODEL_ID,
        "system": [{"text": SYSTEM_PROMPT}],
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


def ask(question: str, region: str | None = None) -> str:
    """Single-question entrypoint (CLI / legacy clients)."""
    return ask_chat([{"role": "user", "text": question}], region=region)


def ask_chat(history: list[dict], region: str | None = None) -> str:
    """Multi-turn entrypoint: history is [{role: user|assistant, text: str}, ...],
    oldest first, ending with the user's latest message."""
    bedrock = boto3.client(
        "bedrock-runtime",
        region_name=region or os.environ.get("AWS_REGION"),
        config=_BOTO_CFG,
    )
    messages = [
        {"role": turn["role"], "content": [{"text": turn["text"]}]}
        for turn in history[-MAX_HISTORY_TURNS:]
    ]

    for _ in range(MAX_TURNS):
        resp = bedrock.converse(**_converse_kwargs(messages))
        stop = resp.get("stopReason")
        out_msg = resp["output"]["message"]
        messages.append(out_msg)

        # Guardrail blocked the prompt or response — return its safe message.
        if stop == "guardrail_intervened":
            text = "".join(b.get("text", "") for b in out_msg["content"]).strip()
            return text or "That request was blocked by the usage policy. Ask me about your Bedrock usage or cost instead."

        if stop != "tool_use":
            return "".join(b.get("text", "") for b in out_msg["content"]).strip()

        tool_results = []
        for block in out_msg["content"]:
            if "toolUse" in block:
                tu = block["toolUse"]
                result = _run_tool(tu["name"], tu.get("input", {}))
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
