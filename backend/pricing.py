"""
Live Bedrock pricing.

Ported from bedrock-lens (github.com/OmarCodes022/bedrock-lens, MIT), trimmed for
Lambda: caches to /tmp, no Rich output, no interactive overrides. Prices are
fetched from the AWS Price List API so they stay current without code edits.

Strategy (priority order):
  1. AmazonBedrockFoundationModels CSV — Anthropic/Claude models, regional AND
     global (cross-region inference profile) rates, including cache read/write.
  2. AmazonBedrock get_products — fallback for non-Anthropic providers (Nova,
     Llama, Mistral …); regional rates only, no caching.
  3. Unknown → lookup() returns needs_pricing=True (cost shown as 0, flagged).

All prices are per 1,000,000 tokens (USD).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import NamedTuple

import boto3


def _ssl_context() -> ssl.SSLContext:
    """SSL context backed by certifi's CA bundle (a boto3 dependency).

    Works in Lambda and on local machines whose Python lacks system CA certs.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


_SSL_CTX = _ssl_context()

_PRICING_CACHE_PATH = os.environ.get("PRICING_CACHE_PATH", "/tmp/bedrock_pricing_cache.json")
_PRICING_CACHE_TTL = int(os.environ.get("PRICING_CACHE_TTL", str(86_400)))  # 24h

_DEFAULT_CROSS_REGION_PREFIXES: tuple[str, ...] = ("us.", "eu.", "ap.", "us-gov.", "global.")


class ModelPricing(NamedTuple):
    input_per_1m: float
    output_per_1m: float
    cache_write_per_1m: float
    cache_read_per_1m: float
    display_name: str
    needs_pricing: bool


# Module-level caches, populated by init_pricing().
_live_cache: dict[str, tuple] = {}    # regional rates
_global_cache: dict[str, tuple] = {}  # global-profile rates
_live_sorted: list = []
_global_sorted: list = []
_model_names: dict[str, str] = {}
_cross_region_prefixes: tuple[str, ...] = _DEFAULT_CROSS_REGION_PREFIXES

# CSV metric name → slot. Handles CamelCase (Claude ≤4.6) and snake_case (≥4.7).
_METRIC_SLOTS: dict[str, str] = {
    "InputTokenCount-Units": "input_regional",
    "input_tokens_standard-Units": "input_regional",
    "InputTokenCount_Global-Units": "input_global",
    "input_tokens_global_standard-Units": "input_global",
    "OutputTokenCount-Units": "output_regional",
    "output_tokens_standard-Units": "output_regional",
    "OutputTokenCount_Global-Units": "output_global",
    "output_tokens_global_standard-Units": "output_global",
    "CacheWriteInputTokenCount-Units": "cache_write_regional",
    "cache_write_tokens_standard-Units": "cache_write_regional",
    "CacheWriteInputTokenCount_Global-Units": "cache_write_global",
    "cache_write_tokens_global_standard-Units": "cache_write_global",
    "CacheReadInputTokenCount-Units": "cache_read_regional",
    "cache_read_tokens_standard-Units": "cache_read_regional",
    "CacheReadInputTokenCount_Global-Units": "cache_read_global",
    "cache_read_tokens_global_standard-Units": "cache_read_global",
}


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _fetch_live_csv(region: str) -> tuple[dict, dict]:
    """Fetch per-1M prices from the AmazonBedrockFoundationModels CSV for a region."""
    try:
        client = boto3.client("pricing", region_name="us-east-1")
        price_lists = client.list_price_lists(
            ServiceCode="AmazonBedrockFoundationModels",
            EffectiveDate=datetime(2030, 1, 1),  # future date → latest list
            CurrencyCode="USD",
        )
        target_arn = next(
            (pl["PriceListArn"] for pl in price_lists.get("PriceLists", [])
             if pl.get("RegionCode") == region),
            None,
        )
        if not target_arn:
            return {}, {}

        url = client.get_price_list_file_url(PriceListArn=target_arn, FileFormat="csv")["Url"]
        with urllib.request.urlopen(url, timeout=15, context=_SSL_CTX) as resp:  # noqa: S310 — AWS-signed URL
            raw_content = resp.read().decode("utf-8")

        # First 5 rows are AWS metadata; row 6 is the header.
        data_section = "".join(raw_content.splitlines(keepends=True)[5:])

        raw: dict[str, dict] = {}
        for row in csv.DictReader(io.StringIO(data_section)):
            service_name = row.get("serviceName", "").replace(" (Amazon Bedrock Edition)", "").strip()
            price_str = row.get("PricePerUnit", "")
            if not service_name or not price_str:
                continue
            metric = row.get("usageType", "").split(":")[-1].split("_", 1)[-1]
            slot = _METRIC_SLOTS.get(metric)
            if slot is None:
                continue
            try:
                price_f = float(price_str)
            except ValueError:
                continue
            key = _normalize(service_name)
            raw.setdefault(key, {"display": service_name})[slot] = price_f

        regional, global_ = {}, {}
        for key, data in raw.items():
            name = data["display"]
            inp_r, out_r = data.get("input_regional"), data.get("output_regional")
            inp_g, out_g = data.get("input_global"), data.get("output_global")
            if inp_r is not None and out_r is not None:
                regional[key] = (inp_r, out_r, data.get("cache_write_regional", 0.0),
                                 data.get("cache_read_regional", 0.0), name)
            if inp_g is not None and out_g is not None:
                global_[key] = (inp_g, out_g, data.get("cache_write_global", 0.0),
                                data.get("cache_read_global", 0.0), name)
        return regional, global_
    except Exception:
        return {}, {}


