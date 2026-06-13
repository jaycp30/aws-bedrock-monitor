"""
Bedrock usage & cost dashboard — backend Lambda.

Data layer ported from bedrock-lens (CloudWatch model-invocation LOGS) for maximum
precision: exact per-invocation tokens including prompt-cache read/write. Cost is
computed from LIVE AWS Price List API rates (per model, regional vs global) and
reconciled against actual billed cost from Cost Explorer.

Routes (API Gateway HTTP API, payload v2.0):
  GET  /usage?range=today|7d|30d|90d   → metrics payload
  POST /ask    {"question": "..."}      → Guardrailed Bedrock agent answer

Local:  python app.py ap-northeast-1 30d
"""

import datetime as dt
import json
import os
import time
from collections import defaultdict

import boto3
from botocore.config import Config

import pricing
from bedrock_logs import aggregate, iter_log_events

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
DEFAULT_REGIONS = ["ap-northeast-1"]
REGIONS = [r.strip() for r in os.environ.get("REGIONS", ",".join(DEFAULT_REGIONS)).split(",") if r.strip()]

# Region whose price list to use (token prices vary slightly by region). Defaults
# to the first monitored region so the sandbox (Tokyo) gets Tokyo prices.
PRICING_REGION = os.environ.get("PRICING_REGION", REGIONS[0] if REGIONS else "us-east-1")

# Control-plane region for model names / inference-profile prefixes.
CONTROL_REGION = os.environ.get("AWS_REGION") or PRICING_REGION

CE_REGION = os.environ.get("CE_REGION", "us-east-1")
CE_CACHE_TTL_SECONDS = int(os.environ.get("CE_CACHE_TTL_SECONDS", str(6 * 3600)))
CE_CACHE_DIR = os.environ.get("CE_CACHE_DIR", "/tmp")
MAX_RANGE_DAYS = int(os.environ.get("MAX_RANGE_DAYS", "90"))
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(64 * 1024)))
USAGE_CACHE_TTL_SECONDS = int(os.environ.get("USAGE_CACHE_TTL_SECONDS", "60"))

_BOTO_CFG = Config(retries={"max_attempts": 3, "mode": "standard"})

_pricing_ready = False
_usage_cache = {}


def _ensure_pricing():
    """Initialise the live price table once per container."""
    global _pricing_ready
    if _pricing_ready:
        return
    bedrock = boto3.client("bedrock", region_name=CONTROL_REGION, config=_BOTO_CFG)
    pricing.init_pricing(PRICING_REGION, bedrock_client=bedrock)
    _pricing_ready = True


def _log_json(event: str, **fields):
    """Emit compact structured logs for CloudWatch without leaking payload data."""
    print(json.dumps({"event": event, **fields}, default=str))


def _usage_cache_key(params: dict) -> str:
    """Stable cache key for the sanitized query params used by /usage."""
    return json.dumps(params, sort_keys=True, default=str, separators=(",", ":"))


def _prune_usage_cache(now: float) -> None:
    expired = [
        key for key, entry in _usage_cache.items()
        if now - entry["created_at"] >= USAGE_CACHE_TTL_SECONDS
    ]
    for key in expired:
        _usage_cache.pop(key, None)


def get_usage_payload(params: dict) -> dict:
    """Return a short-lived cached /usage payload for warm Lambda containers."""
    if USAGE_CACHE_TTL_SECONDS <= 0:
        return build_payload(params)

    now = time.time()
    key = _usage_cache_key(params)
    entry = _usage_cache.get(key)
    if entry and now - entry["created_at"] < USAGE_CACHE_TTL_SECONDS:
        payload = entry["payload"]
        _log_json(
            "usage_cache",
            hit=True,
            age_ms=round((now - entry["created_at"]) * 1000, 2),
            range=payload.get("range", {}).get("label"),
            regions=payload.get("regions", []),
        )
        return payload

    payload = build_payload(params)
    _prune_usage_cache(now)
    _usage_cache[key] = {"created_at": now, "payload": payload}
    _log_json(
        "usage_cache",
        hit=False,
        ttl_seconds=USAGE_CACHE_TTL_SECONDS,
        range=payload.get("range", {}).get("label"),
        regions=payload.get("regions", []),
    )
    return payload


