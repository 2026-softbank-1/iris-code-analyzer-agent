"""A durable conservative spending guard, separate from provider billing reports."""

from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contracts import MODEL_REPLY_SCHEMA, AnalyzerError, canonical_bytes

HIVE_GLM_PRICING = {
    "input": 0.05,
    "output": 0.17,
    "cacheRead": 0.01,
    "source": "https://thehive.ai/models/zai-org/glm-5.3-flash",
    "verifiedDate": "2026-10-01",
}


def estimate_usage_cost(usage: dict | None, pricing: dict = HIVE_GLM_PRICING) -> float | None:
    if not isinstance(usage, dict) or not all(
        isinstance(usage.get(k), (int, float)) for k in ("input", "output")
    ):
        return None
    cache = usage.get("cache") or {}
    # Unknown cache-write pricing is conservatively priced as fresh input.
    return (
        (usage["input"] + cache.get("write", 0)) * pricing["input"]
        + (usage["output"] + usage.get("reasoning", 0)) * pricing["output"]
        + cache.get("read", 0) * pricing["cacheRead"]
    ) / 1_000_000


class BudgetedRunner:
    """Reserve a worst-case cost before inference, across processes sharing a ledger.

    Costs remain estimates. No request goes out when unknown pricing prevents
    computing a configured monetary bound. The provider's actual charge is null
    unless it supplies that value; the ledger never claims to be an invoice.
    """

    def __init__(
        self, runner: Any, ledger: str | Path, *, max_cost_usd: float = 1.0, pricing: dict | None = None
    ):
        self.runner = runner
        self.ledger = Path(ledger)
        self.max_cost_usd = max_cost_usd
        if isinstance(max_cost_usd, bool) or not math.isfinite(max_cost_usd) or max_cost_usd <= 0:
            raise ValueError("max_cost_usd must be positive")
        identity = (runner.config.provider, runner.config.model)
        self.pricing = pricing or (
            HIVE_GLM_PRICING if identity == ("hive-ai", "zai-org/glm-5.3-flash") else None
        )
        if self.pricing is not None:
            if not isinstance(self.pricing, dict) or any(
                not isinstance(self.pricing.get(key), (int, float))
                or isinstance(self.pricing[key], bool)
                or not math.isfinite(self.pricing[key])
                or self.pricing[key] < 0
                for key in ("input", "output", "cacheRead")
            ):
                raise ValueError("Pricing requires finite nonnegative numeric input/output/cacheRead rates")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.runner, name)

    def _transaction(self, mutate):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            text = stream.read()
            try:
                state = json.loads(text) if text else {"schemaVersion": "1", "entries": []}
                if state.get("schemaVersion") != "1" or not isinstance(state.get("entries"), list):
                    raise ValueError("ledger schema")
                for row in state["entries"]:
                    amount = row["estimatedOrReservedUsd"]
                    if not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
                        raise ValueError("ledger amount")
            except (ValueError, AttributeError, TypeError, KeyError) as exc:
                raise AnalyzerError(
                    "MODEL_BUDGET_LEDGER_INVALID", "Budget ledger is invalid; refusing inference"
                ) from exc
            result = mutate(state)
            stream.seek(0)
            stream.truncate()
            stream.write(canonical_bytes(state).decode("utf-8") + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            fcntl.flock(stream, fcntl.LOCK_UN)
            return result

    def invoke_model(self, bundle: dict) -> dict:
        if self.pricing is None:
            raise AnalyzerError(
                "MODEL_PRICING_UNAVAILABLE", "Verified pricing is needed for the monetary guard"
            )
        # UTF-8 bytes give a deliberately conservative token ceiling. Account
        # for the response schema, template, harness prompt and protocol space.
        input_ceiling = len(canonical_bytes(bundle)) + len(canonical_bytes(MODEL_REPLY_SCHEMA)) + 16_384
        output_ceiling = self.runner.config.max_output_tokens
        one_attempt_reserve = (
            input_ceiling * self.pricing["input"] + output_ceiling * self.pricing["output"]
        ) / 1_000_000
        steps = getattr(self.runner.config, "max_inference_steps", 2)
        growth_reserve = output_ceiling * self.pricing["input"] / 1_000_000 * (steps * (steps - 1) // 2)
        per_execution_reserve = one_attempt_reserve * steps + growth_reserve
        reserve = per_execution_reserve * (1 + self.runner.config.max_remote_retries)
        identifier = uuid4().hex

        def add(state):
            committed = sum(row.get("estimatedOrReservedUsd", 0) for row in state["entries"])
            if committed + reserve > self.max_cost_usd:
                raise AnalyzerError(
                    "MODEL_BUDGET_EXCEEDED", "Conservative reserved spending exceeds the ledger ceiling"
                )
            state["entries"].append(
                {
                    "id": identifier,
                    "state": "reserved",
                    "contextHash": bundle["contextHash"],
                    "provider": self.runner.config.provider,
                    "model": self.runner.config.model,
                    "reservedUsd": reserve,
                    "estimatedOrReservedUsd": reserve,
                    "actualCostUsd": None,
                    "pricing": self.pricing,
                }
            )

        self._transaction(add)
        initial = len(self.runner.calls)
        try:
            return self.runner.invoke_model(bundle)
        finally:
            calls = self.runner.calls[initial:]
            usage = calls[-1].get("usage") if calls else None
            estimated = estimate_usage_cost(usage, self.pricing)
            not_submitted = (
                bool(calls)
                and "reservedTokensUpperBound" in calls[-1]
                and (calls[-1]["reservedTokensUpperBound"] is None)
            )
            if not_submitted:
                estimated = 0.0
            retries = calls[-1].get("remoteRetryAttempts", 0) if calls else 0
            if estimated is not None and not not_submitted:
                estimated += retries * per_execution_reserve
                if getattr(self.runner.config, "output_mode", "json_text") == "structured":
                    # The latest-message endpoint may not expose an earlier
                    # assistant's usage. Do not refund that unknown reservation.
                    estimated += (steps - 1) * one_attempt_reserve

            def settle(state):
                row = next(row for row in state["entries"] if row["id"] == identifier)
                row.update(
                    state="not_submitted"
                    if not_submitted
                    else "settled"
                    if estimated is not None
                    else "unknown_usage",
                    estimatedOrReservedUsd=estimated if estimated is not None else reserve,
                    usage=usage,
                )

            self._transaction(settle)
