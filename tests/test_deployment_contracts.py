import copy
from datetime import datetime, timedelta, timezone

import pytest

from iris_analyzer.contracts import AnalyzerError, digest
from iris_analyzer.deployment import lifecycle
from iris_analyzer.deployment.contracts import (
    DEFAULT_PLANNING_REQUEST,
    normalize_planning_request,
    plan_digest,
    validate_deployment_plan,
    validate_planning_request,
)


def recommendation(value):
    return {
        "value": value,
        "provenance": {
            "basis": ["policy"],
            "evidenceIds": [],
            "measurementIds": [],
            "assumptionIds": ["initial-capacity"],
            "reason": "Initial estimate to be checked by a scoped benchmark.",
        },
    }


def sealed(plan):
    plan["requestDigest"] = digest(plan["request"])
    plan["planDigest"] = plan_digest(plan)
    return plan


@pytest.fixture
def ready_plan():
    request = normalize_planning_request()
    request["target"].update(stack="existing_kubernetes", architecture="x86_64")
    request["bindings"].update(
        existingClusterContext="verified-test-cluster",
        imagePlatforms={"api": "amd64"},
        runtimeVerified=True,
        availableCapacity={"cpuMillicores": 1500, "memoryMiB": 4096, "nodeCount": 1},
    )
    image = "registry.example.test/iris/api@sha256:" + "a" * 64
    request["bindings"]["images"] = [{"serviceId": "api", "reference": image}]
    probe = {
        "kind": "tcp",
        "port": 8080,
        "path": None,
        "initialDelaySeconds": 5,
        "periodSeconds": 10,
        "timeoutSeconds": 2,
        "failureThreshold": 3,
    }
    configuration = {
        key: copy.deepcopy(request["bindings"][key])
        for key in (
            "namespace",
            "clusterName",
            "existingClusterContext",
            "imagePlatforms",
            "terraformInputs",
            "ingressVerified",
            "storageDriverVerified",
            "databasesVerified",
            "availableCapacity",
            "runtimeVerified",
        )
    }
    configuration["workloads"] = [
        {
            "serviceId": "api",
            "name": "api",
            "image": image,
            "containerPorts": [{"name": "http", "port": 8080, "protocol": "TCP"}],
            "command": None,
            "args": None,
            "resources": recommendation(
                {
                    "requests": {"cpuMillicores": 250, "memoryMiB": 256},
                    "limits": {"cpuMillicores": 500, "memoryMiB": 512},
                }
            ),
            "replicas": recommendation(1),
            "service": recommendation(
                {"type": "ClusterIP", "port": 80, "targetPort": 8080, "protocol": "TCP"}
            ),
            "ingress": recommendation(
                {"enabled": False, "host": None, "className": None, "tlsSecretName": None, "path": "/"}
            ),
            "probes": recommendation({"readiness": probe, "liveness": probe, "startup": None}),
            "volumes": recommendation([]),
            "secretRefs": recommendation([]),
            "rollout": recommendation(
                {
                    "strategy": "RollingUpdate",
                    "maxSurge": 1,
                    "maxUnavailable": 0,
                    "progressDeadlineSeconds": 600,
                }
            ),
            "rollback": recommendation(
                {"strategy": "helm_atomic", "timeoutSeconds": 600, "revisionHistoryLimit": 3}
            ),
        }
    ]
    return sealed(
        {
            "schemaVersion": "iris.deployment-plan.v1",
            "requestDigest": "0" * 64,
            "analysisDigest": "b" * 64,
            "source": {"sourceSnapshotId": "c" * 64, "contextHash": "d" * 64, "analysisStatus": "complete"},
            "request": request,
            "adapter": "existing_kubernetes",
            "status": "ready",
            "plannerMode": "policy",
            "executionEligible": True,
            "deploymentAuthorized": False,
            "assumptions": [
                {
                    "id": "initial-capacity",
                    "description": "Small test workload.",
                    "impact": "Rerun load test before production.",
                }
            ],
            "questions": [],
            "recommendations": {
                "target": recommendation(copy.deepcopy(request["target"])),
                "instance": recommendation(None),
                "cost": recommendation(
                    {
                        "currency": "USD",
                        "monthlyTotalUsd": None,
                        "coverage": "unknown",
                        "lineItems": [
                            {
                                "key": "cluster",
                                "monthlyUsd": None,
                                "reason": "Existing cluster cost is unmeasured.",
                            }
                        ],
                    }
                ),
                "network": recommendation(
                    {
                        "vpcMode": "existing",
                        "vpcId": None,
                        "subnetIds": [],
                        "availabilityZones": [],
                        "natGatewayCount": 0,
                        "publicIngress": False,
                    }
                ),
                "databases": recommendation([]),
                "storage": recommendation([]),
                "observability": recommendation({"prometheusEnabled": False, "scrapeIntervalSeconds": 30}),
            },
            "configuration": configuration,
        }
    )


