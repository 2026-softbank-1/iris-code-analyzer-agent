"""Independent, source-reviewed acceptance criteria for the two supplied projects."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .contracts import AnalyzerError, Limits, canonical_bytes
from .pipeline import ModelRunner, analyze_with_report, write_json
from .preprocess import prepare_context, release_snapshot
from .result import static_analysis


def _value(field: dict) -> Any:
    return field.get("value")


def score_result(result: dict, bundle: dict, truth: dict) -> dict:
    """Score values/relationships rather than natural-language string matching."""
    checks: list[dict] = []

    def check(key: str, expected: Any, observed: Any) -> None:
        checks.append(
            {"key": key, "expected": expected, "observed": observed, "passed": expected == observed}
        )

    services = result["services"]
    check("application_service_count", truth["serviceCount"], len(services))
    check("service_roles", sorted(truth["roles"]), sorted(_value(s["role"]) for s in services))
    check("component_roots", sorted(truth["componentRoots"]), sorted(bundle["componentRoots"]))
    for item in truth.get("serviceFields", []):
        observed = [service[item["field"]] for service in services if _value(service["role"]) == item["role"]]
        check(
            "service:" + item["role"] + ":" + item["field"],
            True,
            any(
                field["value"] == item["value"]
                and field["scope"] == item["scope"]
                and field["status"] == "detected"
                for field in observed
            ),
        )
    for item in truth["facts"]:
        observed = [
            fact["value"]
            for fact in bundle["facts"]
            if fact["key"] == item["key"]
            and fact["scope"] == item["scope"]
            and ("component" not in item or fact.get("component") == item["component"])
        ]
        check(
            item["key"] + ":" + item["scope"] + ":" + str(item.get("component", "*")),
            True,
            item["value"] in observed,
        )
    route_values = [_value(route) for route in result["apiRoutes"]]
    actual_routes = {
        (r["method"].upper(), r["path"].rstrip("/") or "/")
        for r in route_values
        if isinstance(r, dict) and "method" in r and "path" in r
    }
    expected_routes = {(r[0], r[1].rstrip("/") or "/") for r in truth["routes"]}
    check("route_set", sorted(expected_routes), sorted(actual_routes))
    environment = {_value(field) for field in result["environmentKeys"] if isinstance(_value(field), str)}
    check("required_environment_keys", True, set(truth["requiredEnvironmentKeys"]).issubset(environment))
    for dep in truth["dependencies"]:
        check(
            "dependency:" + dep["name"],
            True,
            any(
                isinstance(_value(field), dict) and all(_value(field).get(k) == v for k, v in dep.items())
                for field in result["dependencies"]
            ),
        )
    connections = [_value(field) for field in result["connections"]]
    for base in truth["frontendBaseUrls"]:
        check(
            "frontend_base:" + base,
            True,
            any(isinstance(value, dict) and value.get("baseUrl") == base for value in connections),
        )
    # Evidence text and source digests are independently checked here, even in static mode.
    manifest = {row["path"]: row for row in bundle["manifest"]}
    check(
        "evidence_integrity",
        True,
        all(
            ev["sourceDigest"] == manifest[ev["path"]]["digest"]
            and ev["contentDigest"] == hashlib.sha256(ev["text"].encode("utf-8")).hexdigest()
            for ev in bundle["evidence"]
        ),
    )
    check(
        "secret_file_exclusion",
        True,
        all(
            not (row["path"].split("/")[-1] == ".env" or "/secrets/" in "/" + row["path"])
            for row in bundle["selectedFiles"]
        ),
    )
    true_positive = len(actual_routes & expected_routes)
    precision = true_positive / len(actual_routes) if actual_routes else float(not expected_routes)
    recall = true_positive / len(expected_routes) if expected_routes else float(not actual_routes)
    return {
        "passed": all(row["passed"] for row in checks),
        "checks": checks,
        "criticalFactsAccuracy": sum(row["passed"] for row in checks) / len(checks),
        "routes": {
            "expected": len(expected_routes),
            "detected": len(actual_routes),
            "truePositive": true_positive,
            "precision": precision,
            "recall": recall,
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0,
        },
        "resultStatus": result["status"],
        "limitations": result["coverage"]["limitations"],
    }


def evaluate_projects(
    tested_code: str | Path,
    truth_path: str | Path,
    *,
    out: str | Path,
    runner: ModelRunner | None = None,
    repetitions: int = 2,
    limits: Limits | None = None,
) -> dict:
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    truth = json.loads(Path(truth_path).read_text(encoding="utf-8"))
    destination = Path(out)
    report: dict = {
        "schemaVersion": "1",
        "evaluationVersion": "1",
        "fixtureDate": truth["reviewDate"],
        "mode": "live_model" if runner else "static",
        "repetitions": repetitions,
        "projects": [],
        "modelVerification": {"performed": runner is not None},
    }
    for project in truth["projects"]:
        repo = Path(tested_code) / project["name"]
        mismatches = [
            name
            for name, expected in project["sourceFiles"].items()
            if not (repo / name).is_file()
            or hashlib.sha256((repo / name).read_bytes()).hexdigest() != expected
        ]
        if mismatches:
            raise AnalyzerError("EVALUATION_FIXTURE_CHANGED", "Reviewed source files changed", mismatches)
        baseline_bundle = prepare_context(repo, limits=limits)
        baseline = static_analysis(baseline_bundle)
        repeated_bundle = prepare_context(repo, limits=limits)
        deterministic = canonical_bytes(baseline_bundle) == canonical_bytes(repeated_bundle)
        project_report: dict = {
            "name": project["name"],
            "snapshotId": baseline_bundle["source"]["snapshotId"],
            "contextHash": baseline_bundle["contextHash"],
            "deterministic": deterministic,
            "bundleBytes": len(canonical_bytes(baseline_bundle)),
            "selectedFiles": len(baseline_bundle["selectedFiles"]),
            "evidenceCount": len(baseline_bundle["evidence"]),
            "static": score_result(baseline, baseline_bundle, project),
            "runs": [],
        }
        for repeat in range(repetitions):
            started = time.monotonic()
            try:
                run = analyze_with_report(
                    repo,
                    runner=runner,
                    limits=limits,
                    out=destination / project["name"] / f"run-{repeat + 1:02d}",
                )
                evaluation = score_result(run.result, run.bundle, project)
                record = {
                    "success": True,
                    "quality": evaluation,
                    "calls": run.report["calls"],
                    "durationSeconds": round(time.monotonic() - started, 4),
                }
                response_path = (
                    destination
                    / project["name"]
                    / f"run-{repeat + 1:02d}"
                    / (f"revision-{run.bundle['revision']:02d}")
                    / "model-response.json"
                )
                if runner and response_path.is_file():
                    raw = json.loads(response_path.read_text(encoding="utf-8"))
                    if raw["kind"] == "analysis":
                        raw_score = score_result(raw["result"], run.bundle, project)
                        record["rawModelObservationPreservation"] = {
                            "serviceRoles": [
                                _value(service["role"]) for service in raw["result"]["services"]
                            ],
                            "routeCounts": raw_score["routes"],
                            "providedEnvironmentKeys": len(raw["result"]["environmentKeys"]),
                            "providedDependencies": len(raw["result"]["dependencies"]),
                            "note": "Supplemental replies may omit observations restored by the static merger.",
                        }
            except AnalyzerError as error:
                record = {
                    "success": False,
                    "errorCode": error.code,
                    "durationSeconds": round(time.monotonic() - started, 4),
                }
                failed_report = destination / project["name"] / f"run-{repeat + 1:02d}" / "run-report.json"
                if failed_report.is_file():
                    record["calls"] = json.loads(failed_report.read_text())["calls"]
            project_report["runs"].append(record)
        project_report["passed"] = (
            deterministic
            and project_report["static"]["passed"]
            and all(r["success"] and r["quality"]["passed"] for r in project_report["runs"])
        )
        report["projects"].append(project_report)
        release_snapshot(baseline_bundle["source"]["snapshotId"])
        release_snapshot(repeated_bundle["source"]["snapshotId"])
    records = [run for project in report["projects"] for run in project["runs"]]
    report.update(
        passed=all(project["passed"] for project in report["projects"]),
        successCount=sum(run["success"] for run in records),
        failureCount=sum(not run["success"] for run in records),
    )
    write_json(destination / "quality-report.json", report)
    return report
