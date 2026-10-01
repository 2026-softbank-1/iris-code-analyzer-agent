"""Worker-friendly planning with the same isolated and budgeted AI boundary."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from ..budget import BudgetedRunner
from ..contracts import AnalyzerError
from ..opencode import IsolatedOpenCodeServer, ModelConfig
from ..pipeline import write_json
from .advisor import PlanningOpenCodeRunner, advisor_input
from .planner import prepare_planning_request


@contextmanager
def live_advisor(
    config: ModelConfig, *, ledger: Path, executable="opencode", max_cost_usd=1.0, cancelled=None
):
    if "/" in str(executable):
        executable = str(Path(executable).resolve())

    def model(effective):
        return PlanningOpenCodeRunner(effective, cancel_event=cancelled)

    if config.server_url:
        with model(config) as runner:
            yield BudgetedRunner(runner, ledger, max_cost_usd=max_cost_usd)
    else:
        with IsolatedOpenCodeServer(config, executable=executable) as server:
            with model(server.config) as runner:
                yield BudgetedRunner(runner, ledger, max_cost_usd=max_cost_usd)


def plan_with_report(
    analysis,
    readiness,
    request=None,
    *,
    config=None,
    ledger=Path("artifacts/model-budget-ledger.json"),
    executable="opencode",
    max_cost_usd=1.0,
    out=None,
    cancelled=None,
):
    from .dossier import build_deployment_dossier_from_readiness

    request = prepare_planning_request(request)
    advice, calls = None, []
    target = request["target"]
    supported_request = target["stack"] in {None, "aws_eks", "existing_kubernetes"} and (
        target.get("cloud") in {None, "aws"} or target["stack"] == "existing_kubernetes"
    )
    if config and supported_request:
        with live_advisor(
            config, ledger=Path(ledger), executable=executable, max_cost_usd=max_cost_usd, cancelled=cancelled
        ) as runner:
            advice = runner.invoke_model(advisor_input(analysis, request, readiness=readiness))
            calls = runner.calls
    if cancelled and cancelled.is_set():
        raise AnalyzerError("PLANNING_CANCELLED", "Planning was cancelled")
    dossier = build_deployment_dossier_from_readiness(analysis, readiness, request, policy_proposal=advice)
    report = {
        "schemaVersion": "iris.planning-run.v1",
        "mode": "ai" if advice else "policy",
        "adviceSkippedReason": None if supported_request else "REQUESTED_ADAPTER_UNAVAILABLE",
        "calls": calls,
        "sourceSnapshotId": analysis["sourceSnapshotId"],
        "planDigest": dossier["deploymentPlan"]["planDigest"],
    }
    if out:
        root = Path(out) / dossier["deploymentPlan"]["planDigest"]
        from .render import write_execution_bundle

        target = root / "execution"
        if root.is_symlink() or target.is_symlink():
            raise AnalyzerError(
                "EXECUTION_ARTIFACT_CHANGED", "Execution artifact directory cannot be a symbolic link"
            )
        if not target.exists():
            write_execution_bundle(dossier["deploymentPlan"], target, analysis=analysis)
        else:
            with TemporaryDirectory(prefix="iris-execution-verify-") as temporary:
                expected = Path(temporary) / "execution"
                write_execution_bundle(dossier["deploymentPlan"], expected, analysis=analysis)

                def fingerprints(directory):
                    result = {}
                    for path in directory.rglob("*"):
                        if path.is_symlink():
                            raise AnalyzerError(
                                "EXECUTION_ARTIFACT_CHANGED",
                                "Execution artifacts cannot contain symbolic links",
                            )
                        if path.is_file():
                            result[path.relative_to(directory).as_posix()] = hashlib.sha256(
                                path.read_bytes()
                            ).hexdigest()
                    return result

                if fingerprints(target) != fingerprints(expected):
                    raise AnalyzerError(
                        "EXECUTION_ARTIFACT_CHANGED",
                        "Existing execution artifacts differ from the validated plan/templates",
                    )
        write_json(root / "deployment-dossier.json", dossier)
        write_json(root / "planning-run-report.json", report)
        if advice:
            write_json(root / "planning-advice.json", advice)
    return dossier, report


async def plan_async(analysis, readiness, request=None, **options):
    """Cancellation waits for model abort/cleanup instead of abandoning a thread."""
    cancelled = threading.Event()
    task = asyncio.create_task(
        asyncio.to_thread(plan_with_report, analysis, readiness, request, cancelled=cancelled, **options)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise
