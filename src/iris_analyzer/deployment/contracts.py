"""Strict, versioned boundary between source analysis, recommendations and IaC.

Validation authorizes compilation into fixed templates only. It never authorizes
deployment, and a successful source analysis is not a capacity measurement.
"""

from __future__ import annotations

import copy
import json
import math
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from iris_analyzer.contracts import AnalyzerError, canonical_bytes, digest, validate_result

from . import lifecycle


def _schema(name: str) -> dict:
    return json.loads(files("iris_analyzer.schemas").joinpath(name).read_text(encoding="utf-8"))


PLANNING_REQUEST_SCHEMA = _schema("planning-request.schema.json")
DEPLOYMENT_PLAN_SCHEMA = _schema("deployment-plan.schema.json")
DEFAULT_PLANNING_REQUEST = {
    "schemaVersion": "iris.planning-request.v1",
    "target": {
        "stack": None,
        "cloud": None,
        "region": None,
        "architecture": None,
        "environment": "test",
    },
    "constraints": {"monthlyBudgetUsd": None, "expectedRps": None, "availability": None},
    "measurements": [],
    "pricingCatalog": None,
    "bindings": {
        "namespace": "iris-preview",
        "clusterName": None,
        "existingClusterContext": None,
        "images": [],
        "imagePlatforms": {},
        "secretRefs": [],
        "ingress": [],
        "databases": [],
        "volumes": [],
        "terraformInputs": None,
        "ingressVerified": False,
        "storageDriverVerified": False,
        "databasesVerified": False,
        "availableCapacity": None,
        "network": None,
        "runtimeVerified": False,
    },
    "overrides": {"instanceType": None, "resources": [], "replicas": []},
}


def _fail(message: str, path: list[Any], code: str = "DEPLOYMENT_PLAN_INVALID") -> None:
    raise AnalyzerError(code, message, {"path": path})


def _validate(document: dict, schema: dict, code: str) -> None:
    canonical_bytes(document)
    error = next(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document), None)
    if error is not None:
        raise AnalyzerError(
            code,
            "JSON does not match the versioned planning contract",
            {"path": list(error.absolute_path), "reason": error.message},
        )


def _unique(items: list[dict], key: str, path: list[Any], code: str) -> None:
    values = [item[key] for item in items]
    if len(values) != len(set(values)):
        _fail("Identifiers must be unique", [*path, key], code)


def _consistent_resources(resources: dict, path: list[Any], code: str) -> None:
    for quantity in ("cpuMillicores", "memoryMiB"):
        if resources["limits"][quantity] < resources["requests"][quantity]:
            _fail("A resource limit cannot be smaller than its request", [*path, "limits", quantity], code)


def validate_planning_request(document: dict) -> dict:
    """Validate and return the original JSON without inserting inferred inputs."""
    code = "PLANNING_REQUEST_INVALID"
    _validate(document, PLANNING_REQUEST_SCHEMA, code)
    measurements = document.get("measurements", [])
    _unique(measurements, "id", ["measurements"], code)
    catalog = document.get("pricingCatalog")
    if catalog is not None:
        _unique(catalog["items"], "key", ["pricingCatalog", "items"], code)
    bindings = document.get("bindings", {})
    _unique(bindings.get("images", []), "serviceId", ["bindings", "images"], code)
    _unique(bindings.get("databases", []), "id", ["bindings", "databases"], code)
    for name in ("secretRefs", "ingress", "volumes"):
        items = bindings.get(name, [])
        discriminator = {"secretRefs": "environmentKey", "ingress": "host", "volumes": "name"}[name]
        pairs = [(item["serviceId"], item[discriminator]) for item in items]
        if len(set(pairs)) != len(pairs):
            _fail("Service bindings must be unique", ["bindings", name], code)
    overrides = document.get("overrides", {})
    for name in ("resources", "replicas"):
        _unique(overrides.get(name, []), "serviceId", ["overrides", name], code)
    for index, resources in enumerate(overrides.get("resources", [])):
        _consistent_resources(resources, ["overrides", "resources", index], code)
    return document


def normalize_planning_request(document: dict | None = None) -> dict:
    """Fill only null/default placeholders; never invent budget or traffic."""
    provided = DEFAULT_PLANNING_REQUEST if document is None else validate_planning_request(document)
    normalized = copy.deepcopy(DEFAULT_PLANNING_REQUEST)
    for key, value in provided.items():
        if key in ("target", "constraints", "bindings", "overrides"):
            normalized[key].update(copy.deepcopy(value))
        else:
            normalized[key] = copy.deepcopy(value)
    return validate_planning_request(normalized)


