"""Deterministic planning around optional, validated AI policy proposals.

No recommendation is promoted into a source observation. Images, account/network
bindings and observed runtime compatibility remain the executor's responsibility.
"""

from __future__ import annotations

import copy
import json
import math
import re
from datetime import datetime, timezone
from importlib.resources import files

from ..contracts import AnalyzerError, digest, validate_result
from .contracts import normalize_planning_request, plan_digest, validate_deployment_plan
from .lifecycle import eks_support

INSTANCE_SPECS = [
    ("t3.medium", "x86_64", 2000, 4096, 400),
    ("t3.large", "x86_64", 2000, 8192, 600),
    ("m6i.large", "x86_64", 2000, 8192, 2000),
    ("m6i.xlarge", "x86_64", 4000, 16384, 4000),
    ("m6i.2xlarge", "x86_64", 8000, 32768, 8000),
    ("t4g.medium", "arm64", 2000, 4096, 400),
    ("t4g.large", "arm64", 2000, 8192, 600),
    ("m6g.large", "arm64", 2000, 8192, 2000),
    ("m6g.xlarge", "arm64", 4000, 16384, 4000),
    ("m6g.2xlarge", "arm64", 8000, 32768, 8000),
]


def default_pricing_catalog() -> dict:
    """Reviewed snapshot, not a live tariff lookup; stale/mismatched prices ignored."""
    return json.loads(files("iris_analyzer.deployment").joinpath("catalogs/aws-seoul.json").read_text())


def prepare_planning_request(document: dict | None = None) -> dict:
    request = normalize_planning_request(document)
    if document is None or "pricingCatalog" not in document:
        request["pricingCatalog"] = default_pricing_catalog()
    return request


