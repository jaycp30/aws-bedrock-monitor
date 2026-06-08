"""
Bedrock model-invocation log reader.

Ported from bedrock-lens (github.com/OmarCodes022/bedrock-lens, MIT) and adapted
for a multi-region serverless dashboard. Reads exact per-invocation token counts
— including prompt-cache read/write tokens — from the CloudWatch log group that
Bedrock model-invocation logging writes to. This is the most precise usage source
(metrics omit cache-token detail).

`filter_log_events` is used (not Logs Insights), so there is no per-GB scan
charge — just standard API calls.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Generator

from botocore.exceptions import ClientError

LOG_GROUP = "/aws/bedrock/model-invocations"


def iter_log_events(client, start_ms: int, end_ms: int) -> Generator[dict, None, None]:
    """Yield parsed Bedrock ModelInvocationLog records from one region's log group.

    Each record is the raw JSON with `_eventId`, `_ingestionTime`, and
    `_timestamp` (event time, epoch ms) added for dedup and time-bucketing.
    Returns silently if the log group does not exist (logging not enabled here).
    """
    kwargs: dict = {
        "logGroupName": LOG_GROUP,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 10_000,
    }
    while True:
        try:
            resp = client.filter_log_events(**kwargs)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                return  # logging not enabled in this region
            raise

        for event in resp.get("events", []):
            try:
                record = json.loads(event["message"])
            except (json.JSONDecodeError, KeyError):
                continue
            if record.get("schemaType") != "ModelInvocationLog":
                continue
            record["_eventId"] = event.get("eventId", "")
            record["_ingestionTime"] = event.get("ingestionTime", 0)
            record["_timestamp"] = event.get("timestamp", 0)
            yield record

        token = resp.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token


def normalize_model_id(model_id: str, cross_region_prefixes: tuple[str, ...]) -> str:
    """Merge all variants of a model into one id.

    Strips an ARN down to the model/profile id, removes the cross-region
    geographic prefix (us./eu./ap./global.…), and drops a trailing version
    suffix (e.g. ':0').
    """
    if model_id.startswith("arn:"):
        model_id = model_id.split("/")[-1]
    for prefix in cross_region_prefixes:
        if model_id.startswith(prefix):
            model_id = model_id[len(prefix):]
            break
    return re.sub(r":\d+$", "", model_id)


def _bucket(ts_ms: int, period_seconds: int) -> str:
    """Floor an epoch-ms timestamp to a period boundary; return ISO string."""
    if ts_ms <= 0:
        return ""
    secs = (ts_ms // 1000) // period_seconds * period_seconds
    return dt.datetime.fromtimestamp(secs, tz=dt.timezone.utc).isoformat()


def aggregate(records, cross_region_prefixes, period_seconds, into=None):
    """Sum tokens/calls per normalized modelId, and build a time series.

    Returns (usage, series):
      usage[model] = {calls, input_tokens, output_tokens,
                      cache_write_tokens, cache_read_tokens, is_global, raw_id}
      series[bucket_iso] = {input_tokens, output_tokens, invocations}
    """
    usage: dict[str, dict] = into if into is not None else {}
    series: dict[str, dict] = {}

    for r in records:
        raw_id = r.get("modelId", "unknown")
        is_global = raw_id.lower().startswith("global.")
        model = normalize_model_id(raw_id, cross_region_prefixes)

        inp_data = r.get("input") or {}
        inp = inp_data.get("inputTokenCount") or 0
        cw = inp_data.get("cacheWriteInputTokenCount") or 0
        cr = inp_data.get("cacheReadInputTokenCount") or 0
        out = (r.get("output") or {}).get("outputTokenCount") or 0

        slot = usage.setdefault(
            model,
            {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_write_tokens": 0,
                "cache_read_tokens": 0,
                "is_global": False,
                "raw_id": raw_id,
            },
        )
        slot["calls"] += 1
        slot["input_tokens"] += inp
        slot["output_tokens"] += out
        slot["cache_write_tokens"] += cw
        slot["cache_read_tokens"] += cr
        slot["is_global"] = slot["is_global"] or is_global

        b = _bucket(r.get("_timestamp", 0), period_seconds)
        if b:
            sb = series.setdefault(b, {"input_tokens": 0, "output_tokens": 0, "invocations": 0})
            sb["input_tokens"] += inp + cw + cr
            sb["output_tokens"] += out
            sb["invocations"] += 1

    return usage, series