def test_unknown_inputs_are_preserved_and_defaults_do_not_fabricate_capacity():
    request = normalize_planning_request(
        {
            "schemaVersion": "iris.planning-request.v1",
            "target": {"stack": None, "environment": "test"},
        }
    )
    assert request == DEFAULT_PLANNING_REQUEST
    assert request["constraints"]["monthlyBudgetUsd"] is None
    assert request["constraints"]["expectedRps"] is None
    request["target"]["stack"] = "user-custom-cloud-stack"
    assert validate_planning_request(request)["target"]["stack"] == "user-custom-cloud-stack"
    assert DEFAULT_PLANNING_REQUEST["target"]["stack"] is None


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), "250m", True])
def test_resource_quantities_are_finite_positive_unambiguous_integers(value):
    request = normalize_planning_request()
    request["overrides"]["resources"] = [
        {
            "serviceId": "api",
            "requests": {"cpuMillicores": value, "memoryMiB": 256},
            "limits": {"cpuMillicores": 500, "memoryMiB": 512},
        }
    ]
    with pytest.raises(AnalyzerError):
        validate_planning_request(request)


def test_duplicate_images_and_inline_secrets_are_rejected():
    request = normalize_planning_request()
    binding = {"serviceId": "api", "reference": "registry.example/api@sha256:" + "a" * 64}
    request["bindings"]["images"] = [binding, binding]
    with pytest.raises(AnalyzerError, match="unique"):
        validate_planning_request(request)
    request["bindings"]["images"] = [binding]
    request["bindings"]["secretValues"] = {"PASSWORD": "must-not-be-accepted"}
    with pytest.raises(AnalyzerError, match="contract"):
        validate_planning_request(request)


def test_ready_means_compilable_and_never_authorizes_deployment(ready_plan):
    assert validate_deployment_plan(ready_plan) is ready_plan
    assert ready_plan["deploymentAuthorized"] is False
    ready_plan["deploymentAuthorized"] = True
    with pytest.raises(AnalyzerError):
        validate_deployment_plan(sealed(ready_plan))


def test_plan_and_request_seals_detect_changed_inputs(ready_plan):
    ready_plan["configuration"]["namespace"] = "changed"
    with pytest.raises(AnalyzerError, match="immutable digest"):
        validate_deployment_plan(ready_plan)
    ready_plan["planDigest"] = plan_digest(ready_plan)
    with pytest.raises(AnalyzerError, match="supplied inputs"):
        validate_deployment_plan(ready_plan)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(executionEligible=False),
        lambda p: p["configuration"].update(runtimeVerified=False),
        lambda p: p["configuration"].update(
            availableCapacity={"cpuMillicores": 100, "memoryMiB": 100, "nodeCount": 1}
        ),
        lambda p: p["configuration"]["workloads"][0].update(image=None),
        lambda p: p["configuration"]["workloads"][0]["probes"]["value"].update(readiness=None),
        lambda p: p["questions"].append(
            {
                "id": "unresolved",
                "field": "database",
                "reason": "Needs explicit binding",
                "requiredForExecution": True,
            }
        ),
    ],
)
def test_ready_rejects_missing_prerequisites_and_insufficient_surge_capacity(ready_plan, mutation):
    mutation(ready_plan)
    with pytest.raises(AnalyzerError):
        validate_deployment_plan(sealed(ready_plan))


def test_source_only_estimates_and_unreferenced_policy_assumptions_are_rejected(ready_plan):
    provenance = ready_plan["configuration"]["workloads"][0]["resources"]["provenance"]
    provenance.update(basis=["source"], evidenceIds=["ev-runtime"])
    with pytest.raises(AnalyzerError, match="Source code alone"):
        validate_deployment_plan(sealed(ready_plan))
    provenance.update(basis=["policy"], evidenceIds=[], assumptionIds=[])
    with pytest.raises(AnalyzerError, match="disclose their assumptions"):
        validate_deployment_plan(sealed(ready_plan))


