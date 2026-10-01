"""Produce inspectable planning artifacts; this command never applies them."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..contracts import AnalyzerError
from ..opencode import ModelConfig
from ..pipeline import analyze_with_report, write_json
from .client import plan_with_report
from .dossier import prepare_readiness


def main(argv=None):
    parser = argparse.ArgumentParser(prog="iris-deployment")
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--request", type=Path, help="Versioned planning request; omit for AI-proposed initial assumptions"
    )
    parser.add_argument(
        "--offline", action="store_true", help="Use explicit policy profiles without a model call"
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--opencode-executable", default="opencode")
    parser.add_argument("--budget-ledger", type=Path, default=Path("artifacts/model-budget-ledger.json"))
    parser.add_argument("--max-cost-usd", type=float, default=1.0)
    args = parser.parse_args(argv)
    try:
        repo, out = args.repo.resolve(), args.out.resolve()
        if out.is_relative_to(repo):
            raise AnalyzerError(
                "PLANNING_OUTPUT_INVALID", "Planning output must be outside the analyzed source tree"
            )
        request = json.loads(args.request.read_text()) if args.request else None
        run = analyze_with_report(repo, out=out / "analysis")
        readiness = prepare_readiness(repo, out=out, analysis=run.result)
        write_json(out / "source-readiness.json", readiness)
        config = (
            None if args.offline else ModelConfig.from_env(args.env_file if args.env_file.is_file() else None)
        )
        dossier, report = plan_with_report(
            run.result,
            readiness,
            request,
            config=config,
            ledger=args.budget_ledger,
            executable=args.opencode_executable,
            max_cost_usd=args.max_cost_usd,
            out=out / "plans",
        )
        write_json(out / "deployment-dossier.json", dossier)
        print(
            json.dumps(
                {
                    "status": dossier["deploymentPlan"]["status"],
                    "execution": dossier["execution"]["status"],
                    "plannerMode": report["mode"],
                    "planDigest": dossier["deploymentPlan"]["planDigest"],
                }
            )
        )
        return 0
    except (AnalyzerError, ValueError, OSError) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": error.code if isinstance(error, AnalyzerError) else "PLANNING_INPUT_INVALID",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