# --------------------------------------------------------------------------- #
# Time range
# --------------------------------------------------------------------------- #
def _resolve_range(params: dict):
    """Return (start, end, period_seconds, label) in UTC."""
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    rng = (params.get("range") or "today").lower()
    if rng == "custom" and params.get("start") and params.get("end"):
        try:
            start = dt.datetime.fromisoformat(params["start"]).astimezone(dt.timezone.utc)
            end = dt.datetime.fromisoformat(params["end"]).astimezone(dt.timezone.utc)
        except ValueError as exc:
            raise ValueError("invalid custom date range") from exc
        if end <= start:
            raise ValueError("custom range end must be after start")
        if end > now:
            end = now
        if (end - start).days > MAX_RANGE_DAYS:
            raise ValueError(f"custom range cannot exceed {MAX_RANGE_DAYS} days")
        period = 3600 if (end - start).total_seconds() <= 3 * 86400 else 86400
        return start, end, period, "custom"
    if rng in ("today", "day", "24h"):
        return now.replace(hour=0, minute=0, second=0), now, 3600, "today"
    if rng in ("week", "7d"):
        return now - dt.timedelta(days=7), now, 3600, "7d"
    if rng in ("month", "30d"):
        return now - dt.timedelta(days=30), now, 86400, "30d"
    if rng in ("90d", "quarter"):
        return now - dt.timedelta(days=90), now, 86400, "90d"
    return now.replace(hour=0, minute=0, second=0), now, 3600, "today"