def _name(value: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", value.lower()).strip("-")[:50] or "app"


def _round(value: float, quantum: int) -> int:
    return max(quantum, math.ceil(value / quantum) * quantum)


def usable_measurements(
    analysis: dict, request: dict, service_id: str, *, architecture: str | None = None
) -> list[dict]:
    """Build/test and idle measurements never establish production serving capacity."""
    now = datetime.now(timezone.utc)
    records = []
    for item in request["measurements"]:
        if not (
            item["verified"]
            and item["kind"] == "load"
            and item["serviceId"] == service_id
            and item["sourceSnapshotId"] == analysis["sourceSnapshotId"]
        ):
            continue
        measured = datetime.fromisoformat(item["measuredAt"].replace("Z", "+00:00"))
        if not measured.tzinfo or not 0 <= (now - measured).total_seconds() <= 7 * 86400:
            continue
        metrics, conditions = item["metrics"], item["conditions"]
        wanted_arch = architecture or request["target"].get("architecture")
        if wanted_arch and conditions.get("architecture") and conditions["architecture"] != wanted_arch:
            continue
        if (conditions["durationSeconds"] or 0) < 60 or not conditions["concurrency"]:
            continue
        if any(
            metrics[k] is None
            for k in ("peakMemoryMiB", "cpuMillicores", "achievedRps", "p95LatencyMs", "errorRate")
        ):
            continue
        if metrics["errorRate"] > 0.01:
            continue
        records.append(item)
    return records


def create_deployment_plan(
    analysis: dict,
    request: dict | None = None,
    *,
    readiness: dict | None = None,
    policy_proposal: dict | None = None,
) -> dict:
    validate_result(analysis)
    request = prepare_planning_request(request)
    if policy_proposal is not None:
        from .advisor import validate_advice

        validate_advice(policy_proposal, analysis, request)
    advice = policy_proposal or {}
    assumptions = [
        {
            "id": "planning-baseline",
            "description": "Initial operating policy, not a source fact or measured capacity.",
            "impact": "Reassess region, availability, load, resource headroom and costs before execution.",
        }
    ]
    questions = []

    def assume(identifier, description, impact):
        if not any(a["id"] == identifier for a in assumptions):
            assumptions.append({"id": identifier, "description": description, "impact": impact})
        return identifier

    def question(identifier, field, reason, required=True):
        if not any(q["id"] == identifier for q in questions):
            questions.append(
                {"id": identifier, "field": field, "reason": reason, "requiredForExecution": required}
            )

    def rec(value, reason, *, basis=None, evidence=(), measurements=(), assumption="planning-baseline"):
        selected = list(basis or ["policy"])
        return {
            "value": value,
            "provenance": {
                "basis": selected,
                "evidenceIds": sorted(set(evidence)),
                "measurementIds": sorted(set(measurements)),
                "assumptionIds": [assumption] if "policy" in selected else [],
                "reason": reason,
            },
        }

    target_in = request["target"]
    target = {
        "stack": target_in.get("stack") or advice.get("target", {}).get("stack") or "aws_eks",
        "cloud": target_in.get("cloud") or advice.get("target", {}).get("cloud") or "aws",
        "region": target_in.get("region") or advice.get("target", {}).get("region") or "ap-northeast-2",
        "architecture": target_in.get("architecture")
        or advice.get("target", {}).get("architecture")
        or "x86_64",
        "environment": target_in["environment"],
    }
    target_assumption = assume(
        "target-defaults",
        f"Initial proposed placement: {target['cloud']}/{target['region']}/{target['stack']}/{target['architecture']}. "
        + (
            advice.get("reason")
            or "Unspecified placement follows the team's AWS/Kubernetes direction and compatibility-first architecture."
        ),
        "Region latency/compliance and image platform still need confirmation; supplied target overrides win.",
    )
    adapter = target["stack"] if target["stack"] in {"aws_eks", "existing_kubernetes"} else None
    if adapter == "aws_eks" and target["cloud"] != "aws":
        adapter = None
    if adapter is None:
        question(
            "adapter",
            "target.stack",
            "Requested stack/cloud has no controlled compiler adapter; request preserved.",
        )
    constraints, bindings, overrides = request["constraints"], request["bindings"], request["overrides"]
    availability = (
        constraints.get("availability")
        or advice.get("availability")
        or ("multi_az" if target["environment"] == "production" else "single_az")
    )
    expected_rps = constraints.get("expectedRps") or advice.get("expectedRps") or 20
    assume(
        "load-scenario",
        f"{expected_rps:g} RPS is an initial test scenario when traffic is unspecified, not a predicted audience.",
        "A successful test does not validate future traffic, payload sizes, dependencies or availability.",
    )
    assume(
        "availability",
        f"{availability} availability policy for the initial {target['environment']} plan.",
        "Single node has downtime; replicas alone do not prove multi-AZ availability.",
    )
    if constraints.get("expectedRps") is None:
        question(
            "traffic",
            "constraints.expectedRps",
            "Replace the initial traffic scenario with observed/business demand.",
            False,
        )
    if constraints.get("monthlyBudgetUsd") is None:
        question(
            "budget",
            "constraints.monthlyBudgetUsd",
            "AI proposes a cost envelope, not an authorized monthly spending limit.",
            False,
        )

    images = {i["serviceId"]: i["reference"] for i in bindings["images"]}
    resource_overrides = {i["serviceId"]: i for i in overrides["resources"]}
    replica_overrides = {i["serviceId"]: i["replicas"] for i in overrides["replicas"]}
    ai_resources = {i["serviceId"]: i for i in advice.get("workloads", [])}
    workloads, disks = [], []
    for service in analysis["services"]:
        sid = service["serviceId"]
        measurement = usable_measurements(analysis, request, sid, architecture=target["architecture"])
        replicas = replica_overrides.get(sid, 2 if availability == "multi_az" else 1)
        capacity_samples = [m for m in measurement if m["conditions"].get("capacityValidated") is True]
        if capacity_samples and sid not in replica_overrides:
            calibrated = min(m["metrics"]["achievedRps"] for m in capacity_samples)
            replicas = max(replicas, math.ceil(expected_rps / (calibrated * 0.7)))
        elif measurement:
            offered = max(m["metrics"]["achievedRps"] for m in measurement)
            question(
                "offered-load-" + sid,
                "measurements",
                "Observed RPS is a tested offered-load scenario, not maximum sustainable capacity. Replica count follows availability until a capacity sweep is validated.",
                False,
            )
            if expected_rps > offered * replicas * 1.05:
                question(
                    "load-range-" + sid,
                    "constraints.expectedRps",
                    "Requested load exceeds the measured scenario; rerun representative load instead of extrapolating CPU or treating observed RPS as maximum capacity.",
                    constraints.get("expectedRps") is not None,
                )
        base_cpu, base_memory = (100, 128) if service["role"]["value"] == "static" else (250, 512)
        mids, resource_basis = [], ["policy"]
        if sid in resource_overrides:
            resources = {k: copy.deepcopy(resource_overrides[sid][k]) for k in ("requests", "limits")}
            resource_basis = ["user"]
            why = "Explicit user resource override; no capacity guarantee implied."
        else:
            if measurement:
                mids = [m["id"] for m in measurement]
                peak = max(m["metrics"]["peakMemoryMiB"] for m in measurement)
                cpu = max(m["metrics"]["cpuMillicores"] for m in measurement)
                capacity = min(m["metrics"]["achievedRps"] for m in measurement)
                base_memory = _round(peak * 1.5, 64)
                base_cpu = _round(
                    cpu * (max(1, expected_rps / replicas / capacity) if capacity_samples else 1) * 1.5, 50
                )
                resource_basis = ["measurement", "policy"]
                why = "Same-snapshot sustained load measurements with 50% CPU/memory headroom; valid only for tested local container scenario/hardware/limits. Production instance performance requires retest."
            elif sid in ai_resources:
                base_cpu, base_memory = ai_resources[sid]["cpuMillicores"], ai_resources[sid]["memoryMiB"]
                why = "AI-proposed initial resource envelope, unmeasured: " + ai_resources[sid]["reason"]
            else:
                why = "Conservative initial profile for a static server or Node service; source cannot establish needed capacity."
            resources = {
                "requests": {"cpuMillicores": base_cpu, "memoryMiB": base_memory},
                "limits": {"cpuMillicores": max(500, base_cpu * 2), "memoryMiB": base_memory * 2},
            }
        rid = assume(
            "resources-" + sid,
            why,
            "Measure latency, error rate, memory and CPU under realistic load; do not infer adequacy from unit tests.",
        )
        if not measurement:
            question(
                "measure-" + sid,
                "measurements",
                "No eligible sustained load sample for this snapshot/service; sizing remains an assumption.",
                False,
            )
        candidates = [
            p
            for p in service["ports"]
            if p["status"] == "detected" and p["scope"] in {"container", "production"}
        ]
        ports = sorted({int(p["value"]) for p in candidates})
        port = ports[0] if len(ports) == 1 else None
        if port is None:
            question(
                "port-" + sid,
                "configuration.workloads.containerPorts",
                "No single confirmed container/production listener; do not use development or host ports.",
            )
        probe = None
        probe_evidence = []
        if port:
            health = [
                h
                for h in service["healthchecks"]
                if h["status"] == "detected" and isinstance(h["value"], str) and h["value"].startswith("/")
            ]

            def mk(path, initial, threshold):
                return {
                    "kind": "http" if path else "tcp",
                    "port": port,
                    "path": path,
                    "initialDelaySeconds": initial,
                    "periodSeconds": 10,
                    "timeoutSeconds": 2,
                    "failureThreshold": threshold,
                }

            live = next((h for h in health if "live" in h["value"]), None)
            ready = next((h for h in health if "ready" in h["value"]), None)
            probe = {
                "readiness": mk(ready["value"] if ready else None, 5, 3),
                "liveness": mk(live["value"] if live else None, 15, 3),
                "startup": mk(live["value"] if live else None, 0, 30),
            }
            probe_evidence = [eid for h in (live, ready) if h for eid in h["evidenceIds"]]
        ingress = next((copy.deepcopy(x) for x in bindings["ingress"] if x["serviceId"] == sid), None)
        if ingress:
            del ingress["serviceId"]
        else:
            ingress = {"enabled": False, "host": None, "className": None, "tlsSecretName": None, "path": "/"}
            question(
                "ingress-" + sid,
                "bindings.ingress",
                "Initial service is ClusterIP only; provide domain, controller and TLS to expose it.",
                False,
            )
        volumes = [
            {k: copy.deepcopy(v) for k, v in item.items() if k != "serviceId"}
            for item in bindings["volumes"]
            if item["serviceId"] == sid
        ]
        for dependency in analysis["dependencies"]:
            value = dependency["value"]
            if (
                not isinstance(value, dict)
                or value.get("kind") != "volume"
                or value.get("component") not in service["componentRoots"]
            ):
                continue
            mount = value.get("mountPath")
            if not mount or any(v["mountPath"] == mount for v in volumes):
                continue
            if value.get("type") == "bind":
                question(
                    "bind-" + sid + "-" + _name(mount),
                    "bindings.volumes",
                    "Source bind mount requires an explicit PVC/ConfigMap/Secret migration; host paths are not copied automatically.",
                )
                continue
            name = _name(value.get("name", "data"))
            volumes.append(
                {
                    "name": name,
                    "mountPath": mount,
                    "claimName": None,
                    "sizeGiB": 10,
                    "storageClass": None,
                    "accessMode": "ReadWriteOnce" if replicas == 1 else "ReadWriteMany",
                }
            )
            disks.append(
                {
                    "name": name,
                    "serviceIds": [sid],
                    "sizeGiB": 10,
                    "storageClass": None,
                    "accessMode": volumes[-1]["accessMode"],
                }
            )
        refs = [
            {k: v for k, v in item.items() if k != "serviceId"}
            for item in bindings["secretRefs"]
            if item["serviceId"] == sid
        ]
        secret_keys = {x["environmentKey"] for x in refs}
        for item in analysis["environmentKeys"]:
            key = item["value"]
            if (
                isinstance(key, str)
                and re.search(r"SECRET|PASSWORD|TOKEN|API_KEY|MONGO_URI|DATABASE_URL", key)
                and key not in secret_keys
            ):
                question(
                    "secret-" + sid + "-" + key,
                    "bindings.secretRefs",
                    "Confirm runtime requirement and supply external Secret reference for observed key "
                    + key
                    + "; values never belong in a plan.",
                )
        for volume in volumes:
            if not bindings["storageDriverVerified"] or not (volume["claimName"] or volume["storageClass"]):
                question(
                    "volume-" + sid + "-" + volume["name"],
                    "bindings.volumes",
                    "Verify storage driver/class or existing claim before compiling mounts.",
                )
        if not images.get(sid):
            question(
                "image-" + sid,
                "bindings.images",
                "Build and provide an immutable image digest; analyzer does not build or guess registry credentials.",
            )
        required_platform = "arm64" if target["architecture"] == "arm64" else "amd64"
        if bindings["imagePlatforms"].get(sid) != required_platform:
            question(
                "platform-" + sid,
                "bindings.imagePlatforms",
                "Verify the image platform matches the selected node architecture.",
            )
        rwo = any(v["accessMode"] == "ReadWriteOnce" for v in volumes)
        workloads.append(
            {
                "serviceId": sid,
                "name": _name(sid),
                "image": images.get(sid),
                "containerPorts": [
                    {"name": "http" if i == 0 else "port-" + str(p), "port": p, "protocol": "TCP"}
                    for i, p in enumerate(ports)
                ],
                "command": None,
                "args": None,
                "resources": rec(resources, why, basis=resource_basis, measurements=mids, assumption=rid),
                "replicas": rec(
                    replicas,
                    "Explicit override or availability/throughput policy; does not claim verified HA.",
                    basis=["user"]
                    if sid in replica_overrides
                    else ["measurement", "policy"]
                    if capacity_samples
                    else ["policy"],
                    measurements=[m["id"] for m in capacity_samples] if sid not in replica_overrides else [],
                    assumption="availability",
                ),
                "service": rec(
                    {"type": "ClusterIP", "port": 80, "targetPort": port, "protocol": "TCP"}
                    if port
                    else None,
                    "Cluster-private Service targets confirmed serving listener.",
                    evidence=[e for p in candidates for e in p["evidenceIds"]],
                ),
                "ingress": rec(
                    ingress,
                    "Explicit ingress binding or private-only initial profile.",
                    basis=["user"] if ingress["enabled"] else ["policy"],
                ),
                "probes": rec(
                    probe or {"readiness": None, "liveness": None, "startup": None},
                    "Detected health paths where available; otherwise TCP is an assumed socket check, not app correctness.",
                    evidence=probe_evidence,
                ),
                "volumes": rec(
                    volumes,
                    "Explicit claims or provisional data disk sizes; source host bind mounts require migration.",
                ),
                "secretRefs": rec(
                    refs,
                    "External Secret names/keys only; credentials are never emitted.",
                    basis=["user"] if refs else ["policy"],
                ),
                "rollout": rec(
                    {
                        "strategy": "RollingUpdate",
                        "maxSurge": 0 if rwo else 1,
                        "maxUnavailable": 1 if rwo else 0,
                        "progressDeadlineSeconds": 600,
                    },
                    "RWO single-writer storage uses no surge and may incur downtime; otherwise reserve one surge pod.",
                ),
                "rollback": rec(
                    {"strategy": "helm_atomic", "timeoutSeconds": 600, "revisionHistoryLimit": 10},
                    "Atomic Helm rollback requires executor flags; data/schema rollback is separate.",
                ),
            }
        )
    if not bindings["runtimeVerified"]:
        question(
            "runtime",
            "bindings.runtimeVerified",
            "Verify immutable images with the compiler's restricted security context and supplied configuration.",
        )
    if analysis["status"] != "complete":
        question(
            "analysis",
            "source.analysisStatus",
            "Resolve incomplete/unsupported source analysis before execution.",
        )
    if readiness and any(f["severity"] == "error" for f in readiness["findings"]):
        question(
            "readiness",
            "sourceReadiness.findings",
            "Resolve reported initial source errors; this is not a full correctness/security audit.",
        )
    if not workloads:
        question("workloads", "configuration.workloads", "No supported deployment workload was discovered.")

    database_engines = sorted(
        {
            x["value"].get("engine")
            for x in analysis["dependencies"]
            if isinstance(x["value"], dict)
            and x["value"].get("kind") == "database"
            and x["value"].get("engine")
        }
    )
    databases = copy.deepcopy(bindings["databases"])
    for engine in database_engines:
        if not any(d["engine"] == engine for d in databases):
            databases.append(
                {
                    "id": engine,
                    "serviceIds": [s["serviceId"] for s in analysis["services"]],
                    "engine": engine,
                    "version": None,
                    "mode": "existing",
                    "name": engine,
                    "storageGiB": None,
                    "connectionSecretName": None,
                }
            )
    if databases and (
        not bindings["databasesVerified"] or any(not d["connectionSecretName"] for d in databases)
    ):
        question(
            "databases",
            "bindings.databases",
            "Verify actual external DB provisioning/connectivity/credentials. MongoDB is not assumed compatible with DocumentDB; no DB is created by this template.",
        )
    network = copy.deepcopy(bindings.get("network")) or {
        "vpcMode": "existing",
        "vpcId": None,
        "subnetIds": [],
        "availabilityZones": [],
        "natGatewayCount": 0,
        "publicIngress": any(w["ingress"]["value"]["enabled"] for w in workloads),
    }
    if adapter == "aws_eks" and (
        not network["vpcId"] or len(network["subnetIds"]) < 2 or len(network["availabilityZones"]) < 2
    ):
        question(
            "network",
            "bindings.network",
            "EKS compiler requires an existing verified VPC and private subnets in at least two availability zones.",
        )
    if adapter == "aws_eks" and (not bindings["clusterName"] or not bindings["terraformInputs"]):
        question(
            "terraform",
            "bindings.terraformInputs",
            "Supply cluster name, supported Kubernetes version, administrator IAM role, node disk/nodes and verified private connectivity.",
        )
    if adapter == "existing_kubernetes" and (
        not bindings["existingClusterContext"] or not bindings["availableCapacity"]
    ):
        question(
            "cluster",
            "bindings.availableCapacity",
            "Supply existing cluster context and measured allocatable capacity; no cloud cost is assumed zero.",
        )

    minimum_nodes = 2 if availability == "multi_az" else 1
    desired_nodes = (bindings["terraformInputs"] or {}).get("desiredNodes", minimum_nodes)
    if desired_nodes < minimum_nodes:
        question(
            "availability-nodes",
            "bindings.terraformInputs.desiredNodes",
            "Supplied node count is below proposed availability policy; preserve input and request correction.",
        )
    required_cpu = sum(
        w["resources"]["value"]["requests"]["cpuMillicores"]
        * (w["replicas"]["value"] + w["rollout"]["value"]["maxSurge"])
        for w in workloads
    )
    required_memory = sum(
        w["resources"]["value"]["requests"]["memoryMiB"]
        * (w["replicas"]["value"] + w["rollout"]["value"]["maxSurge"])
        for w in workloads
    )
    steady_cpu = sum(
        w["resources"]["value"]["requests"]["cpuMillicores"] * w["replicas"]["value"] for w in workloads
    )
    candidates = [
        i
        for i in INSTANCE_SPECS
        if i[1] == target["architecture"]
        and i[2] * 0.8 * desired_nodes >= required_cpu
        and i[3] * 0.8 * desired_nodes >= required_memory
        and i[4] * desired_nodes >= steady_cpu
        and max((w["resources"]["value"]["limits"]["memoryMiB"] for w in workloads), default=0) <= i[3] * 0.8
        and max((w["resources"]["value"]["limits"]["cpuMillicores"] for w in workloads), default=0)
        <= i[2] * 0.8
    ]
    explicit = overrides.get("instanceType")
    preferred = explicit or advice.get("instanceType")
    selected = next((i for i in candidates if i[0] == preferred), None) if preferred else None
    if preferred and selected is None:
        question(
            "instance-override",
            "overrides.instanceType",
            "Requested instance is unsupported, wrong architecture or below surge/steady CPU/memory policy; no silent replacement.",
            bool(explicit),
        )
    selected = selected or (candidates[0] if candidates and not explicit else None)
    instance = (
        {
            "instanceType": selected[0],
            "cpuMillicores": selected[2],
            "memoryMiB": selected[3],
            "minNodes": minimum_nodes,
            "maxNodes": max(desired_nodes, minimum_nodes * 3),
        }
        if selected and adapter == "aws_eks"
        else None
    )
    if adapter == "aws_eks" and instance is None:
        question(
            "capacity",
            "recommendations.instance",
            "No supported instance fits requested resource envelope; extend verified catalog or change constraints.",
        )
    catalog = request["pricingCatalog"]
    price_items = []
    if catalog and catalog["verified"]:
        verified_at = datetime.fromisoformat(catalog["verifiedAt"].replace("Z", "+00:00"))
        if (
            verified_at.tzinfo
            and 0 <= (datetime.now(timezone.utc) - verified_at).total_seconds() <= 30 * 86400
        ):
            price_items = [
                p
                for p in catalog["items"]
                if p["cloud"] == target["cloud"] and p["region"] == target["region"]
            ]

    def priced(key, quantity, unit):
        row = next((p for p in price_items if p["key"] == key and p["unit"] == unit), None)
        return round(row["unitPriceUsd"] * quantity, 4) if row else None

    node_disk = (bindings["terraformInputs"] or {}).get("nodeDiskGiB", 30)
    kubernetes_version = (bindings["terraformInputs"] or {}).get("kubernetesVersion", "1.36")
    support = eks_support(kubernetes_version)
    plane_key = (
        "eks_control_plane"
        if support == "standard"
        else "eks_control_plane_extended"
        if support == "extended"
        else None
    )
    if adapter == "aws_eks" and support is None:
        question(
            "kubernetes-version",
            "bindings.terraformInputs.kubernetesVersion",
            "Unsupported or stale EKS lifecycle snapshot; refresh support data before quoting/compiling.",
        )
    line_items = []
    if adapter == "aws_eks":
        line_items = [
            {
                "key": "control_plane",
                "catalogItemKey": plane_key,
                "quantity": 730,
                "monthlyUsd": priced(plane_key, 730, "hour") if plane_key else None,
                "reason": f"730-hour EKS {kubernetes_version} {support or 'unknown'} support month; lifecycle and pricing must remain current.",
            },
            {
                "key": "workers",
                "catalogItemKey": "ec2:" + instance["instanceType"] if instance else None,
                "quantity": 730 * desired_nodes,
                "monthlyUsd": priced("ec2:" + instance["instanceType"], 730 * desired_nodes, "hour")
                if instance
                else None,
                "reason": "Linux shared On-Demand worker count, not reserved/spot pricing; burst credits may cost extra.",
            },
            {
                "key": "node_disk",
                "catalogItemKey": "ebs_gp3",
                "quantity": node_disk * desired_nodes,
                "monthlyUsd": priced("ebs_gp3", node_disk * desired_nodes, "gb_month"),
                "reason": "Encrypted gp3 base capacity; additional IOPS/throughput excluded.",
            },
        ]
    line_items += [
        {"key": k, "monthlyUsd": None, "reason": reason}
        for k, reason in [
            (
                "network",
                "NAT or endpoints, load balancing, public IPv4 and egress depend on actual network/traffic.",
            ),
            (
                "data",
                "DB, application volumes, backup, logs and registry are not priced by this initial catalog.",
            ),
        ]
    ]
    coverage = "partial" if any(i["monthlyUsd"] is not None for i in line_items) else "unknown"
    known_cost = sum(i["monthlyUsd"] or 0 for i in line_items)
    cost = {"currency": "USD", "monthlyTotalUsd": None, "coverage": coverage, "lineItems": line_items}
    assume(
        "cost-model",
        "730-hour monthly quote, unpriced network/data costs remain unknown.",
        "Known line-item subtotal is a lower bound, not total cost or an approved budget.",
    )
    budget = constraints.get("monthlyBudgetUsd")
    if budget and known_cost > budget:
        question(
            "budget-exceeded",
            "constraints.monthlyBudgetUsd",
            f"Known monthly subtotal USD {known_cost:.2f} already exceeds cap USD {budget:.2f}; use existing cluster or another adapter.",
        )
    if budget and coverage != "complete":
        question(
            "budget-uncertain",
            "recommendations.cost",
            "Unpriced costs prevent verifying the supplied spending cap.",
        )
    configuration = {
        key: copy.deepcopy(bindings[key])
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
    configuration["workloads"] = workloads
    plan = {
        "schemaVersion": "iris.deployment-plan.v1",
        "planDigest": "",
        "requestDigest": digest(request),
        "analysisDigest": digest(analysis),
        "source": {
            "sourceSnapshotId": analysis["sourceSnapshotId"],
            "contextHash": analysis["contextHash"],
            "analysisStatus": analysis["status"],
        },
        "request": request,
        "adapter": adapter,
        "status": "unsupported" if adapter is None else "needs_input",
        "plannerMode": "ai" if policy_proposal else "policy",
        "executionEligible": False,
        "deploymentAuthorized": False,
        "assumptions": assumptions,
        "questions": questions,
        "recommendations": {
            "target": rec(
                target,
                "Requested target takes precedence; unspecified placement is a proposal.",
                assumption=target_assumption,
            ),
            "instance": rec(
                instance,
                "Choose catalog capacity for surge, node overhead and sustained CPU baseline; instance fit is not throughput proof.",
            ),
            "cost": rec(
                cost, "Verified regional rates where available; no invented total.", assumption="cost-model"
            ),
            "operatingPolicy": rec(
                {
                    "expectedRps": expected_rps,
                    "availability": availability,
                    "kubernetesVersion": kubernetes_version if adapter == "aws_eks" else None,
                    "userMonthlyBudgetUsd": budget,
                    "suggestedMonthlyBudgetUsd": math.ceil(known_cost * 2 / 10) * 10 if known_cost else None,
                    "budgetConfidence": "provisional",
                    "knownMonthlyCostFloorUsd": round(known_cost, 4),
                },
                "Initial load/availability scenario; proposed budget is a 2x known-cost reserve rounded to USD10, "
                "not authorization or proof that unpriced network/DB/backup costs fit.",
                assumption="cost-model",
            ),
            "network": rec(
                network,
                "Existing private network inputs required by fixed EKS template; VPC creation is a separate module.",
                basis=["user"] if bindings.get("network") else ["policy"],
            ),
            "databases": rec(
                databases,
                "Observed engines require explicitly provisioned external databases; engine compatibility and credentials are not inferred.",
            ),
            "storage": rec(
                disks, "Initial data-volume envelope; provisioned claims and storage driver must be verified."
            ),
            "observability": rec(
                {"prometheusEnabled": False, "scrapeIntervalSeconds": 30},
                "Monitoring is left to platform modules; this request concerns Kubernetes.",
            ),
        },
        "configuration": configuration,
    }
    plan["planDigest"] = plan_digest(plan)
    validate_deployment_plan(plan, analysis=analysis)
    if adapter and not any(q["requiredForExecution"] for q in questions):
        plan["status"], plan["executionEligible"] = "ready", True
        plan["planDigest"] = plan_digest(plan)
        try:
            validate_deployment_plan(plan, analysis=analysis)
            from .render import compile_plan

            compiled = compile_plan(plan, analysis=analysis)
            for index, error in enumerate(compiled["blockedReasons"]):
                question("compile-" + str(index), error["path"], error["message"])
        except AnalyzerError as error:
            question("eligibility", ".".join(map(str, error.details.get("path", []))), error.message)
        if any(q["requiredForExecution"] for q in questions):
            plan["status"], plan["executionEligible"] = "needs_input", False
            plan["planDigest"] = plan_digest(plan)
    return validate_deployment_plan(plan, analysis=analysis)
