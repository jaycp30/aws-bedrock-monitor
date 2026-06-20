"""
Bedrock usage & cost dashboard — backend Lambda.

Data layer ported from bedrock-lens (CloudWatch model-invocation LOGS) for maximum
precision: exact per-invocation tokens including prompt-cache read/write. Cost is
computed from LIVE AWS Price List API rates (per model, regional vs global) and
reconciled against actual billed cost from Cost Explorer.

Routes (API Gateway HTTP API, payload v2.0):
  GET  /usage?range=today|7d|30d|90d   → metrics payload
  POST /ask    {"question": "..."}      → async Guardrailed Bedrock agent job

EventBridge:
  {"warmup": true}                      → prebuild shared /usage cache

Local:  python app.py ap-northeast-1 30d
"""

import datetime as dt
import hashlib
import json
import os
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

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
USAGE_STALE_TTL_SECONDS = int(os.environ.get("USAGE_STALE_TTL_SECONDS", str(24 * 3600)))
USAGE_REGION_WORKERS = max(1, int(os.environ.get("USAGE_REGION_WORKERS", "5")))
USAGE_SHARED_CACHE_BUCKET = os.environ.get("USAGE_SHARED_CACHE_BUCKET", "")
USAGE_SHARED_CACHE_PREFIX = os.environ.get("USAGE_SHARED_CACHE_PREFIX", "usage/v1")
USAGE_WARMUP_RANGES = [
    r.strip()
    for r in os.environ.get("USAGE_WARMUP_RANGES", "today,7d,30d,90d").split(",")
    if r.strip()
]
ASK_JOBS_TABLE = os.environ.get("ASK_JOBS_TABLE", "")
ASK_WORKER_FUNCTION = os.environ.get("ASK_WORKER_FUNCTION", "")
ASK_JOB_TTL_SECONDS = int(os.environ.get("ASK_JOB_TTL_SECONDS", str(6 * 3600)))

_BOTO_CFG = Config(retries={"max_attempts": 3, "mode": "standard"})

_pricing_ready = False
_usage_cache = {}
_s3_cache_client = None
_jobs_table = None
_lambda_client = None


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


def _usage_shared_cache_key(cache_key: str) -> str:
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    return f"{USAGE_SHARED_CACHE_PREFIX.rstrip('/')}/{digest}.json"


def _get_s3_cache_client():
    global _s3_cache_client
    if not USAGE_SHARED_CACHE_BUCKET:
        return None
    if _s3_cache_client is None:
        _s3_cache_client = boto3.client("s3", region_name=CONTROL_REGION, config=_BOTO_CFG)
    return _s3_cache_client


def _get_jobs_table():
    global _jobs_table
    if not ASK_JOBS_TABLE:
        return None
    if _jobs_table is None:
        _jobs_table = boto3.resource("dynamodb", region_name=CONTROL_REGION, config=_BOTO_CFG).Table(ASK_JOBS_TABLE)
    return _jobs_table


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda", region_name=CONTROL_REGION, config=_BOTO_CFG)
    return _lambda_client


def _prune_usage_cache(now: float) -> None:
    expired = [
        key for key, entry in _usage_cache.items()
        if now - entry["created_at"] >= USAGE_STALE_TTL_SECONDS
    ]
    for key in expired:
        _usage_cache.pop(key, None)