def test_measurement_must_be_verified_scoped_and_bound_to_same_snapshot(ready_plan):
    measurement = {
        "id": "load-test",
        "serviceId": "api",
        "sourceSnapshotId": "c" * 64,
        "kind": "load",
        "verified": True,
        "measuredAt": "2026-10-01T00:00:00Z",
        "metrics": {
            "peakMemoryMiB": 160,
            "cpuMillicores": 100,
            "achievedRps": 20,
            "p95LatencyMs": 50,
            "errorRate": 0,
        },
        "conditions": {"durationSeconds": 60, "concurrency": 10, "command": "caller supplied load harness"},
    }
    ready_plan["request"]["measurements"] = [measurement]
    provenance = ready_plan["configuration"]["workloads"][0]["resources"]["provenance"]
    provenance.update(basis=["measurement"], measurementIds=["load-test"])
    validate_deployment_plan(sealed(ready_plan))
    measurement["sourceSnapshotId"] = "e" * 64
    with pytest.raises(AnalyzerError, match="another source snapshot"):
        validate_deployment_plan(sealed(ready_plan))
    measurement["sourceSnapshotId"] = "c" * 64
    measurement["serviceId"] = "another-service"
    with pytest.raises(AnalyzerError, match="another service"):
        validate_deployment_plan(sealed(ready_plan))
    measurement["serviceId"] = "api"
    measurement["kind"] = "test"
    with pytest.raises(AnalyzerError, match="passing build or test"):
        validate_deployment_plan(sealed(ready_plan))


def test_partial_cost_cannot_be_presented_as_total(ready_plan):
    cost = ready_plan["recommendations"]["cost"]["value"]
    cost.update(coverage="partial", monthlyTotalUsd=10)
    with pytest.raises(AnalyzerError, match="total deployment cost"):
        validate_deployment_plan(sealed(ready_plan))
    cost.update(
        coverage="complete",
        lineItems=[{"key": "instance", "monthlyUsd": 10, "reason": "Unverified caller input"}],
    )
    with pytest.raises(AnalyzerError, match="verified catalog"):
        validate_deployment_plan(sealed(ready_plan))


def test_explicit_stack_architecture_and_replica_overrides_cannot_be_ignored(ready_plan):
    ready_plan["request"]["target"]["architecture"] = "arm64"
    with pytest.raises(AnalyzerError, match="requested target"):
        validate_deployment_plan(sealed(ready_plan))
    ready_plan["request"]["target"]["architecture"] = "x86_64"
    ready_plan["request"]["overrides"]["replicas"] = [{"serviceId": "api", "replicas": 2}]
    with pytest.raises(AnalyzerError, match="replica override"):
        validate_deployment_plan(sealed(ready_plan))


def test_ports_probe_paths_limits_and_rollout_are_semantically_checked(ready_plan):
    workload = ready_plan["configuration"]["workloads"][0]
    workload["service"]["value"]["targetPort"] = 3000
    with pytest.raises(AnalyzerError, match="targetPort"):
        validate_deployment_plan(sealed(ready_plan))
    workload["service"]["value"]["targetPort"] = 8080
    workload["resources"]["value"]["limits"]["memoryMiB"] = 128
    with pytest.raises(AnalyzerError, match="smaller"):
        validate_deployment_plan(sealed(ready_plan))


def test_source_digest_is_checked_against_original_analysis(ready_plan):
    analysis = {
        "schemaVersion": "1",
        "status": "complete",
        "sourceSnapshotId": "c" * 64,
        "contextHash": "d" * 64,
        "services": [],
        "dependencies": [],
        "apiRoutes": [],
        "environmentKeys": [],
        "connections": [],
        "questions": [],
        "coverage": {"completeForProfile": True, "limitations": []},
    }
    with pytest.raises(AnalyzerError, match="immutable analysis"):
        validate_deployment_plan(ready_plan, analysis=analysis)
    ready_plan["analysisDigest"] = digest(analysis)
    with pytest.raises(AnalyzerError, match="unknown source service"):
        validate_deployment_plan(sealed(ready_plan), analysis=analysis)


