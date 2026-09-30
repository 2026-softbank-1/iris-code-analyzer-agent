"""Reproducible command-line entry points for local development and workers."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Sequence

from .budget import BudgetedRunner
from .contracts import AnalyzerError, Limits
from .evaluation import evaluate_projects
from .pipeline import analyze_with_report, write_json
from .preprocess import prepare_context, save_bundle


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iris-analyzer")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("preprocess", "analyze", "verify", "evaluate"):
        p = sub.add_parser(command)
        p.add_argument("--out", required=True, type=Path)
        p.add_argument("--profile", default="deployment_v1", choices=["deployment_v1"])
        p.add_argument("--max-bundle-bytes", type=int, default=180_000)
        p.add_argument("--max-file-bytes", type=int, default=1_000_000)
        p.add_argument("--max-input-tokens", type=int)
        p.add_argument("--max-expansions", type=int, default=1)
        p.add_argument("--max-requested-files", type=int, default=5)
        if command != "evaluate":
            p.add_argument(
                "--repo", type=Path, required=command != "verify", default=Path("fixtures/separated-web-api")
            )
        if command != "preprocess":
            p.add_argument("--offline", action="store_true", help="Run deterministic static analysis only")
            p.add_argument("--env-file", type=Path, default=Path(".env"))
            p.add_argument("--provider")
            p.add_argument("--model")
            p.add_argument("--opencode-url")
            p.add_argument("--opencode-executable", default="opencode")
            p.add_argument("--output-mode", choices=["structured", "json_text"])
            p.add_argument("--reasoning-effort", choices=["low", "high", "max"])
            p.add_argument("--timeout", type=float)
            p.add_argument("--max-output-tokens", type=int)
            p.add_argument("--max-model-calls", type=int)
            p.add_argument("--max-total-tokens", type=int)
            p.add_argument("--max-remote-retries", type=int)
            p.add_argument("--budget-ledger", type=Path, default=Path("artifacts/model-budget-ledger.json"))
            p.add_argument("--max-cost-usd", type=float, default=1.0)
            p.add_argument(
                "--pricing-json", type=Path, help="Reviewed input/output/cacheRead USD-per-million rates"
            )
        if command in {"verify", "evaluate"}:
            p.add_argument("--repetitions", type=int, default=2)
        if command == "evaluate":
            p.add_argument("--tested-code", type=Path, default=Path("../tested_code"))
            p.add_argument("--truth", type=Path, default=Path("evaluations/ground-truth.json"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        limits = Limits(
            max_bundle_bytes=args.max_bundle_bytes,
            max_file_bytes=args.max_file_bytes,
            max_input_tokens=args.max_input_tokens,
            max_expansions=args.max_expansions,
            max_requested_files=args.max_requested_files,
        )
        if args.command == "preprocess":
            repo = args.repo.resolve()
            out = args.out.resolve()
            exclusions = [out.relative_to(repo).as_posix()] if out.is_relative_to(repo) else []
            bundle = prepare_context(repo, profile=args.profile, limits=limits, excluded_paths=exclusions)
            save_bundle(bundle, out)
            print(
                json.dumps(
                    {
                        "snapshotId": bundle["source"]["snapshotId"],
                        "contextHash": bundle["contextHash"],
                        "selectedFiles": len(bundle["selectedFiles"]),
                    }
                )
            )
            return 0
        with ExitStack() as stack:
            runner = None
            if not args.offline:
                from .opencode import IsolatedOpenCodeServer, ModelConfig, OpenCodeRunner

                config = ModelConfig.from_env(
                    args.env_file if args.env_file.is_file() else None,
                    provider=args.provider,
                    model=args.model,
                    server_url=args.opencode_url,
                    output_mode=args.output_mode,
                    reasoning_effort=args.reasoning_effort,
                    timeout_seconds=args.timeout,
                    max_output_tokens=args.max_output_tokens,
                    max_model_calls=args.max_model_calls,
                    max_total_tokens=args.max_total_tokens,
                    max_remote_retries=args.max_remote_retries,
                )
                if not config.server_url:
                    server = stack.enter_context(
                        IsolatedOpenCodeServer(config, executable=args.opencode_executable)
                    )
                    config = server.config
                base_runner = stack.enter_context(OpenCodeRunner(config))
                pricing = json.loads(args.pricing_json.read_text()) if args.pricing_json else None
                runner = BudgetedRunner(
                    base_runner, args.budget_ledger, max_cost_usd=args.max_cost_usd, pricing=pricing
                )
            if args.command == "analyze":
                run = analyze_with_report(
                    args.repo, runner=runner, profile=args.profile, limits=limits, out=args.out
                )
                print(
                    json.dumps(
                        {
                            "status": run.result["status"],
                            "contextHash": run.bundle["contextHash"],
                            "mode": run.report["mode"],
                        }
                    )
                )
                return 0
            if args.command == "evaluate":
                report = evaluate_projects(
                    args.tested_code,
                    args.truth,
                    out=args.out,
                    runner=runner,
                    repetitions=args.repetitions,
                    limits=limits,
                )
                print(
                    json.dumps(
                        {
                            "passed": report["passed"],
                            "mode": report["mode"],
                            "successCount": report["successCount"],
                            "failureCount": report["failureCount"],
                        }
                    )
                )
                return 0 if report["passed"] else 1
            verification: dict = {
                "schemaVersion": "1",
                "mode": "live_model" if runner else "static",
                "modelCalled": runner is not None,
                "cases": [],
                "usage": None,
            }
            for index in range(args.repetitions):
                try:
                    run = analyze_with_report(
                        args.repo,
                        runner=runner,
                        profile=args.profile,
                        limits=limits,
                        out=args.out / f"run-{index + 1:02d}",
                    )
                    verification["cases"].append(
                        {
                            "success": True,
                            "resultStatus": run.result["status"],
                            "contextHash": run.bundle["contextHash"],
                            "calls": run.report["calls"],
                        }
                    )
                except AnalyzerError as error:
                    failure = {"success": False, "errorCode": error.code}
                    failed_report = args.out / f"run-{index + 1:02d}" / "run-report.json"
                    if failed_report.is_file():
                        failure["calls"] = json.loads(failed_report.read_text())["calls"]
                    verification["cases"].append(failure)
            verification["modelCalled"] = any(
                call.get("reservedTokensUpperBound") is not None
                for case in verification["cases"]
                for call in case.get("calls", [])
            )
            verification.update(
                successCount=sum(case["success"] for case in verification["cases"]),
                failureCount=sum(not case["success"] for case in verification["cases"]),
            )
            write_json(args.out / "model-verification-report.json", verification)
            print(
                json.dumps(
                    {
                        "mode": verification["mode"],
                        "successCount": verification["successCount"],
                        "failureCount": verification["failureCount"],
                    }
                )
            )
            return 1 if verification["failureCount"] else 0
    except (AnalyzerError, ValueError, OSError) as error:
        # Avoid echoing provider bodies, credentials or absolute repository paths.
        code = error.code if isinstance(error, AnalyzerError) else "INPUT_INVALID"
        try:
            if not (args.out / "run-report.json").exists():
                write_json(
                    args.out / "run-report.json",
                    {
                        "schemaVersion": "1",
                        "status": "failed",
                        "modelCalled": False,
                        "calls": [],
                        "errors": [{"code": code}],
                        "events": [{"stage": "failed"}],
                    },
                )
            if args.command == "verify" and not (args.out / "model-verification-report.json").exists():
                write_json(
                    args.out / "model-verification-report.json",
                    {
                        "schemaVersion": "1",
                        "mode": "static" if args.offline else "live_model",
                        "modelCalled": False,
                        "cases": [{"success": False, "errorCode": code}],
                        "successCount": 0,
                        "failureCount": 1,
                        "usage": None,
                    },
                )
        except OSError:
            pass  # The original error remains useful when the output path is unwritable.
        print(json.dumps({"status": "failed", "errorCode": code}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"status": "failed", "errorCode": "ANALYSIS_CANCELLED"}), file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
