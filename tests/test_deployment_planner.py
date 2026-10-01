import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from iris_analyzer.contracts import AnalyzerError, digest
from iris_analyzer.deployment.advisor import validate_advice
from iris_analyzer.deployment.contracts import (
    normalize_planning_request,
    plan_digest,
    validate_deployment_plan,
)
from iris_analyzer.deployment.planner import create_deployment_plan, prepare_planning_request
from iris_analyzer.pipeline import analyze_snapshot


@pytest.fixture(scope="module")
def analysis():
    return analyze_snapshot(Path(__file__).resolve().parents[1] / "fixtures" / "separated-web-api")


def existing_request(analysis):
    request = normalize_planning_request()
    request["target"].update(stack="existing_kubernetes", architecture="x86_64")
    request["bindings"].update(
        existingClusterContext="caller-verified-test-cluster",
        runtimeVerified=True,
        availableCapacity={"cpuMillicores": 8000, "memoryMiB": 16384, "nodeCount": 2},
    )
    for service in analysis["services"]:
        sid = service["serviceId"]
        request["bindings"]["images"].append(
            {
                "serviceId": sid,
                "reference": "registry.example/iris/" + sid + "@sha256:" + "a" * 64,
            }
        )
        request["bindings"]["imagePlatforms"][sid] = "amd64"
    return request


def observation(analysis, service_id, **changes):
    document = {
        "id": "observed-load",
        "serviceId": service_id,
        "sourceSnapshotId": analysis["sourceSnapshotId"],
        "kind": "load",
        "verified": True,
        "measuredAt": datetime.now(timezone.utc).isoformat(),
        "metrics": {
            "peakMemoryMiB": 100,
            "cpuMillicores": 100,
            "achievedRps": 10,
            "p95LatencyMs": 50,
            "errorRate": 0,
        },
        "conditions": {"durationSeconds": 120, "concurrency": 10, "command": "caller verified harness"},
    }
    document.update(changes)
    return document


def test_unknown_request_yields_useful_labeled_proposal_without_approving_spend_or_execution(analysis):
    original = copy.deepcopy(analysis)
    plan = create_deployment_plan(analysis)
    assert plan["status"] == "needs_input"
    assert plan["plannerMode"] == "policy"
    assert not plan["executionEligible"] and not plan["deploymentAuthorized"]
    assert plan["recommendations"]["target"]["value"] == {
        "stack": "aws_eks",
        "cloud": "aws",
        "region": "ap-northeast-2",
        "architecture": "x86_64",
        "environment": "test",
    }
    assert plan["recommendations"]["instance"]["value"] is not None
    assert plan["request"]["constraints"] == {
        "monthlyBudgetUsd": None,
        "expectedRps": None,
        "availability": None,
    }
    assert plan["recommendations"]["cost"]["value"]["monthlyTotalUsd"] is None
    assert analysis == original
    validate_deployment_plan(plan, analysis=analysis)


def test_requested_unsupported_stack_and_location_are_preserved(analysis):
    request = {
        "schemaVersion": "iris.planning-request.v1",
        "target": {
            "stack": "gcp_gke",
            "cloud": "gcp",
            "region": "asia-northeast3",
            "architecture": "arm64",
            "environment": "production",
        },
    }
    plan = create_deployment_plan(analysis, request)
    assert plan["status"] == "unsupported" and plan["adapter"] is None
    assert plan["recommendations"]["target"]["value"] == request["target"]
    assert not plan["executionEligible"]


def test_existing_cluster_becomes_compilable_only_with_caller_verified_bindings(analysis):
    plan = create_deployment_plan(analysis, existing_request(analysis))
    assert plan["status"] == "ready" and plan["executionEligible"]
    assert not plan["deploymentAuthorized"]
    assert plan["recommendations"]["cost"]["value"]["coverage"] == "unknown"


def test_changed_source_ports_and_dropped_application_services_are_rejected_even_after_resealing(analysis):
    plan = create_deployment_plan(analysis, existing_request(analysis))
    workload = plan["configuration"]["workloads"][0]
    workload["containerPorts"][0]["port"] = 45678
    plan["planDigest"] = plan_digest(plan)
    with pytest.raises(AnalyzerError, match="confirmed container"):
        validate_deployment_plan(plan, analysis=analysis)
    plan = create_deployment_plan(analysis, existing_request(analysis))
    plan["configuration"]["workloads"].pop()
    plan["planDigest"] = plan_digest(plan)
    with pytest.raises(AnalyzerError, match="every analyzed application service"):
        validate_deployment_plan(plan, analysis=analysis)