def _read_shared_usage_cache_entry(cache_key: str, now: float, *, allow_stale: bool = False) -> dict | None:
    s3 = _get_s3_cache_client()
    if s3 is None:
        return None

    object_key = _usage_shared_cache_key(cache_key)
    try:
        resp = s3.get_object(Bucket=USAGE_SHARED_CACHE_BUCKET, Key=object_key)
        entry = json.loads(resp["Body"].read())
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code not in ("NoSuchKey", "NoSuchBucket", "404"):
            _log_json("usage_shared_cache", hit=False, error=code, key=object_key)
        return None
    except Exception as exc:  # noqa: BLE001
        _log_json("usage_shared_cache", hit=False, error=str(exc), key=object_key)
        return None

    if entry.get("cache_key") != cache_key:
        _log_json("usage_shared_cache", hit=False, error="cache key mismatch", key=object_key)
        return None

    created_at = float(entry.get("created_at") or 0)
    age_seconds = now - created_at
    if age_seconds >= USAGE_STALE_TTL_SECONDS:
        _log_json(
            "usage_shared_cache",
            hit=False,
            expired=True,
            age_ms=round(age_seconds * 1000, 2),
            key=object_key,
        )
        return None
    if age_seconds >= USAGE_CACHE_TTL_SECONDS and not allow_stale:
        _log_json(
            "usage_shared_cache",
            hit=False,
            stale=True,
            age_ms=round(age_seconds * 1000, 2),
            key=object_key,
        )
        return None

    payload = entry.get("payload")
    if not isinstance(payload, dict):
        _log_json("usage_shared_cache", hit=False, error="payload missing", key=object_key)
        return None

    _log_json(
        "usage_shared_cache",
        hit=True,
        stale=age_seconds >= USAGE_CACHE_TTL_SECONDS,
        age_ms=round(age_seconds * 1000, 2),
        range=payload.get("range", {}).get("label"),
        regions=payload.get("regions", []),
    )
    return {"created_at": created_at, "payload": payload}


def _read_shared_usage_cache(cache_key: str, now: float) -> dict | None:
    entry = _read_shared_usage_cache_entry(cache_key, now)
    return entry["payload"] if entry else None


def _read_stale_usage_cache(cache_key: str, now: float) -> dict | None:
    entry = _usage_cache.get(cache_key)
    if entry and now - entry["created_at"] < USAGE_STALE_TTL_SECONDS:
        payload = entry["payload"]
        _log_json(
            "usage_cache",
            hit=True,
            source="memory_stale",
            age_ms=round((now - entry["created_at"]) * 1000, 2),
            range=payload.get("range", {}).get("label"),
            regions=payload.get("regions", []),
        )
        return payload

    shared_entry = _read_shared_usage_cache_entry(cache_key, now, allow_stale=True)
    if shared_entry is None:
        return None
    _usage_cache[cache_key] = shared_entry
    return shared_entry["payload"]


