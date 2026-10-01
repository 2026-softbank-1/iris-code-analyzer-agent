"""Reviewed price snapshots shared by reservations and usage reports."""

import copy

HIVE_GLM_PRICING = {
    "input": 0.05,
    "output": 0.17,
    "cacheRead": 0.01,
    "source": "https://thehive.ai/models/zai-org/glm-5.3-flash",
    "verifiedDate": "2026-10-01",
}
OPENAI_MINI_PRICING = {
    "input": 0.40,
    "output": 1.60,
    "cacheRead": 0.10,
    "source": "https://developers.openai.com/api/docs/pricing",
    "verifiedDate": "2026-10-01",
}
OPENAI_LUNA_PRICING = {
    "input": 0.10,
    "output": 0.50,
    "cacheRead": 0.01,
    "cacheWrite": 0.125,
    "reservationInput": 0.25,
    "reservationOutput": 0.75,
    "longContextThreshold": 272000,
    "source": "https://developers.openai.com/api/docs/pricing",
    "verifiedDate": "2026-10-01",
    "estimatePolicy": "Conservative fresh input includes possible cache-write premium; long-context multipliers apply above 272K prompt tokens.",
}


def pricing_for(provider, model):
    if provider == "openai" and model == "gpt-6-luna":
        return copy.deepcopy(OPENAI_LUNA_PRICING)
    if (provider, model) == ("hive-ai", "zai-org/glm-5.3-flash"):
        return copy.deepcopy(HIVE_GLM_PRICING)
    if provider == "openai" and model in {"gpt-4.1-mini", "gpt-4.1-mini-2025-04-14"}:
        return copy.deepcopy(OPENAI_MINI_PRICING)
    return None


def usage_cost(usage, pricing):
    if not isinstance(usage, dict) or not all(
        type(usage.get(k)) in {int, float} for k in ("input", "output")
    ):
        return None
    cache = usage.get("cache") or {}
    prompt = usage["input"] + cache.get("read", 0) + cache.get("write", 0)
    output = usage["output"] + usage.get("reasoning", 0)
    if prompt + output == 0:
        return None
    multiplier = 2 if pricing.get("longContextThreshold") and prompt > pricing["longContextThreshold"] else 1
    out_multiplier = 1.5 if multiplier == 2 else 1
    fresh_rate = max(pricing["input"], pricing.get("cacheWrite", pricing["input"]))
    return (
        (
            usage["input"] * fresh_rate
            + cache.get("write", 0) * pricing.get("cacheWrite", pricing["input"])
            + cache.get("read", 0) * pricing["cacheRead"]
        )
        * multiplier
        + output * pricing["output"] * out_multiplier
    ) / 1_000_000