@pytest.mark.parametrize("missing", ["runtime", "image", "platform", "capacity"])
def test_existing_cluster_missing_prerequisites_remain_structured_questions(analysis, missing):
    request = existing_request(analysis)
    if missing == "runtime":
        request["bindings"]["runtimeVerified"] = False
    elif missing == "image":
        request["bindings"]["images"] = []
    elif missing == "platform":
        request["bindings"]["imagePlatforms"] = {}
    else:
        request["bindings"]["availableCapacity"] = None
    plan = create_deployment_plan(analysis, request)
    assert plan["status"] == "needs_input" and not plan["executionEligible"]
    assert any(question["requiredForExecution"] for question in plan["questions"])


def test_insufficient_existing_capacity_reports_needs_input_instead_of_throwing(analysis):
    request = existing_request(analysis)
    request["bindings"]["availableCapacity"] = {"cpuMillicores": 100, "memoryMiB": 128, "nodeCount": 1}
    plan = create_deployment_plan(analysis, request)
    assert plan["status"] == "needs_input" and not plan["executionEligible"]
    assert any("capacity" in question["reason"].lower() for question in plan["questions"])


def test_explicit_resources_and_replica_overrides_win_and_remain_user_basis(analysis):
    request = existing_request(analysis)
    sid = analysis["services"][0]["serviceId"]
    request["overrides"]["resources"] = [
        {
            "serviceId": sid,
            "requests": {"cpuMillicores": 500, "memoryMiB": 512},
            "limits": {"cpuMillicores": 1000, "memoryMiB": 1024},
        }
    ]
    request["overrides"]["replicas"] = [{"serviceId": sid, "replicas": 3}]
    plan = create_deployment_plan(analysis, request)
    workload = next(item for item in plan["configuration"]["workloads"] if item["serviceId"] == sid)
    assert workload["replicas"]["value"] == 3
    assert workload["resources"]["value"]["requests"]["cpuMillicores"] == 500
    assert workload["resources"]["provenance"]["basis"] == ["user"]


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "test"},
        {"verified": False},
        {"sourceSnapshotId": "f" * 64},
        {"measuredAt": (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()},
    ],
)
def test_test_success_stale_or_other_snapshot_measurements_do_not_calibrate_resources(analysis, changes):
    request = existing_request(analysis)
    sid = analysis["services"][1]["serviceId"]
    request["measurements"] = [observation(analysis, sid, **changes)]
    plan = create_deployment_plan(analysis, request)
    workload = next(item for item in plan["configuration"]["workloads"] if item["serviceId"] == sid)
    assert workload["resources"]["provenance"]["basis"] == ["policy"]
    assert workload["resources"]["provenance"]["measurementIds"] == []


def test_scoped_load_measurement_calibrates_headroom_and_replica_demand(analysis):
    request = existing_request(analysis)
    sid = analysis["services"][1]["serviceId"]
    request["constraints"]["expectedRps"] = 70
    request["measurements"] = [observation(analysis, sid)]
    request["measurements"][0]["conditions"]["capacityValidated"] = True
    plan = create_deployment_plan(analysis, request)
    workload = next(item for item in plan["configuration"]["workloads"] if item["serviceId"] == sid)
    assert workload["replicas"]["value"] == 10
    assert workload["resources"]["value"]["requests"]["memoryMiB"] == 192
    assert workload["resources"]["provenance"]["measurementIds"] == ["observed-load"]
    assert "measurement" in workload["resources"]["provenance"]["basis"]


def test_offered_load_is_not_maximum_capacity_or_a_cpu_extrapolation(analysis):
    request = existing_request(analysis)
    sid = analysis["services"][1]["serviceId"]
    request["constraints"]["expectedRps"] = 70
    request["measurements"] = [observation(analysis, sid)]
    plan = create_deployment_plan(analysis, request)
    workload = next(item for item in plan["configuration"]["workloads"] if item["serviceId"] == sid)
    assert workload["replicas"]["value"] == 1
    assert workload["resources"]["value"]["requests"]["cpuMillicores"] == 150
    assert any(q["id"] == "load-range-" + sid and q["requiredForExecution"] for q in plan["questions"])
    assert not plan["executionEligible"]


