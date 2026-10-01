"""Public backend composition preserves source identity and measurement limits."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from iris_analyzer.contracts import AnalyzerError, canonical_bytes, digest
from iris_analyzer.deployment.dossier import (
    build_deployment_dossier,
    build_deployment_dossier_from_readiness,
    prepare_readiness,
)
from iris_analyzer.deployment.planner import prepare_planning_request
from iris_analyzer.preprocess import expand_context, prepare_context, release_snapshot
from iris_analyzer.readiness import build_readiness
from iris_analyzer.result import static_analysis


@pytest.fixture
def source(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "dossier-api",
                "engines": {"node": ">=22 <23"},
                "scripts": {"start": "node src/index.js"},
                "dependencies": {"express": "5"},
            }
        )
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src/index.js").write_text(
        "import express from 'express';\nconst app = express();\napp.listen(3000);\n"
    )
    (tmp_path / ".nvmrc").write_text("22.8.0\n")
    (tmp_path / "Dockerfile").write_text(
        "FROM node:22 AS build\nRUN npm run build\nFROM node:22\nWORKDIR /app\n"
        'EXPOSE 3000\nCMD ["node", "src/index.js"]\n'
    )
    return tmp_path


@pytest.fixture
def analyzed(source):
    bundle = prepare_context(source)
    try:
        yield source, bundle, static_analysis(bundle)
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def request(**parts):
    target = {"stack": None, "environment": "test", **parts.pop("target", {})}
    return {"schemaVersion": "iris.planning-request.v1", "target": target, "pricingCatalog": None, **parts}


def test_dossier_has_separate_documents_and_no_target_source_text(analyzed):
    _, bundle, analysis = analyzed
    original_bundle, original_analysis = copy.deepcopy(bundle), copy.deepcopy(analysis)
    dossier = build_deployment_dossier(analysis, bundle, request())
    assert dossier["schemaVersion"] == "iris.deployment-dossier.v1"
    assert dossier["sourceReadiness"]["schemaVersion"] == "iris.source-readiness.v1"
    assert dossier["deploymentPlan"]["schemaVersion"] == "iris.deployment-plan.v1"
    assert dossier["execution"]["deploymentAuthorized"] is False
    assert dossier["benchmarkPlan"]["executed"] is False
    assert "evidence" not in dossier and "bundle" not in dossier
    assert "const app = express()" not in json.dumps(dossier)
    assert bundle == original_bundle and analysis == original_analysis
    assert canonical_bytes(dossier)


def test_missing_measurements_are_policy_assumptions_not_tested_capacity(analyzed):
    _, bundle, analysis = analyzed
    dossier = build_deployment_dossier(analysis, bundle, request())
    assert dossier["deploymentPlan"]["status"] == "needs_input"
    assert dossier["execution"]["status"] == "blocked"
    workload = dossier["deploymentPlan"]["configuration"]["workloads"][0]
    assert workload["resources"]["provenance"]["basis"] == ["policy"]
    assert workload["resources"]["provenance"]["measurementIds"] == []
    load = next(item for item in dossier["benchmarkPlan"]["scenarios"] if item["kind"] == "load")
    assert load["inputs"]["targetBaseUrl"] is None
    assert load["inputs"]["targetRps"] == 20
    assert load["inputs"]["targetRpsBasis"] == "policy_assumption"
    assert "not predicted demand" in load["inputs"]["targetRpsReason"]
    assert load["inputs"]["concurrency"] is None
    assert load["acceptance"]["maxP95LatencyMs"] is None
    assert load["eligibleExistingMeasurementIds"] == []


def test_load_scenarios_keep_serving_ports_and_declared_image_provenance(analyzed):
    _, bundle, analysis = analyzed
    dossier = build_deployment_dossier(analysis, bundle, request(constraints={"expectedRps": 55}))
    scenarios = dossier["benchmarkPlan"]["scenarios"]
    assert {item["kind"] for item in scenarios} == {"build", "test", "load"}
    load = next(item for item in scenarios if item["kind"] == "load")
    assert load["inputs"]["targetRps"] == 55 and load["inputs"]["targetRpsBasis"] == "user"
    assert {field["value"] for field in load["inputs"]["candidateServingPorts"]} == {3000}
    assert load["inputs"]["imageReferences"][0]["evidenceIds"]
    assert load["inputs"]["imageReferences"][0]["imageReference"] == "node:22"
    assert all(item["executionAuthorized"] is False and item["executed"] is False for item in scenarios)
    assert all(item["capacityEvidenceEligible"] is False for item in scenarios if item["kind"] != "load")


def test_build_and_test_measurements_never_count_as_serving_load(analyzed):
    _, bundle, analysis = analyzed
    sid = analysis["services"][0]["serviceId"]
    now = datetime.now(timezone.utc).isoformat()
    measurements = [
        {
            "id": "passed-build",
            "serviceId": sid,
            "sourceSnapshotId": analysis["sourceSnapshotId"],
            "kind": "build",
            "verified": True,
            "measuredAt": now,
            "metrics": {
                "peakMemoryMiB": 1200,
                "cpuMillicores": 1500,
                "achievedRps": None,
                "p95LatencyMs": None,
                "errorRate": None,
            },
            "conditions": {"durationSeconds": 120, "concurrency": None, "command": "npm run build"},
        }
    ]
    dossier = build_deployment_dossier(analysis, bundle, request(measurements=measurements))
    workload = dossier["deploymentPlan"]["configuration"]["workloads"][0]
    assert workload["resources"]["provenance"]["measurementIds"] == []
    load = next(item for item in dossier["benchmarkPlan"]["scenarios"] if item["kind"] == "load")
    assert load["eligibleExistingMeasurementIds"] == []


@pytest.mark.parametrize("architecture", [None, "x86_64", "arm64"])
def test_verified_same_snapshot_load_can_calibrate_but_stays_scenario_bound(analyzed, architecture):
    _, bundle, analysis = analyzed
    sid = analysis["services"][0]["serviceId"]
    measurements = [
        {
            "id": "load-one",
            "serviceId": sid,
            "sourceSnapshotId": analysis["sourceSnapshotId"],
            "kind": "load",
            "verified": True,
            "measuredAt": datetime.now(timezone.utc).isoformat(),
            "metrics": {
                "peakMemoryMiB": 160,
                "cpuMillicores": 100,
                "achievedRps": 80,
                "p95LatencyMs": 30,
                "errorRate": 0.001,
            },
            "conditions": {"durationSeconds": 300, "concurrency": 20, "command": "verified-load-run"},
        }
    ]
    if architecture is not None:
        measurements[0]["conditions"]["architecture"] = architecture
    dossier = build_deployment_dossier(analysis, bundle, request(measurements=measurements))
    workload = dossier["deploymentPlan"]["configuration"]["workloads"][0]
    eligible_ids = [] if architecture == "arm64" else ["load-one"]
    assert workload["resources"]["provenance"]["measurementIds"] == eligible_ids
    assert workload["resources"]["provenance"]["basis"] == (
        ["measurement", "policy"] if eligible_ids else ["policy"]
    )
    load = next(item for item in dossier["benchmarkPlan"]["scenarios"] if item["kind"] == "load")
    assert load["eligibleExistingMeasurementIds"] == eligible_ids
    assert load["executed"] is False


def test_validated_optional_ai_policy_remains_an_assumption(analyzed):
    _, bundle, analysis = analyzed
    supplied = request()
    sid = analysis["services"][0]["serviceId"]
    proposal = {
        "schemaVersion": "iris.planning-advice.v1",
        "analysisDigest": digest(analysis),
        "requestDigest": digest(prepare_planning_request(supplied)),
        "target": {"stack": "aws_eks", "cloud": "aws", "region": "ap-northeast-2", "architecture": "x86_64"},
        "instanceType": None,
        "availability": "single_az",
        "expectedRps": 50,
        "reason": "Unmeasured initial scenario",
        "workloads": [
            {"serviceId": sid, "cpuMillicores": 500, "memoryMiB": 768, "reason": "Initial assumed envelope"}
        ],
    }
    dossier = build_deployment_dossier(analysis, bundle, supplied, policy_proposal=proposal)
    plan = dossier["deploymentPlan"]
    assert plan["plannerMode"] == "ai"
    resources = plan["configuration"]["workloads"][0]["resources"]
    assert resources["value"]["requests"]["memoryMiB"] == 768
    assert resources["provenance"]["basis"] == ["policy"]
    assert resources["provenance"]["measurementIds"] == []
    load = dossier["benchmarkPlan"]["scenarios"][-1]
    assert load["inputs"]["targetRps"] == 50
    assert load["inputs"]["targetRpsBasis"] == "policy_assumption"
    assert "not predicted demand" in load["inputs"]["targetRpsReason"]


def test_prepared_expanded_context_hash_is_linked_without_changing_analysis(analyzed):
    _, bundle, analysis = analyzed
    expanded = expand_context(bundle, [".nvmrc", "Dockerfile"])
    dossier = build_deployment_dossier(analysis, expanded, request())
    link = dossier["sourceLink"]
    assert link["contextExpanded"] is True
    assert link["sourceSnapshotId"] == analysis["sourceSnapshotId"]
    assert link["analysisContextHash"] == analysis["contextHash"]
    assert link["readinessContextHash"] == expanded["contextHash"]
    assert dossier["deploymentPlan"]["source"]["contextHash"] == analysis["contextHash"]
    assert any(item["scope"] == "build" for item in dossier["sourceReadiness"]["runtimeVersions"])


def test_different_source_snapshot_is_rejected(analyzed):
    _, bundle, analysis = analyzed
    changed = copy.deepcopy(analysis)
    changed["sourceSnapshotId"] = "b" * 64
    with pytest.raises(AnalyzerError) as exc:
        build_deployment_dossier(changed, bundle, request())
    assert exc.value.code == "SOURCE_SNAPSHOT_CHANGED"
    readiness = build_readiness(bundle)
    readiness["sourceSnapshotId"] = "c" * 64
    with pytest.raises(AnalyzerError) as exc:
        build_deployment_dossier_from_readiness(analysis, readiness, request())
    assert exc.value.code == "SOURCE_SNAPSHOT_CHANGED"


def test_prepare_readiness_checks_whole_files_and_saves_only_sanitized_evidence(analyzed, tmp_path):
    repo, bundle, analysis = analyzed
    # Place output outside the captured source directory.
    output = tmp_path.parent / (tmp_path.name + "-readiness-output")
    report = prepare_readiness(repo, out=output, analysis=analysis)
    assert report["sourceSnapshotId"] == bundle["source"]["snapshotId"]
    assert report["coverage"]["syntaxCheckedFiles"] == ["src/index.js"]
    assert "Dockerfile" in report["coverage"]["containerCheckedFiles"]
    assert (output / "readiness-context/evidence.jsonl").is_file()
    saved = json.loads((output / "source-readiness.json").read_text())
    assert saved == report
    assert "const app = express()" not in json.dumps(report)


def test_prepare_readiness_rejects_source_changed_after_analysis_and_releases_capture(analyzed, monkeypatch):
    repo, _, analysis = analyzed
    (repo / "src/index.js").write_text("export const changed = true;\n")
    released = []
    from iris_analyzer.deployment import dossier as module

    original = module.release_snapshot
    monkeypatch.setattr(
        module, "release_snapshot", lambda snapshot: (released.append(snapshot), original(snapshot))
    )
    with pytest.raises(AnalyzerError) as exc:
        prepare_readiness(repo, analysis=analysis)
    assert exc.value.code == "SOURCE_SNAPSHOT_CHANGED"
    assert len(released) == 1


def test_reported_initial_source_errors_block_compilation(source):
    (source / "src/index.js").write_text("const = ;\n")
    bundle = prepare_context(source)
    try:
        analysis = static_analysis(bundle)
        dossier = build_deployment_dossier(analysis, bundle, request())
        assert any(item["severity"] == "error" for item in dossier["sourceReadiness"]["findings"])
        assert any(
            item["id"] == "readiness" and item["requiredForExecution"]
            for item in dossier["deploymentPlan"]["questions"]
        )
        assert dossier["execution"]["status"] == "blocked"
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_alternate_requested_stack_is_preserved_as_unsupported(analyzed):
    _, bundle, analysis = analyzed
    dossier = build_deployment_dossier(
        analysis, bundle, request(target={"stack": "cloud_run", "cloud": "gcp", "region": "asia-northeast3"})
    )
    plan = dossier["deploymentPlan"]
    assert plan["recommendations"]["target"]["value"]["stack"] == "cloud_run"
    assert plan["status"] == "unsupported" and dossier["execution"]["status"] == "blocked"


def test_pricing_default_applies_only_when_caller_omits_catalog(analyzed):
    _, bundle, analysis = analyzed
    dossier = build_deployment_dossier(analysis, bundle)
    assert dossier["deploymentPlan"]["request"]["pricingCatalog"] is not None
    dossier = build_deployment_dossier(analysis, bundle, request())
    assert dossier["deploymentPlan"]["request"]["pricingCatalog"] is None


def test_configured_existing_cluster_compiles_but_never_authorizes_execution(analyzed):
    _, bundle, analysis = analyzed
    sid = analysis["services"][0]["serviceId"]
    dossier = build_deployment_dossier(
        analysis,
        bundle,
        request(
            target={"stack": "existing_kubernetes", "architecture": "x86_64"},
            bindings={
                "existingClusterContext": "test-cluster",
                "runtimeVerified": True,
                "availableCapacity": {"cpuMillicores": 4000, "memoryMiB": 8192, "nodeCount": 2},
                "images": [{"serviceId": sid, "reference": "ghcr.io/example/iris-api@sha256:" + "a" * 64}],
                "imagePlatforms": {sid: "amd64"},
            },
        ),
    )
    assert dossier["deploymentPlan"]["status"] == "ready"
    assert dossier["execution"]["status"] == "ready"
    assert dossier["execution"]["deploymentAuthorized"] is False
    assert dossier["execution"]["terraform"] is None
    assert dossier["execution"]["kubernetes"]


@pytest.mark.parametrize("name", ["Temp_log", "portpolio-production"])
def test_preparation_on_user_samples_reports_real_bounded_syntax_coverage(name):
    repo = Path(__file__).resolve().parents[2] / "tested_code" / name
    if not repo.is_dir():
        pytest.skip("User sample is not present")
    report = prepare_readiness(repo)
    assert report["coverage"]["syntaxCheckedFiles"]
    assert report["coverage"]["containerCheckedFiles"]
    assert report["coverage"]["targetCodeExecuted"] is False
    assert not any(item["ruleId"] == "syntax.javascript_parser" for item in report["findings"])


@pytest.mark.parametrize("compose_name,required", [("compose.tunnel.yaml", False), ("compose.yaml", True)])
def test_source_conditions_and_inherited_runtime_survive_ai_recommendation(source, compose_name, required):
    from iris_analyzer.deployment.advisor import advisor_input

    (source / "Dockerfile").write_text(
        "FROM node:24-alpine AS base\nFROM base AS intermediate\nFROM intermediate AS runtime\n"
        'WORKDIR /app\nEXPOSE 3000\nCMD ["node", "src/index.js"]\n'
    )
    (source / compose_name).write_text(
        "services:\n  app:\n    build: .\n    volumes:\n      - ./secrets/token:/run/secrets/token:ro\n"
    )
    bundle = prepare_context(source)
    try:
        analysis = static_analysis(bundle)
        expanded = expand_context(bundle, ["Dockerfile", "package.json", compose_name])
        readiness = build_readiness(expanded)
        request = prepare_planning_request()
        ai_input = advisor_input(analysis, request, readiness=readiness)
        dependency = next(
            d
            for d in analysis["dependencies"]
            if isinstance(d["value"], dict) and d["value"].get("mountPath") == "/run/secrets/token"
        )
        assert dependency in ai_input["analysisResult"]["dependencies"]
        assert ai_input["sourceReadiness"] == readiness
        assert any(
            v["scope"] == "runtime" and v["constraint"] == "24-alpine" for v in readiness["runtimeVersions"]
        )
        assert any(f["ruleId"] == "runtime.incompatible_major_constraint" for f in readiness["findings"])
        advice = {
            "schemaVersion": "iris.planning-advice.v1",
            "analysisDigest": digest(analysis),
            "requestDigest": digest(request),
            "target": {
                "stack": "aws_eks",
                "cloud": "aws",
                "region": "ap-northeast-2",
                "architecture": "x86_64",
            },
            "instanceType": None,
            "availability": "single_az",
            "expectedRps": 20,
            "reason": "Synthetic offline policy proposal",
            "workloads": [],
        }
        dossier = build_deployment_dossier_from_readiness(
            analysis, readiness, request, policy_proposal=advice
        )
        assert dossier["sourceReadiness"] == readiness
        assert dossier["deploymentPlan"]["plannerMode"] == "ai"
        question = next(
            q
            for q in dossier["deploymentPlan"]["questions"]
            if q["id"].startswith("bind-") and "token" in q["id"]
        )
        assert question["requiredForExecution"] is required
        if not required:
            assert f"when Compose file {compose_name} is selected" in question["reason"]
        assert dependency == next(d for d in ai_input["analysisResult"]["dependencies"] if d == dependency)
    finally:
        release_snapshot(bundle["source"]["snapshotId"])