def test_priced_components_require_current_scoped_catalog_and_match_quantity(ready_plan):
    target = {**ready_plan["request"]["target"], "cloud": "aws", "region": "ap-northeast-2"}
    ready_plan["request"]["target"] = target
    ready_plan["recommendations"]["target"]["value"] = copy.deepcopy(target)
    catalog = {
        "currency": "USD",
        "sourceUrl": "https://aws.amazon.com/ec2/pricing/",
        "verifiedAt": datetime.now(timezone.utc).isoformat(),
        "verified": True,
        "items": [
            {
                "key": "test-instance",
                "cloud": "aws",
                "region": "ap-northeast-2",
                "unit": "hour",
                "unitPriceUsd": 0.1,
                "instanceType": "test.small",
                "architecture": "x86_64",
                "cpuMillicores": 2000,
                "memoryMiB": 2048,
            }
        ],
    }
    ready_plan["request"]["pricingCatalog"] = catalog
    cost = ready_plan["recommendations"]["cost"]["value"]
    cost.update(
        coverage="partial",
        lineItems=[
            {
                "key": "instance",
                "monthlyUsd": 73,
                "reason": "730 hours assumption",
                "catalogItemKey": "test-instance",
                "quantity": 730,
            }
        ],
    )
    validate_deployment_plan(sealed(ready_plan))
    catalog["items"][0]["region"] = "us-east-1"
    with pytest.raises(AnalyzerError, match="selected cloud and region"):
        validate_deployment_plan(sealed(ready_plan))
    catalog["items"][0]["region"] = "ap-northeast-2"
    catalog["verifiedAt"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    with pytest.raises(AnalyzerError, match="last 30 days"):
        validate_deployment_plan(sealed(ready_plan))
    catalog["verifiedAt"] = datetime.now(timezone.utc).isoformat()
    cost["lineItems"][0]["monthlyUsd"] = 72
    with pytest.raises(AnalyzerError, match="price and quantity"):
        validate_deployment_plan(sealed(ready_plan))
    cost["lineItems"][0]["monthlyUsd"] = 73
    ready_plan["request"]["constraints"]["monthlyBudgetUsd"] = 50
    with pytest.raises(AnalyzerError, match="budget cap"):
        validate_deployment_plan(sealed(ready_plan))


def configure_eks(plan, version):
    target = {
        "stack": "aws_eks",
        "cloud": "aws",
        "region": "ap-northeast-2",
        "architecture": "x86_64",
        "environment": "test",
    }
    plan["request"]["target"] = target
    plan["adapter"] = "aws_eks"
    plan["recommendations"]["target"]["value"] = copy.deepcopy(target)
    inputs = {
        "kubernetesVersion": version,
        "administratorRoleArn": "arn:aws:iam::123456789012:role/test-admin",
        "nodeDiskGiB": 30,
        "desiredNodes": 2,
        "privateNetworkEgressVerified": True,
        "executorPrivateApiReachable": True,
    }
    network = {
        "vpcMode": "existing",
        "vpcId": "vpc-abc123",
        "subnetIds": ["subnet-abc123", "subnet-def456"],
        "availabilityZones": ["ap-northeast-2a", "ap-northeast-2c"],
        "natGatewayCount": 0,
        "publicIngress": False,
    }
    plan["request"]["bindings"].update(
        clusterName="iris-test", existingClusterContext=None, terraformInputs=inputs, network=network
    )
    plan["configuration"].update(
        clusterName="iris-test", existingClusterContext=None, terraformInputs=copy.deepcopy(inputs)
    )
    plan["recommendations"]["network"]["value"] = copy.deepcopy(network)
    plan["recommendations"]["instance"]["value"] = {
        "instanceType": "t3.medium",
        "cpuMillicores": 2000,
        "memoryMiB": 4096,
        "minNodes": 1,
        "maxNodes": 3,
    }


def price_eks_plane(plan, key, rate):
    plan["request"]["pricingCatalog"] = {
        "currency": "USD",
        "sourceUrl": "https://aws.amazon.com/eks/pricing/",
        "verifiedAt": datetime.now(timezone.utc).isoformat(),
        "verified": True,
        "items": [
            {
                "key": key,
                "cloud": "aws",
                "region": "ap-northeast-2",
                "unit": "hour",
                "unitPriceUsd": rate,
                "instanceType": None,
                "architecture": None,
                "cpuMillicores": None,
                "memoryMiB": None,
            }
        ],
    }
    plan["recommendations"]["cost"]["value"].update(
        coverage="partial",
        lineItems=[
            {
                "key": "control_plane",
                "catalogItemKey": key,
                "quantity": 730,
                "monthlyUsd": rate * 730,
                "reason": "Verified version support tier; remaining costs are unpriced.",
            }
        ],
    )


@pytest.mark.parametrize(
    "version,key,rate",
    [
        ("1.36", "eks_control_plane", 0.1),
        ("1.33", "eks_control_plane_extended", 0.6),
    ],
)
def test_eks_control_plane_cost_matches_the_configured_support_tier(ready_plan, version, key, rate):
    configure_eks(ready_plan, version)
    price_eks_plane(ready_plan, key, rate)
    validate_deployment_plan(sealed(ready_plan))


def test_extended_version_cannot_quote_standard_control_plane_price(ready_plan):
    configure_eks(ready_plan, "1.33")
    price_eks_plane(ready_plan, "eks_control_plane", 0.1)
    with pytest.raises(AnalyzerError, match="support tier"):
        validate_deployment_plan(sealed(ready_plan))


def test_aws_ready_rejects_unknown_or_stale_eks_lifecycle(ready_plan, monkeypatch):
    configure_eks(ready_plan, "1.30")
    with pytest.raises(AnalyzerError, match="current lifecycle snapshot"):
        validate_deployment_plan(sealed(ready_plan))
    configure_eks(ready_plan, "1.36")
    monkeypatch.setattr(lifecycle, "OBSERVED_AT", datetime.now(timezone.utc) - timedelta(days=31))
    with pytest.raises(AnalyzerError, match="current lifecycle snapshot"):
        validate_deployment_plan(sealed(ready_plan))