def _fetch_live_products(region: str) -> dict[str, tuple]:
    """Fallback via get_products for non-Anthropic models. Regional rates only."""
    try:
        client = boto3.client("pricing", region_name="us-east-1")
        paginator = client.get_paginator("get_products")
        raw: dict[str, dict] = {}
        for page in paginator.paginate(
            ServiceCode="AmazonBedrock",
            Filters=[{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}],
        ):
            for p in page["PriceList"]:
                obj = json.loads(p)
                attr = obj["product"]["attributes"]
                model_name = attr.get("model", "")
                inference_type = attr.get("inferenceType", "")
                if not model_name or inference_type not in ("Input tokens", "Output tokens"):
                    continue
                try:
                    terms = obj["terms"]["OnDemand"]
                    dims = next(iter(terms.values()))["priceDimensions"]
                    dim = next(iter(dims.values()))
                    unit = dim.get("unit", "")
                    price = float(dim["pricePerUnit"]["USD"])
                except (KeyError, StopIteration, ValueError):
                    continue
                if unit in ("1K tokens", "1000 Tokens"):
                    price_per_1m = price * 1000
                elif unit in ("1M tokens", "1000000 Tokens"):
                    price_per_1m = price
                else:
                    continue
                key = _normalize(model_name)
                raw.setdefault(key, {"display": model_name})
                raw[key]["input" if "Input" in inference_type else "output"] = price_per_1m
        return {
            key: (data["input"], data["output"], 0.0, 0.0, data["display"])
            for key, data in raw.items()
            if "input" in data and "output" in data
        }
    except Exception:
        return {}


def _fetch_model_names(bedrock_client) -> dict[str, str]:
    try:
        resp = bedrock_client.list_foundation_models(byInferenceType="ON_DEMAND")
        return {m["modelId"]: m["modelName"] for m in resp.get("modelSummaries", [])}
    except Exception:
        return {}


def _fetch_cross_region_prefixes(bedrock_client) -> tuple[str, ...]:
    try:
        prefixes: set[str] = set()
        kwargs: dict = {"typeEquals": "SYSTEM_DEFINED", "maxResults": 1000}
        while True:
            resp = bedrock_client.list_inference_profiles(**kwargs)
            for profile in resp.get("inferenceProfileSummaries", []):
                pid = profile.get("inferenceProfileId", "")
                dot = pid.find(".")
                if 1 < dot < 10:
                    prefixes.add(pid[: dot + 1])
            if not (token := resp.get("nextToken")):
                break
            kwargs["nextToken"] = token
        return tuple(prefixes) if prefixes else _DEFAULT_CROSS_REGION_PREFIXES
    except Exception:
        return _DEFAULT_CROSS_REGION_PREFIXES


