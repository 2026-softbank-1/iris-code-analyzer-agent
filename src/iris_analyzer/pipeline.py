"""Worker-facing orchestration with immutable revisions and explicit state events."""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .contracts import (
    AnalyzerError,
    Limits,
    canonical_bytes,
    validate_bundle,
    validate_reply,
    validate_result,
)
from .preprocess import expand_context, prepare_context, release_snapshot, save_bundle
from .result import static_analysis, validate_analysis


class ModelRunner(Protocol):
    calls: list[dict[str, Any]]

    def invoke_model(self, bundle: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class PipelineRun:
    result: dict[str, Any]
    bundle: dict[str, Any]
    report: dict[str, Any]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(value) + b"\n")


def analyze_with_report(
    repo: str | Path,
    *,
    runner: ModelRunner | None = None,
    profile: str = "deployment_v1",
    limits: Limits | None = None,
    out: str | Path | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> PipelineRun:
    """Analyze a fixed source; callbacks let a worker persist PostgreSQL Job state.

    No runner selects the explicitly static mode. A model failure never silently
    becomes an apparently successful model analysis.
    """
    limits = limits or Limits()
    destination = Path(out).resolve() if out is not None else None
    report: dict[str, Any] = {
        "schemaVersion": "1",
        "mode": "opencode" if runner else "static",
        "events": [],
        "calls": [],
        "revisions": [],
        "errors": [],
    }
    started = time.monotonic()
    call_start = len(getattr(runner, "calls", [])) if runner else 0
    bundle: dict[str, Any] | None = None

    def event(stage: str) -> None:
        data: dict[str, Any] = {"stage": stage, "elapsedSeconds": round(time.monotonic() - started, 4)}
        if bundle:
            data.update(
                snapshotId=bundle["source"]["snapshotId"],
                contextHash=bundle["contextHash"],
                revision=bundle["revision"],
            )
        report["events"].append(data)
        if on_event:
            on_event(copy.deepcopy(data))

    def persist_revision() -> None:
        assert bundle is not None
        report["revisions"].append({"revision": bundle["revision"], "contextHash": bundle["contextHash"]})
        if destination:
            save_bundle(bundle, destination / f"revision-{bundle['revision']:02d}")

    try:
        event("queued")
        event("preprocessing")
        exclusions = []
        if destination and destination.is_relative_to(Path(repo).resolve()):
            exclusions = [destination.relative_to(Path(repo).resolve()).as_posix()]
        bundle = prepare_context(repo, profile=profile, limits=limits, excluded_paths=exclusions)
        validate_bundle(bundle)
        persist_revision()
        pending: list[dict[str, Any]] = []
        if runner is None:
            event("validating")
            result = static_analysis(bundle)
        else:
            for call_index in range(limits.max_expansions + 1):
                event("analyzing")
                reply = validate_reply(runner.invoke_model(copy.deepcopy(bundle)))
                report["calls"] = copy.deepcopy(getattr(runner, "calls", [])[call_start:])
                if destination:
                    from .opencode import model_input

                    actual_input = model_input(bundle)
                    if getattr(runner, "last_request", None):
                        actual_input = json.loads(runner.last_request["parts"][0]["text"])
                        write_json(
                            destination / f"revision-{bundle['revision']:02d}" / "model-request.json",
                            runner.last_request,
                        )
                    write_json(
                        destination / f"revision-{bundle['revision']:02d}" / "model-input.json", actual_input
                    )
                    write_json(
                        destination / f"revision-{bundle['revision']:02d}" / "model-response.json", reply
                    )
                if reply["kind"] == "analysis":
                    event("validating")
                    result = validate_analysis(reply, bundle)
                    break
                paths = reply["requestedPaths"]
                if call_index == limits.max_expansions:
                    pending.append(
                        {
                            "key": "additional_files",
                            "reason": "Input expansion limit reached: " + reply["reason"],
                            "kind": "code_review",
                        }
                    )
                    result = static_analysis(bundle)
                    break
                if len(paths) > limits.max_requested_files:
                    pending.append(
                        {
                            "key": "additional_files",
                            "reason": "Requested file count exceeds policy",
                            "kind": "code_review",
                        }
                    )
                    result = static_analysis(bundle)
                    break
                event("expanding")
                bundle = expand_context(bundle, paths, limits=limits)
                validate_bundle(bundle)
                persist_revision()
            else:  # Defensive: a positive bounded call loop always breaks above.
                raise AnalyzerError("PIPELINE_STATE_INVALID", "No analysis response")
        if pending:
            result["questions"].extend(pending)
            result["status"] = "needs_input"
            result["coverage"]["completeForProfile"] = False
            result["coverage"]["limitations"].extend(item["reason"] for item in pending)
        validate_result(result)
        event("succeeded" if result["status"] == "complete" else result["status"])
        report.update(
            status=result["status"],
            snapshotId=bundle["source"]["snapshotId"],
            contextHash=bundle["contextHash"],
            durationSeconds=round(time.monotonic() - started, 4),
        )
        if destination:
            save_bundle(bundle, destination)
            if runner:
                from .opencode import model_input

                actual_input = model_input(bundle)
                if getattr(runner, "last_request", None):
                    actual_input = json.loads(runner.last_request["parts"][0]["text"])
                    write_json(destination / "model-request.json", runner.last_request)
                write_json(destination / "model-input.json", actual_input)
                write_json(destination / "model-response.json", reply)
            write_json(destination / "analysis-result.json", result)
            write_json(destination / "run-report.json", report)
        return PipelineRun(result, bundle, report)
    except (AnalyzerError, KeyboardInterrupt) as error:
        code = error.code if isinstance(error, AnalyzerError) else "ANALYSIS_CANCELLED"
        report["errors"].append({"code": code})
        report["calls"] = copy.deepcopy(getattr(runner, "calls", [])[call_start:]) if runner else []
        report.update(status="failed", durationSeconds=round(time.monotonic() - started, 4))
        event("failed")
        if destination:
            if runner and bundle and getattr(runner, "last_request", None):
                write_json(
                    destination / f"revision-{bundle['revision']:02d}" / "model-request.json",
                    runner.last_request,
                )
                write_json(
                    destination / f"revision-{bundle['revision']:02d}" / "model-input.json",
                    json.loads(runner.last_request["parts"][0]["text"]),
                )
                if getattr(runner, "last_response", None):
                    write_json(
                        destination / f"revision-{bundle['revision']:02d}" / "model-response-raw.json",
                        runner.last_response,
                    )
            write_json(destination / "run-report.json", report)
        raise
    finally:
        if bundle:
            release_snapshot(bundle["source"]["snapshotId"])


def analyze_snapshot(repo: str | Path, **options: Any) -> dict[str, Any]:
    """Public library entry point returning only the downstream AnalysisResult."""
    return analyze_with_report(repo, **options).result