def _write_shared_usage_cache(cache_key: str, created_at: float, payload: dict) -> None:
    s3 = _get_s3_cache_client()
    if s3 is None:
        return

    object_key = _usage_shared_cache_key(cache_key)
    body = json.dumps(
        {"created_at": created_at, "cache_key": cache_key, "payload": payload},
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        s3.put_object(
            Bucket=USAGE_SHARED_CACHE_BUCKET,
            Key=object_key,
            Body=body,
            ContentType="application/json",
            CacheControl=f"max-age={USAGE_CACHE_TTL_SECONDS}",
        )
    except Exception as exc:  # noqa: BLE001
        _log_json("usage_shared_cache", write=False, error=str(exc), key=object_key)


def _with_stale_warning(payload: dict, reason: str) -> dict:
    """Return a response copy that tells the UI it is seeing cached data."""
    out = dict(payload)
    warnings = list(out.get("warnings") or [])
    generated_at = out.get("generated_at", "an earlier run")
    warnings.append(f"Showing cached usage from {generated_at}; fresh refresh {reason}.")
    out["warnings"] = warnings
    out["stale"] = True
    return out


def get_usage_payload(params: dict, *, prefer_stale: bool = False, force_refresh: bool = False) -> dict:
    """Return a cached /usage payload, shared across warm Lambda containers when configured."""
    if USAGE_CACHE_TTL_SECONDS <= 0:
        return build_payload(params)

    now = time.time()
    key = _usage_cache_key(params)
    if not force_refresh:
        entry = _usage_cache.get(key)
        if entry and now - entry["created_at"] < USAGE_CACHE_TTL_SECONDS:
            payload = entry["payload"]
            _log_json(
                "usage_cache",
                hit=True,
                source="memory",
                age_ms=round((now - entry["created_at"]) * 1000, 2),
                range=payload.get("range", {}).get("label"),
                regions=payload.get("regions", []),
            )
            return payload

        payload = _read_shared_usage_cache(key, now)
        if payload is not None:
            _prune_usage_cache(now)
            _usage_cache[key] = {"created_at": now, "payload": payload}
            _log_json(
                "usage_cache",
                hit=True,
                source="shared",
                range=payload.get("range", {}).get("label"),
                regions=payload.get("regions", []),
            )
            return payload

        if prefer_stale:
            stale_payload = _read_stale_usage_cache(key, now)
            if stale_payload is not None:
                return _with_stale_warning(stale_payload, "is running in the background")

    try:
        payload = build_payload(params)
    except Exception:
        stale_payload = _read_stale_usage_cache(key, now)
        if stale_payload is not None:
            _log_json(
                "usage_cache",
                hit=True,
                source="stale_after_build_error",
                range=stale_payload.get("range", {}).get("label"),
                regions=stale_payload.get("regions", []),
            )
            return _with_stale_warning(stale_payload, "failed")
        raise

    _prune_usage_cache(now)
    _usage_cache[key] = {"created_at": now, "payload": payload}
    _write_shared_usage_cache(key, now, payload)
    _log_json(
        "usage_cache",
        hit=False,
        source="build",
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
def _load_region_usage(region: str, start_ms: int, end_ms: int, prefixes, period: int) -> dict:
    """Read and aggregate one region's Bedrock invocation logs."""
    t0 = time.perf_counter()
    try:
        logs = boto3.client("logs", region_name=region, config=_BOTO_CFG)
        usage, series, users = aggregate(
            iter_log_events(logs, start_ms, end_ms), prefixes, period
        )
        return {
            "region": region,
            "usage": usage,
            "series": series,
            "users": users,
            "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "region": region,
            "usage": {},
            "series": {},
            "users": {},
            "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
            "error": str(exc),
        }


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
    user_acc = {}
    region_acc = {r: _empty_region(r) for r in regions}
    merged_series = defaultdict(lambda: {"input_tokens": 0, "output_tokens": 0, "invocations": 0})
    needs_pricing = set()

    worker_count = min(len(regions), USAGE_REGION_WORKERS) if regions else 1
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [
            pool.submit(_load_region_usage, region, start_ms, end_ms, prefixes, period)
            for region in regions
        ]

        region_results = [future.result() for future in as_completed(futures)]

    for result in region_results:
        region = result["region"]
        timings[f"logs_{region}_ms"] = result["duration_ms"]
        if result["error"]:
            warnings.append(f"{region}: {result['error']}")
            continue

        usage = result["usage"]
        series = result["series"]
        users = result["users"]

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

        for principal, user_data in users.items():
            u = user_acc.setdefault(
                principal,
                {
                    "principal": principal,
                    "identity_arn": user_data.get("identity_arn", ""),
                    "regions": set(),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_write_tokens": 0,
                    "cache_read_tokens": 0,
                    "total_tokens": 0,
                    "invocations": 0,
                    "estimated_cost": 0.0,
                },
            )
            u["regions"].add(region)
            for model_data in user_data.get("models", {}).values():
                cost = pricing.calculate_cost(
                    model_data["raw_id"],
                    model_data["input_tokens"],
                    model_data["output_tokens"],
                    model_data["cache_write_tokens"],
                    model_data["cache_read_tokens"],
                    prefer_global=model_data["is_global"],
                )
                u["input_tokens"] += model_data["input_tokens"]
                u["output_tokens"] += model_data["output_tokens"]
                u["cache_write_tokens"] += model_data["cache_write_tokens"]
                u["cache_read_tokens"] += model_data["cache_read_tokens"]
                u["invocations"] += model_data["calls"]
                u["estimated_cost"] += cost

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

    by_user = []
    for u in user_acc.values():
        u["regions"] = sorted(u["regions"])
        u["total_tokens"] = (
            u["input_tokens"]
            + u["output_tokens"]
            + u["cache_write_tokens"]
            + u["cache_read_tokens"]
        )
        u["estimated_cost"] = round(u["estimated_cost"], 6)
        by_user.append(u)

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
        "by_user": sorted(by_user, key=lambda u: u["total_tokens"], reverse=True),
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
ASK_RANGES = {"today", "7d", "30d", "90d"}


def _ask_range(body: dict) -> str:
    rng = body.get("range")
    return rng if isinstance(rng, str) and rng in ASK_RANGES else "7d"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _request_owner(event: dict) -> str:
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    return claims.get("sub") or claims.get("username") or claims.get("email") or "anonymous"


def _job_public_view(item: dict) -> dict:
    body = {
        "job_id": item["job_id"],
        "status": item.get("status", "queued"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }
    if item.get("answer"):
        body["answer"] = item["answer"]
    if item.get("error"):
        body["error"] = item["error"]
    return body


def _create_ask_job(owner: str, history: list[dict], ask_range: str) -> dict:
    table = _get_jobs_table()
    if table is None or not ASK_WORKER_FUNCTION:
        raise RuntimeError("ask async infrastructure is not configured")

    now = int(time.time())
    job_id = uuid.uuid4().hex
    item = {
        "job_id": job_id,
        "owner": owner,
        "status": "queued",
        "range": ask_range,
        "history": history,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "expires_at": now + ASK_JOB_TTL_SECONDS,
    }
    table.put_item(
        Item=item,
        ConditionExpression="attribute_not_exists(job_id)",
    )

    payload = {
        "job_id": job_id,
        "owner": owner,
        "history": history,
        "range": ask_range,
    }
    try:
        _get_lambda_client().invoke(
            FunctionName=ASK_WORKER_FUNCTION,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
    except Exception:
        table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :status, #e = :error, updated_at = :updated_at",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={
                ":status": "failed",
                ":error": "Ask worker could not be started.",
                ":updated_at": _now_iso(),
            },
        )
        raise

    return _job_public_view(item)


def _get_ask_job(job_id: str, owner: str) -> tuple[int, dict]:
    table = _get_jobs_table()
    if table is None:
        return 500, {"error": "Ask job storage is not configured."}
    if not job_id or len(job_id) > 80:
        return 400, {"error": "invalid ask job id"}

    resp = table.get_item(Key={"job_id": job_id}, ConsistentRead=True)
    item = resp.get("Item")
    if not item:
        return 404, {"error": "Ask job not found."}
    if item.get("owner") != owner:
        return 404, {"error": "Ask job not found."}
    return 200, _job_public_view(item)


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


def _warm_usage_cache(event: dict, context) -> dict:
    ranges = event.get("ranges") if isinstance(event.get("ranges"), list) else USAGE_WARMUP_RANGES
    ranges = [r for r in ranges if isinstance(r, str) and r in ASK_RANGES]
    if not ranges:
        ranges = ["today", "7d", "30d"]

    results = []
    for rng in ranges:
        remaining_ms = context.get_remaining_time_in_millis() if context else 90000
        if remaining_ms < 25000:
            _log_json("usage_warmup_skip", range=rng, remaining_ms=remaining_ms)
            results.append({"range": rng, "status": "skipped", "remaining_ms": remaining_ms})
            continue

        t0 = time.perf_counter()
        try:
            payload = get_usage_payload({"range": rng}, force_refresh=True)
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            _log_json(
                "usage_warmup",
                range=rng,
                status="ok",
                duration_ms=duration_ms,
                generated_at=payload.get("generated_at"),
            )
            results.append({"range": rng, "status": "ok", "duration_ms": duration_ms})
        except Exception as exc:  # noqa: BLE001
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            _log_json(
                "usage_warmup",
                range=rng,
                status="error",
                duration_ms=duration_ms,
                error=str(exc),
            )
            results.append({"range": rng, "status": "error", "duration_ms": duration_ms, "error": str(exc)})

    return {"ok": True, "results": results}


def handler(event, context):
    started = time.perf_counter()
    event = event or {}
    if event.get("warmup") is True:
        return _warm_usage_cache(event, context)

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
            ask_range = _ask_range(body)
            job = _create_ask_job(_request_owner(event), history, ask_range)
            _log_json(
                "ask_job_created",
                job_id=job["job_id"],
                turns=len(history),
                default_range=ask_range,
            )
            return finish(202, job)
        except json.JSONDecodeError:
            return finish(400, {"error": "invalid JSON body"})
        except Exception as exc:  # noqa: BLE001
            print(f"/ask failed: {type(exc).__name__}: {exc}")
            return finish(500, {"error": "Ask is temporarily unavailable."})

    if method == "GET" and "/ask/" in path:
        job_id = path.rstrip("/").rsplit("/", 1)[-1]
        code, body = _get_ask_job(job_id, _request_owner(event))
        return finish(code, body)

    if not path.endswith("/usage"):
        return finish(404, {"error": "not found"})
    if method != "GET":
        return finish(405, {"error": "method not allowed"})

    params = event.get("queryStringParameters") or {}
    if params.get("regions"):
        requested = [r.strip() for r in params["regions"].split(",") if r.strip()]
        params["_regions"] = [r for r in requested if r in REGIONS] or REGIONS
    try:
        return finish(200, get_usage_payload(params, prefer_stale=True))
    except ValueError as exc:
        return finish(400, {"error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        print(f"/usage failed: {type(exc).__name__}: {exc}")
        return finish(500, {"error": "Usage data is temporarily unavailable."})


def ask_worker_handler(event, context):
    """Async Lambda entrypoint for long-running Ask requests."""
    started = time.perf_counter()
    table = _get_jobs_table()
    if table is None:
        raise RuntimeError("ASK_JOBS_TABLE is not configured")

    event = event or {}
    job_id = event.get("job_id")
    owner = event.get("owner")
    history = event.get("history")
    ask_range = event.get("range")
    if not job_id or not owner or not isinstance(history, list):
        raise ValueError("invalid ask worker payload")

    def update(status: str, **fields):
        names = {"#s": "status"}
        values = {":status": status, ":updated_at": _now_iso()}
        assignments = ["#s = :status", "updated_at = :updated_at"]
        for key, value in fields.items():
            names[f"#{key}"] = key
            values[f":{key}"] = value
            assignments.append(f"#{key} = :{key}")
        table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET " + ", ".join(assignments),
            ConditionExpression="#owner = :owner",
            ExpressionAttributeNames={**names, "#owner": "owner"},
            ExpressionAttributeValues={**values, ":owner": owner},
        )

    try:
        update("running")
        import ask  # lazy import avoids loading bedrock-runtime for /usage
        ask_t0 = time.perf_counter()
        answer = ask.ask_chat(history, region=CONTROL_REGION, default_range=ask_range)
        ask_chat_ms = round((time.perf_counter() - ask_t0) * 1000, 2)
        if not isinstance(answer, str) or not answer.strip():
            update("failed", error="Ask returned no text answer. Try again or narrow the time range.")
            _log_json(
                "ask_empty_answer",
                job_id=job_id,
                turns=len(history),
                default_range=ask_range,
                ask_chat_ms=ask_chat_ms,
            )
            return {"ok": False, "error": "empty answer"}
        update("complete", answer=answer)
        _log_json(
            "ask_timing",
            job_id=job_id,
            turns=len(history),
            default_range=ask_range,
            ask_chat_ms=ask_chat_ms,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        print(f"ask worker failed: {type(exc).__name__}: {exc}")
        try:
            update("failed", error="Ask is temporarily unavailable.")
        except Exception as update_exc:  # noqa: BLE001
            print(f"ask worker failed to update job: {type(update_exc).__name__}: {update_exc}")
        raise


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        os.environ["REGIONS"] = sys.argv[1]
        REGIONS = [r.strip() for r in sys.argv[1].split(",") if r.strip()]
        PRICING_REGION = REGIONS[0]
        CONTROL_REGION = REGIONS[0]
    print(json.dumps(build_payload({"range": sys.argv[2] if len(sys.argv) > 2 else "30d"}), indent=2))
