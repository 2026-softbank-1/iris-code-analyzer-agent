"""Evaluate reviewed AI judgment fixtures with explicit model budget and artifacts.

Example (paid API execution is opt-in):
  python scripts/evaluate_ai_judgment.py --live --env-file ../.env \
    --provider openai --model gpt-6-luna --repetitions 3 --max-cost-usd 2 \
    --out artifacts/ai-judgment/new-prompt
"""

from __future__ import annotations

import argparse
import copy
import json
from contextlib import ExitStack
from pathlib import Path

from iris_analyzer.budget import BudgetedRunner
from iris_analyzer.contracts import AnalyzerError, Limits, digest
from iris_analyzer.judgment_evaluation import evaluate_judgment_cases
from iris_analyzer.opencode import IsolatedOpenCodeServer, ModelConfig, OpenCodeRunner
from iris_analyzer.opencode.runner import MODEL_PROPOSAL_SCHEMA


class EvaluationOpenCodeRunner(OpenCodeRunner):
    """Keep prompt comparison local to evaluation; never edit runtime prompt files."""

    def __init__(self, config: ModelConfig, *, prompt_file: Path | None = None):
        super().__init__(config)
        self._prompt_override = prompt_file.read_text(encoding="utf-8") if prompt_file else None
        self._selection: dict = {}
        if self._prompt_override is not None:
            self.response_schema = MODEL_PROPOSAL_SCHEMA
            if not self._prompt_override.strip() or len(self._prompt_override.encode()) > 100_000:
                raise ValueError("evaluation prompt must be nonempty and at most 100000 bytes")
            self.prompt_version = "evaluation-override-" + digest(self._prompt_override)[:12]

    def set_evaluation_context(self, selected_context: dict) -> None:
        # Give both prompt variants identical user-selected execution conditions.
        # No gold labels, expected claims, fixture IDs or evaluation-phase hints.
        self._selection = {
            key: copy.deepcopy(value)
            for key, value in selected_context.items()
            if key
            in {"component", "environment", "dockerfile", "target", "composeFiles", "developmentComponent"}
        }

    def _system_prompt(self) -> str:
        return self._prompt_override if self._prompt_override is not None else super()._system_prompt()

    def _request_document(self, bundle: dict) -> dict:
        document = super()._request_document(bundle)
        document["selectedExecutionContext"] = self._selection
        return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--corpus", type=Path, default=root / "evaluations/ai-judgment-cases.json")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Call the configured paid model; default only checks static fixtures",
    )
    parser.add_argument("--env-file", type=Path, default=root.parent / ".env")
    parser.add_argument("--provider", choices=["openai", "hive-ai"], default="openai")
    parser.add_argument("--model", help="Provider-specific default when omitted")
    parser.add_argument(
        "--reasoning-effort", choices=["none", "low", "medium", "high", "xhigh", "max"], default="low"
    )
    parser.add_argument("--prompt-file", type=Path, help="Archived old prompt for a same-model comparison")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--max-cost-usd", type=float, default=1.0)
    parser.add_argument("--budget-ledger", type=Path, default=root / "artifacts/model-budget-ledger.json")
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--max-total-tokens", type=int, default=1_000_000)
    parser.add_argument("--max-model-calls", type=int, default=100)
    parser.add_argument("--max-expansions", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--opencode-executable", default="opencode")
    args = parser.parse_args(argv)
    try:
        with ExitStack() as stack:
            runner = None
            if args.live:
                config = ModelConfig.from_env(
                    args.env_file,
                    provider=args.provider,
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                    max_output_tokens=args.max_output_tokens,
                    max_model_calls=args.max_model_calls,
                    max_total_tokens=args.max_total_tokens,
                    timeout_seconds=args.timeout,
                    max_remote_retries=0,
                )
                if not config.server_url:
                    server = stack.enter_context(
                        IsolatedOpenCodeServer(config, executable=args.opencode_executable)
                    )
                    config = server.config
                model = stack.enter_context(EvaluationOpenCodeRunner(config, prompt_file=args.prompt_file))
                runner = BudgetedRunner(model, args.budget_ledger, max_cost_usd=args.max_cost_usd)
            report = evaluate_judgment_cases(
                args.corpus,
                out=args.out,
                runner=runner,
                repetitions=args.repetitions,
                limits=Limits(max_expansions=args.max_expansions),
                case_ids=args.case_ids,
            )
            print(json.dumps({"mode": report["mode"], **report["summary"]}, ensure_ascii=False))
            return (
                0
                if report["summary"]["baselinePassed"]
                and not report["summary"]["failedRuns"]
                and not report["summary"]["skippedRuns"]
                else 2
            )
    except AnalyzerError as error:
        print(json.dumps({"errorCode": error.code}))
        return 2
    except (ValueError, OSError):
        # Input paths may point near credentials; do not echo file contents/errors.
        print(json.dumps({"errorCode": "JUDGMENT_EVALUATION_INPUT_INVALID"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