# --------------------------------------------------------------------------- #
# Cost Explorer billed cost (cached)
# --------------------------------------------------------------------------- #
def _billed_cost_by_region(start, end) -> dict:
    start_d = start.date().isoformat()
    end_d = (end.date() + dt.timedelta(days=1)).isoformat()
    cache = os.path.join(CE_CACHE_DIR, f"ce_{start_d}_{end_d}.json")
    if os.path.exists(cache) and (time.time() - os.path.getmtime(cache)) < CE_CACHE_TTL_SECONDS:
        try:
            return json.load(open(cache))
        except (OSError, ValueError):
            pass
    out = {}
    try:
        ce = boto3.client("ce", region_name=CE_REGION, config=_BOTO_CFG)
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start_d, "End": end_d},
            Granularity="MONTHLY" if (end - start).days > 2 else "DAILY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Bedrock"]}},
            GroupBy=[{"Type": "DIMENSION", "Key": "REGION"}],
        )
        for block in resp.get("ResultsByTime", []):
            for g in block.get("Groups", []):
                region = g["Keys"][0] or "global"
                out[region] = round(out.get(region, 0.0) + float(g["Metrics"]["UnblendedCost"]["Amount"]), 6)
        with open(cache, "w") as fh:
            json.dump(out, fh)
    except Exception as exc:  # noqa: BLE001
        out = {"_error": str(exc)}
    return out


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def build_payload(params: dict) -> dict:
    timings = {}
    regions = params.get("_regions") or REGIONS
    start, end, period, label = _resolve_range(params)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    t0 = time.perf_counter()
    _ensure_pricing()
    timings["pricing_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    prefixes = pricing.get_cross_region_prefixes()
    warnings = []

    by_model = []
    region_acc = {r: _empty_region(r) for r in regions}
    merged_series = defaultdict(lambda: {"input_tokens": 0, "output_tokens": 0, "invocations": 0})
    needs_pricing = set()

    for region in regions:
        region_t0 = time.perf_counter()
        try:
            logs = boto3.client("logs", region_name=region, config=_BOTO_CFG)
            usage, series = aggregate(
                iter_log_events(logs, start_ms, end_ms), prefixes, period
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{region}: {exc}")
            continue
        finally:
            timings[f"logs_{region}_ms"] = round((time.perf_counter() - region_t0) * 1000, 2)

        for model, d in usage.items():
            cost = pricing.calculate_cost(
                d["raw_id"], d["input_tokens"], d["output_tokens"],
                d["cache_write_tokens"], d["cache_read_tokens"], prefer_global=d["is_global"],
            )
            p = pricing.lookup(d["raw_id"], prefer_global=d["is_global"])
            if p.needs_pricing:
                needs_pricing.add(model)
            total = d["input_tokens"] + d["output_tokens"] + d["cache_write_tokens"] + d["cache_read_tokens"]
            by_model.append({
                "region": region,
                "model_id": model,
                "display_name": p.display_name,
                "input_tokens": d["input_tokens"],
                "output_tokens": d["output_tokens"],
                "cache_write_tokens": d["cache_write_tokens"],
                "cache_read_tokens": d["cache_read_tokens"],
                "total_tokens": total,
                "invocations": d["calls"],
                "estimated_cost": round(cost, 6),
                "needs_pricing": p.needs_pricing,
            })
            a = region_acc[region]
            a["input_tokens"] += d["input_tokens"]
            a["output_tokens"] += d["output_tokens"]
            a["cache_write_tokens"] += d["cache_write_tokens"]
            a["cache_read_tokens"] += d["cache_read_tokens"]
            a["invocations"] += d["calls"]
            a["estimated_cost"] += cost

        for bucket, vals in series.items():
            for k in ("input_tokens", "output_tokens", "invocations"):
                merged_series[bucket][k] += vals[k]

    ce_t0 = time.perf_counter()
    billed = _billed_cost_by_region(start, end)
    timings["cost_explorer_ms"] = round((time.perf_counter() - ce_t0) * 1000, 2)
    if isinstance(billed, dict) and "_error" in billed:
        warnings.append(f"Cost Explorer unavailable: {billed['_error']}")
        billed = {}

    by_region = []
    for r in regions:
        a = region_acc[r]
        a["total_tokens"] = a["input_tokens"] + a["output_tokens"] + a["cache_write_tokens"] + a["cache_read_tokens"]
        a["estimated_cost"] = round(a["estimated_cost"], 6)
        a["billed_cost"] = round(billed.get(r, 0.0), 6)
        by_region.append(a)

    series = [
        {"t": b, **{k: int(v[k]) for k in ("input_tokens", "output_tokens", "invocations")}}
        for b, v in sorted(merged_series.items())
    ]

    totals = {k: sum(r[k] for r in by_region) for k in
              ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens", "total_tokens", "invocations")}
    totals["estimated_cost"] = round(sum(r["estimated_cost"] for r in by_region), 6)
    totals["billed_cost"] = round(sum(r["billed_cost"] for r in by_region), 6)

    if needs_pricing:
        warnings.append(
            "No live price found for: " + ", ".join(sorted(needs_pricing))
            + " (cost shown as 0 for these)."
        )

    _log_json(
        "usage_build_timing",
        range=label,
        regions=regions,
        period_seconds=period,
        model_count=len(by_model),
        warning_count=len(warnings),
        **timings,
    )

    return {
        "range": {"start": start.isoformat(), "end": end.isoformat(), "period_seconds": period, "label": label},
        "regions": regions,
        "totals": totals,
        "by_region": by_region,
        "by_model": sorted(by_model, key=lambda m: m["estimated_cost"], reverse=True),
        "series": series,
        "warnings": warnings,
        "pricing_region": PRICING_REGION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def _empty_region(r):
    return {
        "region": r, "input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0,
        "cache_read_tokens": 0, "total_tokens": 0, "invocations": 0,
        "estimated_cost": 0.0, "billed_cost": 0.0,
    }


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
_CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "http://localhost:5173")
_CORS = {
    "Access-Control-Allow-Origin": _CORS_ORIGIN,
    "Access-Control-Allow-Headers": "authorization,content-type",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Max-Age": "600",
    "X-Content-Type-Options": "nosniff",
    "Content-Type": "application/json",
}


def _resp(code, body):
    return {"statusCode": code, "headers": _CORS, "body": json.dumps(body)}


# Chat request limits — the browser carries the conversation; never trust it.
MAX_CHAT_TURNS = 20
MAX_TURN_CHARS = 4000


def _validate_chat(body):
    """Return a clean [{role, text}] history, or an error string.

    Accepts {"messages": [{role, text}, ...]} (multi-turn) or the legacy
    {"question": "..."} shape. The history must alternate user/assistant and
    end with a user message.
    """
    if isinstance(body.get("messages"), list):
        raw = body["messages"]
    elif (body.get("question") or "").strip():
        raw = [{"role": "user", "text": body["question"]}]
    else:
        return "messages (or question) is required"

    if not raw:
        return "messages must not be empty"
    if len(raw) > MAX_CHAT_TURNS:
        raw = raw[-MAX_CHAT_TURNS:]

    history = []
    for turn in raw:
        if not isinstance(turn, dict):
            return "each message must be an object"
        role = turn.get("role")
        raw_text = turn.get("text")
        if role not in ("user", "assistant"):
            return "message role must be user or assistant"
        if not isinstance(raw_text, str):
            return "message text must be a string"
        text = raw_text.strip()
        if not text:
            return "message text must not be empty"
        history.append({"role": role, "text": text[:MAX_TURN_CHARS]})

    if history[-1]["role"] != "user":
        return "the last message must be from the user"
    for prev, cur in zip(history, history[1:]):
        if prev["role"] == cur["role"]:
            return "messages must alternate between user and assistant"
    return history


def handler(event, context):
    started = time.perf_counter()
    event = event or {}
    ctx = event.get("requestContext", {}).get("http", {})
    method = ctx.get("method", "GET")
    path = ctx.get("path", "") or event.get("rawPath", "")

    def finish(code, body):
        _log_json(
            "http_request",
            method=method,
            path=path,
            status=code,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return _resp(code, body)

    if method == "OPTIONS":
        return finish(204, {})

    if path.endswith("/ask") and method == "POST":
        try:
            raw_body = event.get("body") or "{}"
            if len(raw_body.encode("utf-8")) > MAX_BODY_BYTES:
                return finish(413, {"error": "request body too large"})
            body = json.loads(raw_body)
            history = _validate_chat(body)
            if isinstance(history, str):  # validation error message
                return finish(400, {"error": history})
            import ask  # lazy import avoids loading bedrock-runtime for /usage
            ask_t0 = time.perf_counter()
            answer = ask.ask_chat(history, region=CONTROL_REGION)
            _log_json(
                "ask_timing",
                turns=len(history),
                ask_chat_ms=round((time.perf_counter() - ask_t0) * 1000, 2),
            )
            return finish(200, {"answer": answer})
        except json.JSONDecodeError:
            return finish(400, {"error": "invalid JSON body"})
        except Exception as exc:  # noqa: BLE001
            print(f"/ask failed: {type(exc).__name__}: {exc}")
            return finish(500, {"error": "Ask is temporarily unavailable."})

    if not path.endswith("/usage"):
        return finish(404, {"error": "not found"})
    if method != "GET":
        return finish(405, {"error": "method not allowed"})

    params = event.get("queryStringParameters") or {}
    if params.get("regions"):
        requested = [r.strip() for r in params["regions"].split(",") if r.strip()]
        params["_regions"] = [r for r in requested if r in REGIONS] or REGIONS
    try:
        return finish(200, get_usage_payload(params))
    except ValueError as exc:
        return finish(400, {"error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        print(f"/usage failed: {type(exc).__name__}: {exc}")
        return finish(500, {"error": "Usage data is temporarily unavailable."})


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        os.environ["REGIONS"] = sys.argv[1]
        REGIONS = [r.strip() for r in sys.argv[1].split(",") if r.strip()]
        PRICING_REGION = REGIONS[0]
        CONTROL_REGION = REGIONS[0]
    print(json.dumps(build_payload({"range": sys.argv[2] if len(sys.argv) > 2 else "30d"}), indent=2))
