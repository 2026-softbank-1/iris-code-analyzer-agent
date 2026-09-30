import json
from types import SimpleNamespace

import pytest

from iris_analyzer.budget import BudgetedRunner, estimate_usage_cost
from iris_analyzer.contracts import AnalyzerError


class FakeRunner:
    def __init__(self, usage=None, *, retries=0, fail=False):
        self.config = SimpleNamespace(
            provider="hive-ai",
            model="zai-org/glm-5.3-flash",
            max_output_tokens=8192,
            max_remote_retries=retries,
            max_inference_steps=2,
            output_mode="json_text",
        )
        self.calls = []
        self.usage = usage
        self.fail = fail

    def invoke_model(self, bundle):
        self.calls.append({"usage": self.usage, "remoteRetryAttempts": self.config.max_remote_retries})
        if self.fail:
            raise AnalyzerError("MODEL_TIMEOUT", "Timeout")
        return {"kind": "needs_files", "requestedPaths": [], "reason": "fixture"}


def test_price_estimate_includes_reasoning_and_cache():
    usage = {"input": 100, "output": 200, "reasoning": 20, "cache": {"read": 300, "write": 10}}
    assert estimate_usage_cost(usage) == pytest.approx((110 * 0.05 + 220 * 0.17 + 300 * 0.01) / 1e6)
    assert estimate_usage_cost(None) is None


@pytest.mark.parametrize("ceiling", [float("nan"), float("inf"), -1, 0, True])
def test_invalid_monetary_limit_rejected(tmp_path, ceiling):
    with pytest.raises(ValueError):
        BudgetedRunner(FakeRunner(), tmp_path / "ledger.json", max_cost_usd=ceiling)


@pytest.mark.parametrize("rate", [float("nan"), float("inf"), -1, True, "cheap"])
def test_invalid_pricing_rejected(tmp_path, rate):
    with pytest.raises(ValueError):
        BudgetedRunner(
            FakeRunner(), tmp_path / "ledger.json", pricing={"input": rate, "output": 0.17, "cacheRead": 0.01}
        )


def test_budget_reservation_rejects_before_inference(tmp_path):
    runner = FakeRunner()
    budget = BudgetedRunner(runner, tmp_path / "ledger.json", max_cost_usd=0.00000001)
    with pytest.raises(AnalyzerError) as exc:
        budget.invoke_model({"contextHash": "f" * 64})
    assert exc.value.code == "MODEL_BUDGET_EXCEEDED"
    assert runner.calls == []


def test_two_runner_instances_share_durable_spending(tmp_path):
    ledger = tmp_path / "ledger.json"
    first = BudgetedRunner(FakeRunner({"input": 1_000_000, "output": 0}), ledger, max_cost_usd=0.06)
    first.invoke_model({"contextHash": "a" * 64})
    second_runner = FakeRunner()
    second = BudgetedRunner(second_runner, ledger, max_cost_usd=0.05000001)
    with pytest.raises(AnalyzerError):
        second.invoke_model({"contextHash": "b" * 64})
    assert second_runner.calls == []
    entries = json.loads(ledger.read_text())["entries"]
    assert entries[0]["actualCostUsd"] is None
    assert entries[0]["estimatedOrReservedUsd"] == 0.05


def test_lost_usage_retains_reservation_after_failure(tmp_path):
    ledger = tmp_path / "ledger.json"
    budget = BudgetedRunner(FakeRunner(fail=True), ledger)
    with pytest.raises(AnalyzerError):
        budget.invoke_model({"contextHash": "a" * 64})
    row = json.loads(ledger.read_text())["entries"][0]
    assert row["state"] == "unknown_usage"
    assert row["estimatedOrReservedUsd"] == row["reservedUsd"] > 0
    assert row["usage"] is None


def test_remote_retries_reserve_and_retain_unreported_cost(tmp_path):
    ledger = tmp_path / "ledger.json"
    runner = FakeRunner({"input": 100, "output": 100}, retries=2)
    BudgetedRunner(runner, ledger).invoke_model({"contextHash": "a" * 64})
    row = json.loads(ledger.read_text())["entries"][0]
    assert row["estimatedOrReservedUsd"] == pytest.approx(
        estimate_usage_cost(runner.usage) + row["reservedUsd"] * 2 / 3
    )


def test_corrupt_ledger_prevents_model_call(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text('{"schemaVersion":"1","entries":[{"estimatedOrReservedUsd":-1}]}')
    runner = FakeRunner()
    with pytest.raises(AnalyzerError) as exc:
        BudgetedRunner(runner, ledger).invoke_model({"contextHash": "a" * 64})
    assert exc.value.code == "MODEL_BUDGET_LEDGER_INVALID"
    assert not runner.calls


def test_unknown_model_price_blocks_monetary_guard(tmp_path):
    runner = FakeRunner()
    runner.config.model = "unpriced-model"
    with pytest.raises(AnalyzerError) as exc:
        BudgetedRunner(runner, tmp_path / "ledger.json").invoke_model({"contextHash": "a" * 64})
    assert exc.value.code == "MODEL_PRICING_UNAVAILABLE"


@pytest.mark.parametrize("mode", ["json_text", "structured"])
def test_rejected_before_prompt_refunds_known_unsubmitted_reservation(tmp_path, mode):
    class RejectedRunner(FakeRunner):
        def invoke_model(self, bundle):
            self.calls.append({"usage": None, "reservedTokensUpperBound": None})
            raise AnalyzerError("MODEL_BUDGET_EXCEEDED", "Not submitted")

    ledger = tmp_path / "ledger.json"
    runner = RejectedRunner()
    runner.config.output_mode = mode
    with pytest.raises(AnalyzerError):
        BudgetedRunner(runner, ledger).invoke_model({"contextHash": "a" * 64})
    row = json.loads(ledger.read_text())["entries"][0]
    assert row["state"] == "not_submitted"
    assert row["estimatedOrReservedUsd"] == 0