def test_rate_limited_or_failed_load_does_not_establish_sizing(analysis):
    request = existing_request(analysis)
    sid = analysis["services"][1]["serviceId"]
    measurement = observation(analysis, sid)
    measurement["metrics"]["errorRate"] = 0.9
    request["measurements"] = [measurement]
    plan = create_deployment_plan(analysis, request)
    workload = next(item for item in plan["configuration"]["workloads"] if item["serviceId"] == sid)
    assert workload["resources"]["provenance"]["measurementIds"] == []


def test_budget_below_known_eks_subtotal_and_incomplete_price_blocks_execution(analysis):
    request = prepare_planning_request(
        {
            "schemaVersion": "iris.planning-request.v1",
            "target": {"stack": None, "environment": "test"},
            "constraints": {"monthlyBudgetUsd": 50},
        }
    )
    plan = create_deployment_plan(analysis, request)
    assert {question["id"] for question in plan["questions"]} >= {"budget-exceeded", "budget-uncertain"}
    assert plan["recommendations"]["cost"]["value"]["monthlyTotalUsd"] is None


def test_eks_requested_extended_version_quotes_extended_fee_and_preserves_selection(analysis):
    request = prepare_planning_request()
    request["target"]["stack"] = "aws_eks"
    request["bindings"]["terraformInputs"] = {
        "kubernetesVersion": "1.33",
        "administratorRoleArn": "arn:aws:iam::123456789012:role/test-admin",
        "nodeDiskGiB": 30,
        "desiredNodes": 1,
        "privateNetworkEgressVerified": True,
        "executorPrivateApiReachable": True,
    }
    plan = create_deployment_plan(analysis, request)
    plane = next(
        item
        for item in plan["recommendations"]["cost"]["value"]["lineItems"]
        if item["key"] == "control_plane"
    )
    assert plane["catalogItemKey"] == "eks_control_plane_extended"
    assert plane["monthlyUsd"] == 438
    assert plan["recommendations"]["operatingPolicy"]["value"]["kubernetesVersion"] == "1.33"
    assert plan["configuration"]["terraformInputs"]["kubernetesVersion"] == "1.33"
    assert not plan["executionEligible"]


def test_unsupported_requested_eks_version_preserves_request_and_leaves_control_plane_quote_unknown(analysis):
    request = prepare_planning_request()
    request["bindings"]["terraformInputs"] = {
        "kubernetesVersion": "1.30",
        "administratorRoleArn": "arn:aws:iam::123456789012:role/test-admin",
        "nodeDiskGiB": 30,
        "desiredNodes": 1,
        "privateNetworkEgressVerified": True,
        "executorPrivateApiReachable": True,
    }
    plan = create_deployment_plan(analysis, request)
    plane = next(
        item
        for item in plan["recommendations"]["cost"]["value"]["lineItems"]
        if item["key"] == "control_plane"
    )
    assert plane["monthlyUsd"] is None
    assert plan["configuration"]["terraformInputs"]["kubernetesVersion"] == "1.30"
    assert "kubernetes-version" in {question["id"] for question in plan["questions"]}
    assert plan["status"] == "needs_input"


def test_ai_advice_is_bound_to_input_digests_and_cannot_supply_iac_or_fake_evidence(analysis):
    request = prepare_planning_request()
    advice = {
        "schemaVersion": "iris.planning-advice.v1",
        "analysisDigest": digest(analysis),
        "requestDigest": digest(request),
        "target": {"stack": "aws_eks", "cloud": "aws", "region": "ap-northeast-2", "architecture": "x86_64"},
        "instanceType": None,
        "availability": "single_az",
        "expectedRps": 20,
        "reason": "AI proposal, still unmeasured and awaiting bindings.",
        "workloads": [
            {
                "serviceId": service["serviceId"],
                "cpuMillicores": 150,
                "memoryMiB": 256,
                "reason": "Initial test envelope",
            }
            for service in analysis["services"]
        ],
    }
    plan = create_deployment_plan(analysis, request, policy_proposal=advice)
    assert plan["plannerMode"] == "ai" and plan["status"] == "needs_input"
    assert all(
        workload["resources"]["provenance"]["basis"] == ["policy"]
        for workload in plan["configuration"]["workloads"]
    )
    advice["terraform"] = "resource arbitrary {}"
    with pytest.raises(AnalyzerError, match="bounded advice schema"):
        validate_advice(advice, analysis, request)
    del advice["terraform"]
    advice["analysisDigest"] = "f" * 64
    with pytest.raises(AnalyzerError, match="source analysis"):
        validate_advice(advice, analysis, request)