def plan_digest(plan: dict) -> str:
    """Seal a complete plan; its own digest is excluded from the payload."""
    return digest({key: value for key, value in plan.items() if key != "planDigest"})


def _recommendations(value: Any, path: list[Any] | None = None):
    path = [] if path is None else path
    if isinstance(value, dict):
        if set(value) == {"value", "provenance"}:
            yield path, value
        else:
            for key, item in value.items():
                yield from _recommendations(item, [*path, key])
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _recommendations(item, [*path, index])


def _evidence_ids(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        result.update(value.get("evidenceIds", []))
        for item in value.values():
            result.update(_evidence_ids(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_evidence_ids(item))
    return result


def _validate_provenance(plan: dict, analysis: dict | None) -> None:
    assumptions = {item["id"] for item in plan["assumptions"]}
    measurements = {item["id"]: item for item in plan["request"].get("measurements", [])}
    evidence = _evidence_ids(analysis) if analysis is not None else None
    for path, recommendation in _recommendations(plan):
        provenance = recommendation["provenance"]
        basis = set(provenance["basis"])
        if not set(provenance["assumptionIds"]) <= assumptions:
            _fail("Recommendation references an unknown assumption", [*path, "provenance", "assumptionIds"])
        if "policy" in basis and not provenance["assumptionIds"]:
            _fail("Policy recommendations must disclose their assumptions", [*path, "provenance"])
        if "source" in basis and not provenance["evidenceIds"]:
            _fail("Source provenance requires source evidence references", [*path, "provenance"])
        if evidence is not None and not set(provenance["evidenceIds"]) <= evidence:
            _fail("Recommendation references unknown source evidence", [*path, "provenance", "evidenceIds"])
        if "measurement" in basis and not provenance["measurementIds"]:
            _fail("Measurement provenance requires measured observation references", [*path, "provenance"])
        for identifier in provenance["measurementIds"]:
            measurement = measurements.get(identifier)
            if measurement is None or not measurement["verified"]:
                _fail("Recommendation references an unverified or unknown measurement", [*path, "provenance"])
            if measurement["sourceSnapshotId"] != plan["source"]["sourceSnapshotId"]:
                _fail("Measurement belongs to another source snapshot", [*path, "provenance"])
            if "workloads" in path:
                workload = plan["configuration"]["workloads"][path[path.index("workloads") + 1]]
                if measurement["serviceId"] != workload["serviceId"]:
                    _fail("Measurement belongs to another service", [*path, "provenance"])
            if path[-1] in ("resources", "instance") and (
                measurement["kind"] not in ("runtime", "load")
                or all(
                    measurement["metrics"][metric] is None for metric in ("peakMemoryMiB", "cpuMillicores")
                )
            ):
                _fail(
                    "Capacity requires measured runtime resources, not a passing build or test",
                    [*path, "provenance"],
                )
            if path[-1] == "replicas" and (
                measurement["kind"] != "load"
                or measurement["metrics"]["achievedRps"] is None
                or measurement["conditions"].get("capacityValidated") is not True
            ):
                _fail(
                    "Throughput-based replica sizing requires a reviewed capacity measurement, not offered RPS",
                    [*path, "provenance"],
                )
        if path[-1] in ("resources", "replicas", "instance", "cost") and basis == {"source"}:
            _fail("Source code alone cannot establish capacity or price", [*path, "provenance"])


def _validate_cost(plan: dict) -> None:
    cost = plan["recommendations"]["cost"]["value"]
    total = cost["monthlyTotalUsd"]
    if cost["coverage"] != "complete" and total is not None:
        _fail(
            "A partial estimate cannot be reported as the total deployment cost", ["recommendations", "cost"]
        )
    if cost["coverage"] == "complete":
        catalog = plan["request"].get("pricingCatalog")
        amounts = [item["monthlyUsd"] for item in cost["lineItems"]]
        if catalog is None or not catalog["verified"] or total is None or not amounts or None in amounts:
            _fail(
                "Complete pricing requires a verified catalog and every cost component",
                ["recommendations", "cost"],
            )
        if not math.isclose(total, sum(amounts), rel_tol=1e-6, abs_tol=0.01):
            _fail("Cost total does not match its components", ["recommendations", "cost"])
    _unique(
        cost["lineItems"], "key", ["recommendations", "cost", "value", "lineItems"], "DEPLOYMENT_PLAN_INVALID"
    )
    catalog = plan["request"].get("pricingCatalog")
    target = plan["recommendations"]["target"]["value"]
    for index, item in enumerate(cost["lineItems"]):
        if item["monthlyUsd"] is None:
            continue
        path = ["recommendations", "cost", "value", "lineItems", index]
        if catalog is None or not catalog["verified"]:
            _fail("Priced components require a verified pricing catalog", path)
        verified_at = datetime.fromisoformat(catalog["verifiedAt"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if verified_at > now + timedelta(minutes=5) or now - verified_at > timedelta(days=30):
            _fail("Priced components require pricing verified within the last 30 days", path)
        prices = {price["key"]: price for price in catalog["items"]}
        price = prices.get(item.get("catalogItemKey"))
        quantity = item.get("quantity")
        if price is None or quantity is None:
            _fail("Priced components require a catalog item and an explicit quantity", path)
        if price["cloud"] != target["cloud"] or price["region"] != target["region"]:
            _fail("Pricing must match the selected cloud and region", path)
        if price["key"].startswith("eks_control_plane"):
            terraform = plan["configuration"]["terraformInputs"]
            operating = plan["recommendations"].get("operatingPolicy", {}).get("value", {})
            version = terraform["kubernetesVersion"] if terraform else operating.get("kubernetesVersion")
            support = lifecycle.eks_support(version)
            expected_key = {
                "standard": "eks_control_plane",
                "extended": "eks_control_plane_extended",
            }.get(support)
            if expected_key is None or price["key"] != expected_key:
                _fail("EKS control-plane pricing must match the current Kubernetes support tier", path)
        if not math.isclose(item["monthlyUsd"], price["unitPriceUsd"] * quantity, rel_tol=1e-6, abs_tol=0.01):
            _fail("Cost component does not match its referenced price and quantity", path)
    operating = plan["recommendations"].get("operatingPolicy")
    if operating is not None:
        policy = operating["value"]
        constraints = plan["request"].get("constraints", {})
        if policy["userMonthlyBudgetUsd"] != constraints.get("monthlyBudgetUsd"):
            _fail(
                "A recommended budget cannot replace the user's spending cap",
                ["recommendations", "operatingPolicy"],
            )
        if constraints.get("expectedRps") is not None and policy["expectedRps"] != constraints["expectedRps"]:
            _fail("Explicit traffic requirement must be preserved", ["recommendations", "operatingPolicy"])
        if (
            constraints.get("availability") is not None
            and policy["availability"] != constraints["availability"]
        ):
            _fail(
                "Explicit availability requirement must be preserved", ["recommendations", "operatingPolicy"]
            )
        floor = sum(item["monthlyUsd"] or 0 for item in cost["lineItems"])
        if not math.isclose(policy["knownMonthlyCostFloorUsd"], floor, rel_tol=1e-6, abs_tol=0.01):
            _fail("Known cost floor must match priced components", ["recommendations", "operatingPolicy"])
        if policy["budgetConfidence"] == "measured" and cost["coverage"] != "complete":
            _fail(
                "Incomplete pricing cannot establish a measured operating budget",
                ["recommendations", "operatingPolicy"],
            )
        terraform = plan["configuration"]["terraformInputs"]
        if (
            terraform is not None
            and policy.get("kubernetesVersion") is not None
            and policy["kubernetesVersion"] != terraform["kubernetesVersion"]
        ):
            _fail(
                "Recommended Kubernetes version must match the execution binding",
                ["recommendations", "operatingPolicy", "kubernetesVersion"],
            )


def _validate_workload(workload: dict, index: int, configuration: dict, eligible: bool) -> None:
    path = ["configuration", "workloads", index]
    _consistent_resources(workload["resources"]["value"], [*path, "resources"], "DEPLOYMENT_PLAN_INVALID")
    ports = workload["containerPorts"]
    _unique(ports, "name", [*path, "containerPorts"], "DEPLOYMENT_PLAN_INVALID")
    pairs = [(port["port"], port["protocol"]) for port in ports]
    if len(pairs) != len(set(pairs)):
        _fail("Container ports must be unique", [*path, "containerPorts"])
    service = workload["service"]["value"]
    if service is not None and (service["targetPort"], service["protocol"]) not in pairs:
        _fail("Service targetPort must reference an exposed container port", [*path, "service"])
    probes = workload["probes"]["value"]
    for name, probe in probes.items():
        if probe is not None:
            if (probe["port"], "TCP") not in pairs:
                _fail("Probe must reference a TCP container port", [*path, "probes", name])
            if probe["kind"] == "http" and probe["path"] is None:
                _fail("An HTTP probe requires an explicit path", [*path, "probes", name])
            if probe["kind"] == "tcp" and probe["path"] is not None:
                _fail("A TCP probe must not include an HTTP path", [*path, "probes", name])
            if probe["timeoutSeconds"] > probe["periodSeconds"]:
                _fail("Probe timeout cannot exceed its polling interval", [*path, "probes", name])
    ingress = workload["ingress"]["value"]
    if ingress["enabled"] and service is None:
        _fail("Ingress requires a Kubernetes Service", [*path, "ingress"])
    rollout = workload["rollout"]["value"]
    replicas = workload["replicas"]["value"]
    if rollout["maxSurge"] == 0 and rollout["maxUnavailable"] == 0:
        _fail("RollingUpdate must permit an update to make progress", [*path, "rollout"])
    if rollout["maxUnavailable"] > replicas:
        _fail("Rollout cannot make more replicas unavailable than exist", [*path, "rollout"])
    _unique(workload["volumes"]["value"], "name", [*path, "volumes"], "DEPLOYMENT_PLAN_INVALID")
    _unique(
        workload["secretRefs"]["value"], "environmentKey", [*path, "secretRefs"], "DEPLOYMENT_PLAN_INVALID"
    )
    if not eligible:
        return
    if workload["image"] is None or workload["serviceId"] not in configuration["imagePlatforms"]:
        _fail("Compilation requires an immutable image and its verified CPU platform", [*path, "image"])
    if ports and (probes["readiness"] is None or probes["liveness"] is None):
        _fail("Serving workloads require readiness and liveness probes", [*path, "probes"])
    if ingress["enabled"] and (
        not configuration["ingressVerified"]
        or any(ingress[key] is None for key in ("host", "className", "tlsSecretName"))
    ):
        _fail("Ingress requires verified controller and host/TLS bindings", [*path, "ingress"])
    for volume in workload["volumes"]["value"]:
        if not configuration["storageDriverVerified"] or (
            volume["claimName"] is None and (volume["sizeGiB"] is None or volume["storageClass"] is None)
        ):
            _fail(
                "Volumes require verified storage and a claim or complete provisioning inputs",
                [*path, "volumes"],
            )


def _validate_execution(plan: dict) -> None:
    eligible = plan["executionEligible"]
    if eligible != (plan["status"] == "ready"):
        _fail("Execution eligibility and planning status disagree", ["executionEligible"])
    configuration = plan["configuration"]
    workloads = configuration["workloads"]
    _unique(workloads, "serviceId", ["configuration", "workloads"], "DEPLOYMENT_PLAN_INVALID")
    _unique(workloads, "name", ["configuration", "workloads"], "DEPLOYMENT_PLAN_INVALID")
    for index, workload in enumerate(workloads):
        _validate_workload(workload, index, configuration, eligible)
    if not eligible:
        return
    if not workloads or plan["source"]["analysisStatus"] != "complete":
        _fail("Compilation requires complete source analysis and at least one workload", ["configuration"])
    if not configuration["runtimeVerified"]:
        _fail(
            "Fixed chart security and runtime compatibility must be verified",
            ["configuration", "runtimeVerified"],
        )
    budget = plan["request"].get("constraints", {}).get("monthlyBudgetUsd")
    cost_floor = sum(
        item["monthlyUsd"] or 0 for item in plan["recommendations"]["cost"]["value"]["lineItems"]
    )
    if budget is not None and cost_floor > budget:
        _fail("Known monthly costs exceed the supplied budget cap", ["recommendations", "cost"])
    if any(question["requiredForExecution"] for question in plan["questions"]):
        _fail("Unresolved execution questions block compilation", ["questions"])
    target = plan["recommendations"]["target"]["value"]
    if plan["adapter"] is None or target["stack"] != plan["adapter"]:
        _fail("Compilation requires a supported, matching template adapter", ["adapter"])
    if target["architecture"] is None:
        _fail("Compilation requires an explicit CPU architecture", ["recommendations", "target"])
    platform = {"x86_64": "amd64", "arm64": "arm64"}[target["architecture"]]
    if any(configuration["imagePlatforms"][workload["serviceId"]] != platform for workload in workloads):
        _fail(
            "Image CPU platform does not match the selected architecture", ["configuration", "imagePlatforms"]
        )
    databases = plan["recommendations"]["databases"]["value"]
    if databases and (
        not configuration["databasesVerified"] or any(db["connectionSecretName"] is None for db in databases)
    ):
        _fail(
            "Database connectivity and Secret references must be verified", ["recommendations", "databases"]
        )
    instance = plan["recommendations"]["instance"]["value"]
    if plan["adapter"] == "aws_eks":
        inputs = configuration["terraformInputs"]
        network = plan["recommendations"]["network"]["value"]
        if target["cloud"] != "aws" or target["region"] is None or configuration["clusterName"] is None:
            _fail("EKS requires AWS region and cluster identity", ["recommendations", "target"])
        if instance is None or inputs is None:
            _fail("EKS requires typed instance and Terraform inputs", ["configuration", "terraformInputs"])
        if lifecycle.eks_support(inputs["kubernetesVersion"]) is None:
            _fail(
                "EKS compilation requires a supported version and a current lifecycle snapshot",
                ["configuration", "terraformInputs", "kubernetesVersion"],
            )
        if not inputs["privateNetworkEgressVerified"] or not inputs["executorPrivateApiReachable"]:
            _fail(
                "EKS private networking prerequisites must be verified", ["configuration", "terraformInputs"]
            )
        if not instance["minNodes"] <= inputs["desiredNodes"] <= instance["maxNodes"]:
            _fail(
                "Desired nodes must be within the recommended node-group bounds",
                ["configuration", "terraformInputs"],
            )
        if network["vpcMode"] == "existing" and (
            network["vpcId"] is None or len(set(network["subnetIds"])) < 2
        ):
            _fail(
                "Existing EKS networking requires VPC and two private subnets", ["recommendations", "network"]
            )
        if len(set(network["availabilityZones"])) < 2:
            _fail("EKS requires two distinct availability zones", ["recommendations", "network"])
        capacity = {
            "cpuMillicores": instance["cpuMillicores"] * inputs["desiredNodes"] * 0.8,
            "memoryMiB": instance["memoryMiB"] * inputs["desiredNodes"] * 0.8,
        }
        for index, workload in enumerate(workloads):
            for quantity in ("cpuMillicores", "memoryMiB"):
                if workload["resources"]["value"]["limits"][quantity] > instance[quantity] * 0.8:
                    _fail(
                        "A workload limit exceeds usable single-node capacity",
                        ["configuration", "workloads", index, "resources", "limits", quantity],
                    )
    else:
        if configuration["existingClusterContext"] is None or configuration["availableCapacity"] is None:
            _fail(
                "Existing Kubernetes requires a selected cluster and verified allocatable capacity",
                ["configuration"],
            )
        capacity = configuration["availableCapacity"]
    for quantity in ("cpuMillicores", "memoryMiB"):
        required = sum(
            workload["resources"]["value"]["requests"][quantity]
            * (workload["replicas"]["value"] + workload["rollout"]["value"]["maxSurge"])
            for workload in workloads
        )
        if required > capacity[quantity]:
            _fail(
                "Available capacity cannot accommodate requests during rolling updates",
                ["configuration", quantity],
            )


def _validate_request_preservation(plan: dict) -> None:
    request = plan["request"]
    target = plan["recommendations"]["target"]["value"]
    for key, value in request["target"].items():
        if value is not None and value != target[key]:
            _fail(
                "An explicit requested target must not be silently replaced",
                ["recommendations", "target", key],
            )
    bindings = request.get("bindings", {})
    configuration = plan["configuration"]
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
    ):
        if key in bindings and configuration[key] != bindings[key]:
            _fail("Execution bindings must preserve the supplied inputs", ["configuration", key])
    if (
        bindings.get("network") is not None
        and plan["recommendations"]["network"]["value"] != bindings["network"]
    ):
        _fail("Execution network binding differs from the request", ["recommendations", "network"])
    images = {item["serviceId"]: item["reference"] for item in bindings.get("images", [])}
    for index, workload in enumerate(configuration["workloads"]):
        if workload["serviceId"] in images and workload["image"] != images[workload["serviceId"]]:
            _fail(
                "Immutable image binding differs from the request",
                ["configuration", "workloads", index, "image"],
            )
        for collection in ("secretRefs", "volumes", "ingress"):
            provided = [
                {key: value for key, value in item.items() if key != "serviceId"}
                for item in bindings.get(collection, [])
                if item["serviceId"] == workload["serviceId"]
            ]
            if not provided:
                continue
            actual = workload[collection]["value"]
            expected = provided[0] if collection == "ingress" else provided
            if actual != expected:
                _fail(
                    "Explicit workload bindings must be preserved",
                    ["configuration", "workloads", index, collection],
                )
    databases = {item["id"]: item for item in plan["recommendations"]["databases"]["value"]}
    for database in bindings.get("databases", []):
        if databases.get(database["id"]) != database:
            _fail("Explicit database binding must be preserved", ["recommendations", "databases"])
    overrides = request.get("overrides", {})
    instance = plan["recommendations"]["instance"]["value"]
    if (
        overrides.get("instanceType") is not None
        and instance is not None
        and instance["instanceType"] != overrides["instanceType"]
    ):
        _fail("Requested instance override was ignored", ["recommendations", "instance"])
    workloads = {workload["serviceId"]: workload for workload in configuration["workloads"]}
    for override in overrides.get("resources", []):
        workload = workloads.get(override["serviceId"])
        expected = {key: override[key] for key in ("requests", "limits")}
        if workload is None or workload["resources"]["value"] != expected:
            _fail("Requested resource override was ignored", ["request", "overrides", "resources"])
    for override in overrides.get("replicas", []):
        workload = workloads.get(override["serviceId"])
        if workload is None or workload["replicas"]["value"] != override["replicas"]:
            _fail("Requested replica override was ignored", ["request", "overrides", "replicas"])


def validate_deployment_plan(plan: dict, *, analysis: dict | None = None) -> dict:
    """Validate schema, immutable seals, provenance and compilation prerequisites.

    Supply the original analysis when crossing a process boundary to verify its
    digest and evidence references as well as the self-contained plan contract.
    """
    _validate(plan, DEPLOYMENT_PLAN_SCHEMA, "DEPLOYMENT_PLAN_INVALID")
    validate_planning_request(plan["request"])
    if plan["planDigest"] != plan_digest(plan):
        _fail("Plan content differs from its immutable digest", ["planDigest"])
    if plan["requestDigest"] != digest(plan["request"]):
        _fail("Plan request differs from its immutable digest", ["requestDigest"])
    if analysis is not None:
        validate_result(analysis)
        if digest(analysis) != plan["analysisDigest"] or any(
            plan["source"][plan_key] != analysis[analysis_key]
            for plan_key, analysis_key in (
                ("sourceSnapshotId", "sourceSnapshotId"),
                ("contextHash", "contextHash"),
                ("analysisStatus", "status"),
            )
        ):
            _fail("Plan source does not match the immutable analysis result", ["source"])
        services = {service["serviceId"] for service in analysis["services"]}
        workload_ids = {workload["serviceId"] for workload in plan["configuration"]["workloads"]}
        if not workload_ids <= services:
            _fail("Plan workload references an unknown source service", ["configuration", "workloads"])
        if plan["executionEligible"] and workload_ids != services:
            _fail(
                "Compilation must account for every analyzed application service",
                ["configuration", "workloads"],
            )
        source_services = {service["serviceId"]: service for service in analysis["services"]}
        for index, workload in enumerate(plan["configuration"]["workloads"]):
            source_ports = {
                int(port["value"])
                for port in source_services[workload["serviceId"]]["ports"]
                if port["status"] == "detected"
                and port["scope"] in ("container", "production")
                and (type(port["value"]) is int or isinstance(port["value"], str) and port["value"].isdigit())
            }
            if not {port["port"] for port in workload["containerPorts"]} <= source_ports:
                _fail(
                    "Deployment ports must match confirmed container or production listeners",
                    ["configuration", "workloads", index, "containerPorts"],
                )
    _unique(plan["assumptions"], "id", ["assumptions"], "DEPLOYMENT_PLAN_INVALID")
    _unique(plan["questions"], "id", ["questions"], "DEPLOYMENT_PLAN_INVALID")
    instance = plan["recommendations"]["instance"]["value"]
    if instance is not None and instance["minNodes"] > instance["maxNodes"]:
        _fail("Node-group minimum cannot exceed maximum", ["recommendations", "instance"])
    _validate_provenance(plan, analysis)
    _validate_cost(plan)
    _validate_request_preservation(plan)
    _validate_execution(plan)
    return plan