def _load_pricing_cache(region: str) -> tuple[dict, dict] | None:
    try:
        data = json.loads(open(_PRICING_CACHE_PATH).read())
        entry = data[region]
        if time.time() - entry["timestamp"] > _PRICING_CACHE_TTL:
            return None
        load = lambda d: {k: tuple(v) for k, v in d.items()}
        return load(entry["live"]), load(entry["global"])
    except Exception:
        return None


def _save_pricing_cache(region: str, live: dict, global_: dict) -> None:
    try:
        try:
            data = json.loads(open(_PRICING_CACHE_PATH).read())
        except Exception:
            data = {}
        data[region] = {"timestamp": time.time(), "live": live, "global": global_}
        with open(_PRICING_CACHE_PATH, "w") as fh:
            json.dump(data, fh)
    except Exception:
        pass


def init_pricing(region: str | None, bedrock_client=None) -> None:
    """Populate pricing caches. Call once before any lookup()/calculate_cost()."""
    global _live_cache, _global_cache, _live_sorted, _global_sorted
    global _model_names, _cross_region_prefixes

    resolved = region or "us-east-1"
    cached = _load_pricing_cache(resolved)

    if cached is not None:
        _live_cache, _global_cache = cached
        if bedrock_client is not None:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_names = pool.submit(_fetch_model_names, bedrock_client)
                f_prefixes = pool.submit(_fetch_cross_region_prefixes, bedrock_client)
                _model_names = f_names.result()
                _cross_region_prefixes = f_prefixes.result()
    else:
        with ThreadPoolExecutor(max_workers=4) as pool:
            f_csv = pool.submit(_fetch_live_csv, resolved)
            f_products = pool.submit(_fetch_live_products, resolved)
            f_names = pool.submit(_fetch_model_names, bedrock_client) if bedrock_client else None
            f_prefixes = pool.submit(_fetch_cross_region_prefixes, bedrock_client) if bedrock_client else None

            csv_regional, csv_global = f_csv.result()
            products = f_products.result()
            if f_names is not None:
                _model_names = f_names.result()
            if f_prefixes is not None:
                _cross_region_prefixes = f_prefixes.result()

        _live_cache = {**products, **csv_regional}
        _global_cache = csv_global
        _save_pricing_cache(resolved, _live_cache, _global_cache)

    _live_sorted = sorted(_live_cache.items(), key=lambda x: -len(x[0]))
    _global_sorted = sorted(_global_cache.items(), key=lambda x: -len(x[0]))


def get_cross_region_prefixes() -> tuple[str, ...]:
    return _cross_region_prefixes


def _derive_display_name(model_id: str) -> str:
    name = re.sub(r"^[^.]+\.", "", model_id)
    name = re.sub(r"[-_]\d{6,}.*$", "", name)
    name = re.sub(r"[-_]v\d+[:\d]*$", "", name)
    parts = name.split("-")
    merged: list[str] = []
    i = 0
    while i < len(parts):
        if i + 1 < len(parts) and parts[i].isdigit() and parts[i + 1].isdigit():
            merged.append(f"{parts[i]}.{parts[i + 1]}")
            i += 2
        else:
            merged.append(parts[i].capitalize())
            i += 1
    return " ".join(merged)


def get_model_display_name(model_id: str) -> str:
    return _model_names.get(model_id) or _derive_display_name(model_id)


def lookup(model_id: str, prefer_global: bool = False) -> ModelPricing:
    norm = _normalize(model_id.lower())
    primary = _global_sorted if prefer_global else _live_sorted
    fallback = _live_sorted if prefer_global else _global_sorted
    for cache in (primary, fallback):
        for key, (in_p, out_p, cw_p, cr_p, name) in cache:
            if key in norm:
                return ModelPricing(in_p, out_p, cw_p, cr_p, name, False)
    return ModelPricing(0.0, 0.0, 0.0, 0.0, get_model_display_name(model_id), True)


def calculate_cost(model_id, input_tokens, output_tokens,
                   cache_write_tokens=0, cache_read_tokens=0, prefer_global=False) -> float:
    p = lookup(model_id, prefer_global=prefer_global)
    return (
        input_tokens * p.input_per_1m
        + output_tokens * p.output_per_1m
        + cache_write_tokens * p.cache_write_per_1m
        + cache_read_tokens * p.cache_read_per_1m
    ) / 1_000_000
