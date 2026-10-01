"""Backend composition boundary for analysis, planning and controlled outputs.

All benchmark entries are requests for a later isolated measurement runner. This
module neither executes source nor treats a successful build/test as capacity.
"""

from __future__ import annotations

import copy
from pathlib import Path, PurePosixPath

from iris_analyzer.contracts import AnalyzerError, Limits, canonical_bytes, digest, validate_result
from iris_analyzer.preprocess import expand_context, prepare_context, release_snapshot, save_bundle
from iris_analyzer.preprocess.snapshot import SOURCE_EXTENSIONS
from iris_analyzer.readiness import build_readiness, validate_readiness

from .planner import create_deployment_plan, prepare_planning_request, usable_measurements
from .render import compile_plan


def _same_snapshot(analysis: dict, bundle: dict) -> None:
    validate_result(analysis)
    if analysis["sourceSnapshotId"] != bundle["source"]["snapshotId"]:
        raise AnalyzerError(
            "SOURCE_SNAPSHOT_CHANGED",
            "Deployment planning and source readiness must use the same immutable source snapshot",
        )


def _matching_versions(service: dict, readiness: dict, scope: str) -> list[dict]:
    roots = set(service["componentRoots"]) | {"."}
    if isinstance(service["root"]["value"], str):
        roots.add(service["root"]["value"])
    return [
        copy.deepcopy(item)
        for item in readiness["runtimeVersions"]
        if item["scope"] == scope and item["component"] in roots
    ]


def _build_inputs(service: dict, readiness: dict) -> dict:
    """Keep host source paths, build-stage paths and serving paths separate."""
    roots = set(service["componentRoots"]) | {service["root"]["value"]}
    targets = [copy.deepcopy(row) for row in readiness.get("buildTargets", []) if row["component"] in roots]
    exact = [
        row
        for row in targets
        if row["serviceName"] is not None
        and row["contextPath"] is not None
        and service["serviceId"]
        == "svc-" + digest({"service": row["serviceName"], "root": row["contextPath"]})[:16]
    ]
    if exact:
        targets = exact
    else:
        docker = [row for row in targets if row["dockerfilePath"] is not None and row["serviceName"] is None]
        targets = docker or [row for row in targets if row["dockerfilePath"] is None]
    primary = [row for row in targets if row["condition"] is None]
    selected = primary[0] if len(primary) == 1 and len(targets) == 1 else None
    return {
        "command": copy.deepcopy(service["buildCommand"]),
        "resolvedCommand": selected["buildCommand"] if selected else None,
        "resolvedCommandBasis": selected["buildCommandBasis"] if selected else "unknown",
        "workingDirectory": selected["buildWorkingDirectory"] if selected else None,
        "workingDirectoryScope": "container_build_stage"
        if selected and selected["dockerfilePath"]
        else "repository"
        if selected
        else "unknown",
        "buildContext": selected["contextPath"] if selected else None,
        "dockerfilePath": selected["dockerfilePath"] if selected else None,
        "target": selected["target"] if selected else None,
        "installCommand": selected["installCommand"] if selected else None,
        "installCommandBasis": selected["installCommandBasis"] if selected else "unknown",
        "buildTargets": targets,
        "runtimeWorkingDirectory": copy.deepcopy(service.get("workingDirectory")),
        "outputDirectory": copy.deepcopy(service["outputDirectory"]),
        "imageReferences": _matching_versions(service, readiness, "build"),
        "requiresTargetSelection": selected is None,
    }


def _benchmark_plan(analysis: dict, readiness: dict, plan: dict) -> dict:
    request = plan["request"]
    expected = request["constraints"]["expectedRps"]
    operating_policy = plan["recommendations"]["operatingPolicy"]["value"]
    target_rps = expected if expected is not None else operating_policy["expectedRps"]
    architecture = plan["recommendations"]["target"]["value"]["architecture"]
    scenarios = []
    for service in analysis["services"]:
        sid = service["serviceId"]
        eligible = usable_measurements(analysis, request, sid, architecture=architecture)
        ports = [
            copy.deepcopy(item)
            for item in service["ports"]
            if item["status"] == "detected" and item["scope"] in {"container", "production"}
        ]
        routes = [
            copy.deepcopy(item)
            for item in analysis["apiRoutes"]
            if item["status"] == "detected"
            and isinstance(item["value"], dict)
            and item["value"].get("component", ".") in set(service["componentRoots"]) | {"."}
        ]
        common = {
            "serviceId": sid,
            "sourceSnapshotId": analysis["sourceSnapshotId"],
            "executionAuthorized": False,
            "status": "planned",
            "executed": False,
        }
        scenarios.append(
            {
                "id": "build-" + sid,
                **common,
                "kind": "build",
                "environmentClass": "isolated_build",
                "inputs": _build_inputs(service, readiness),
                "metricsToCollect": ["exitCode", "durationSeconds", "peakMemoryMiB", "cpuSeconds"],
                "capacityEvidenceEligible": False,
                "purpose": "Check build validity and build-worker requirements; these measurements cannot size production serving workloads.",
            }
        )
        scenarios.append(
            {
                "id": "test-" + sid,
                **common,
                "kind": "test",
                "environmentClass": "isolated_test",
                "inputs": {"command": None, "testDatasetRef": None, "sandboxProfile": None},
                "requiredInputs": [
                    "verified test command",
                    "isolated execution profile",
                    "test dataset/fixtures",
                ],
                "metricsToCollect": ["exitCode", "passedTests", "failedTests", "durationSeconds"],
                "capacityEvidenceEligible": False,
                "purpose": "Verify selected correctness checks; passing tests do not establish production memory, throughput or availability.",
            }
        )
        scenarios.append(
            {
                "id": "load-" + sid,
                **common,
                "kind": "load",
                "environmentClass": "production_like_serving",
                "inputs": {
                    "targetBaseUrl": None,
                    "payloadDatasetRef": None,
                    "concurrency": None,
                    "durationSeconds": 300,
                    "warmupSeconds": 30,
                    "targetRps": target_rps,
                    "targetRpsBasis": "user" if expected is not None else "policy_assumption",
                    "targetRpsReason": "User-provided demand scenario; measured results remain limited to tested conditions."
                    if expected is not None
                    else "Initial policy/AI load-test scenario, not predicted demand or verified production capacity.",
                    "candidateServingPorts": ports,
                    "candidateRoutes": routes,
                    "imageReferences": _matching_versions(service, readiness, "runtime"),
                    "deploymentImageBinding": copy.deepcopy(
                        next(
                            (item for item in request["bindings"]["images"] if item["serviceId"] == sid), None
                        )
                    ),
                    "dependencyDatasetRef": None,
                    "requestMix": None,
                },
                "requiredInputs": [
                    "authorized isolated target endpoint",
                    "verified immutable serving image/platform",
                    "representative payloads and route mix",
                    "concurrency and demand target",
                    "production-like dependency data/network",
                    "latency/error acceptance criteria",
                ],
                "metricsToCollect": [
                    "peakMemoryMiB",
                    "cpuMillicores",
                    "achievedRps",
                    "p95LatencyMs",
                    "errorRate",
                ],
                "acceptance": {"maxP95LatencyMs": None, "maxErrorRate": None},
                "capacityEvidenceEligible": True,
                "eligibleExistingMeasurementIds": [item["id"] for item in eligible],
                "purpose": "Collect measured serving capacity for this exact snapshot and workload scenario. The 300-second duration is an initial test policy, not proven coverage.",
            }
        )
    return {
        "schemaVersion": "iris.benchmark-plan.v1",
        "sourceSnapshotId": analysis["sourceSnapshotId"],
        "analysisContextHash": analysis["contextHash"],
        "readinessContextHash": readiness["contextHash"],
        "status": "needs_input",
        "executed": False,
        "executionAuthorized": False,
        "scenarios": scenarios,
        "measurementContract": {
            "schemaVersion": "iris.planning-request.v1",
            "destination": "measurements",
            "requiredIdentity": ["id", "serviceId", "sourceSnapshotId", "kind", "verified", "measuredAt"],
            "resourceMetrics": ["peakMemoryMiB", "cpuMillicores"],
            "loadMetrics": ["achievedRps", "p95LatencyMs", "errorRate"],
            "conditions": ["durationSeconds", "concurrency", "command"],
            "verificationRequired": True,
        },
        "limitations": [
            "Benchmark requests are unexecuted; endpoints, test commands and datasets are intentionally unresolved.",
            "Source/container references are input provenance, not proof that an image builds, runs safely or supports the selected CPU architecture.",
            "Only verified same-snapshot sustained serving-load observations may calibrate capacity; build/test successes remain separate.",
            "Every measured result is limited to its dataset, dependency behavior, concurrency, duration and deployment conditions.",
        ],
    }


def build_deployment_dossier(
    analysis: dict, bundle: dict, request: dict | None = None, *, policy_proposal: dict | None = None
) -> dict:
    """Compose portable backend outputs; no target/measurement/cloud execution."""
    _same_snapshot(analysis, bundle)
    return build_deployment_dossier_from_readiness(
        analysis, build_readiness(bundle), request, policy_proposal=policy_proposal
    )


def build_deployment_dossier_from_readiness(
    analysis: dict,
    source_readiness: dict,
    request: dict | None = None,
    *,
    policy_proposal: dict | None = None,
) -> dict:
    """Compose a prepared supplemental document without loading source text."""
    validate_result(analysis)
    validate_readiness(source_readiness)
    if analysis["sourceSnapshotId"] != source_readiness["sourceSnapshotId"]:
        raise AnalyzerError(
            "SOURCE_SNAPSHOT_CHANGED",
            "Deployment planning and prepared readiness refer to different source snapshots",
        )
    readiness = copy.deepcopy(source_readiness)
    plan = create_deployment_plan(
        analysis, prepare_planning_request(request), readiness=readiness, policy_proposal=policy_proposal
    )
    dossier = {
        "schemaVersion": "iris.deployment-dossier.v1",
        "sourceLink": {
            "sourceSnapshotId": analysis["sourceSnapshotId"],
            "analysisDigest": digest(analysis),
            "analysisContextHash": analysis["contextHash"],
            "readinessContextHash": readiness["contextHash"],
            "contextExpanded": analysis["contextHash"] != readiness["contextHash"],
        },
        "sourceReadiness": readiness,
        "deploymentPlan": plan,
        "execution": compile_plan(plan, analysis=analysis),
        "benchmarkPlan": _benchmark_plan(analysis, readiness, plan),
        "limitations": [
            "Analysis observations, planning recommendations, measured observations and execution configuration remain separate documents.",
            "Expanded readiness evidence uses the same fixed source snapshot; its context hash may differ from the original canonical analysis context.",
            "Compilation eligibility does not authorize deployment, cloud expenditure or source execution.",
            "No benchmark was run by this composition step; a passing source analysis or test suite cannot establish production capacity.",
        ],
    }
    canonical_bytes(dossier)
    return dossier


def _expansion_candidates(bundle: dict, readiness: dict, limits: Limits) -> list[str]:
    manifest = {item["path"]: item for item in bundle["manifest"]}
    verified = set(readiness["coverage"]["completeVerifiedFiles"])
    requested = set(bundle["policy"]["requestedPaths"])
    paths = list(readiness["coverage"]["requiredContextPaths"])
    paths.extend(
        item["path"]
        for item in bundle["selectedFiles"]
        if PurePosixPath(item["path"]).suffix.lower() in SOURCE_EXTENSIONS
    )
    paths = list(dict.fromkeys(paths))
    # Full snippets retain existing observations, so reserve twice the file
    # byte size plus overhead and a 10% safety margin. Expansion enforces the
    # actual hard limit; this estimate never silently truncates source.
    available = max(0, int(limits.max_bundle_bytes * 0.9) - len(canonical_bytes(bundle)))
    accepted = []
    for path in paths:
        if path in verified or path in requested or path not in manifest or not manifest[path]["eligible"]:
            continue
        estimated = manifest[path]["size"] * 2 + 1500
        if estimated > available:
            continue
        accepted.append(path)
        available -= estimated
        if len(accepted) >= limits.max_requested_files:
            break
    return accepted


def prepare_readiness(
    repo: str | Path,
    out: str | Path | None = None,
    analysis: dict | None = None,
    max_bundle_bytes: int = 512000,
) -> dict:
    """Capture source and request bounded whole-file evidence, then release it.

    An optional output directory receives source-readiness.json and a separate
    readiness-context/ containing sanitized evidence. The returned document
    contains observations and coverage only, never source-file text.
    """
    limits = Limits(max_bundle_bytes=max_bundle_bytes, max_expansions=4, max_requested_files=12)
    bundle = prepare_context(repo, limits=limits)
    try:
        if analysis is not None:
            _same_snapshot(analysis, bundle)
        for _ in range(limits.max_expansions):
            readiness = build_readiness(bundle)
            paths = _expansion_candidates(bundle, readiness, limits)
            if not paths:
                break
            previous_hash = bundle["contextHash"]
            bundle = expand_context(bundle, paths, limits=limits)
            if bundle["contextHash"] == previous_hash or any(
                item.get("reason") == "context_budget"
                for item in bundle["coverage"].get("rejectedRequests", [])
            ):
                break
        readiness = build_readiness(bundle)
        if out is not None:
            output = Path(out)
            save_bundle(bundle, output / "readiness-context")
            (output / "source-readiness.json").write_bytes(canonical_bytes(readiness) + b"\n")
        return readiness
    finally:
        release_snapshot(bundle["source"]["snapshotId"])
