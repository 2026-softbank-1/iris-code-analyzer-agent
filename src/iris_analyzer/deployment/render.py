"""Compile a validated deployment plan into fixed, inspectable execution inputs.

This module does not build images, execute source, contact a cluster, invoke
Terraform/Helm, or authorize deployment. Hashes provide integrity, not identity.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from iris_analyzer.contracts import AnalyzerError, canonical_bytes, digest

TEMPLATE_VERSION = "iris.execution.v1"
SUPPORTED_ADAPTERS = {"aws_eks", "existing_kubernetes"}
_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_EKS_NAME = re.compile(r"^[a-z][a-z0-9-]{2,39}$")
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
_INSTANCES = {
    "t3.medium": ("amd64", 2000, 4096),
    "t3.large": ("amd64", 2000, 8192),
    "m6i.large": ("amd64", 2000, 8192),
    "m6i.xlarge": ("amd64", 4000, 16384),
    "m6i.2xlarge": ("amd64", 8000, 32768),
    "t4g.medium": ("arm64", 2000, 4096),
    "t4g.large": ("arm64", 2000, 8192),
    "m6g.large": ("arm64", 2000, 8192),
    "m6g.xlarge": ("arm64", 4000, 16384),
    "m6g.2xlarge": ("arm64", 8000, 32768),
}
_SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "powershell", "pwsh", "cmd", "cmd.exe"}


def _value(recommendation: dict | None) -> Any:
    return recommendation.get("value") if isinstance(recommendation, dict) else None


def _reason(code: str, path: str, message: str) -> dict:
    return {"code": code, "path": path, "message": message}


def _base(plan: dict) -> dict:
    identifier = plan.get("planDigest")
    identifier = (
        identifier if isinstance(identifier, str) and re.fullmatch(r"[a-f0-9]{64}", identifier) else None
    )
    return {
        "schemaVersion": TEMPLATE_VERSION,
        "templateVersion": TEMPLATE_VERSION,
        "planDigest": identifier,
        "status": "blocked",
        "executionEligible": False,
        "deploymentAuthorized": False,
        "blockedReasons": [],
        "terraform": None,
        "helm": None,
        "kubernetes": None,
    }


def _positive(value: Any) -> bool:
    return type(value) is int and value > 0


def _architecture(target: dict | None) -> str | None:
    value = target.get("architecture") if target else None
    return "amd64" if value == "x86_64" else value


def _claim_name(workload: dict, volume: dict) -> str:
    if volume.get("claimName"):
        return volume["claimName"]
    name = workload["name"] + "-" + volume["name"]
    return name if len(name) <= 63 else name[:54].rstrip("-") + "-" + digest(name)[:8]


def _preflight(plan: dict) -> list[dict]:
    """Compiler-specific bounds in addition to the shared plan validator."""
    errors: list[dict] = []

    def block(code: str, path: str, message: str) -> None:
        errors.append(_reason(code, path, message))

    adapter = plan["adapter"]
    target = _value(plan["recommendations"]["target"])
    config = plan["configuration"]
    namespace = config.get("namespace")
    if not isinstance(namespace, str) or not _NAME.fullmatch(namespace):
        block("NAMESPACE_REQUIRED", "configuration.namespace", "An explicit DNS namespace is required")
    if not target or target.get("stack") != adapter:
        block("ADAPTER_TARGET_MISMATCH", "recommendations.target", "Target must match the selected template")
    architecture = _architecture(target)
    if architecture not in {"amd64", "arm64"}:
        block("ARCHITECTURE_REQUIRED", "recommendations.target", "An explicit CPU architecture is required")
    workloads = config.get("workloads", [])
    if not workloads:
        block("WORKLOADS_REQUIRED", "configuration.workloads", "At least one configured workload is required")
    if _value(plan["recommendations"].get("databases")) and not config.get("databasesVerified"):
        block(
            "DATABASE_PREREQUISITE",
            "configuration.databasesVerified",
            "External databases must be provisioned and connectivity verified",
        )
    if (_value(plan["recommendations"].get("observability")) or {}).get("prometheusEnabled"):
        block(
            "MONITORING_TEMPLATE_UNSUPPORTED",
            "recommendations.observability",
            "This template does not install monitoring infrastructure",
        )
    names: set[str] = set()
    claims: dict[str, dict] = {}
    claim_pods: dict[str, int] = {}
    total_cpu = total_memory = largest_cpu = largest_memory = 0
    for index, workload in enumerate(workloads):
        path = f"configuration.workloads.{index}"
        name = workload.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in names:
            block("WORKLOAD_NAME_INVALID", path + ".name", "Workload names must be unique DNS labels")
        if isinstance(name, str):
            names.add(name)
        image = workload.get("image")
        if (
            not isinstance(image, str)
            or not _IMAGE.fullmatch(image)
            or any(segment in {"", ".", ".."} for segment in image.partition("@")[0].split("/"))
        ):
            block(
                "IMMUTABLE_IMAGE_REQUIRED",
                path + ".image",
                "A registry image pinned by sha256 digest is required",
            )
        if config.get("imagePlatforms", {}).get(workload.get("serviceId")) != architecture:
            block(
                "IMAGE_PLATFORM_UNVERIFIED",
                path + ".image",
                "The image platform must be explicitly verified against the target architecture",
            )
        command = workload.get("command")
        if command and Path(command[0]).name.lower() in _SHELLS:
            block(
                "SHELL_COMMAND_UNSUPPORTED",
                path + ".command",
                "This template accepts image entrypoints and direct exec arrays only",
            )
        resources = _value(workload.get("resources")) or {}
        requests = resources.get("requests", {})
        limits = resources.get("limits", {})
        cpu, memory = requests.get("cpuMillicores"), requests.get("memoryMiB")
        limit_cpu, limit_memory = limits.get("cpuMillicores"), limits.get("memoryMiB")
        valid_resources = all(_positive(v) for v in (cpu, memory, limit_cpu, limit_memory))
        if not valid_resources or limit_cpu < cpu or limit_memory < memory:
            block(
                "RESOURCES_INVALID",
                path + ".resources",
                "Positive integer requests and limits at least as large as requests are required",
            )
        replicas = _value(workload.get("replicas"))
        rollout = _value(workload.get("rollout")) or {}
        surge, unavailable = rollout.get("maxSurge"), rollout.get("maxUnavailable")
        valid_rollout = (
            _positive(replicas)
            and replicas <= 50
            and type(surge) is int
            and 0 <= surge <= 50
            and type(unavailable) is int
            and 0 <= unavailable <= replicas
            and surge + unavailable > 0
            and rollout.get("strategy") == "RollingUpdate"
            and _positive(rollout.get("progressDeadlineSeconds"))
        )
        if not valid_rollout:
            block(
                "ROLLOUT_INVALID",
                path + ".rollout",
                "Replicas and a bounded non-stalled RollingUpdate policy are required",
            )
        rollback = _value(workload.get("rollback")) or {}
        if (
            rollback.get("strategy") != "helm_atomic"
            or not _positive(rollback.get("revisionHistoryLimit"))
            or not _positive(rollback.get("timeoutSeconds"))
            or rollback.get("timeoutSeconds", 0) < rollout.get("progressDeadlineSeconds", 0)
        ):
            block(
                "ROLLBACK_INVALID",
                path + ".rollback",
                "A Helm atomic rollback timeout must cover the rollout deadline",
            )
        if valid_resources and valid_rollout:
            total_cpu += cpu * (replicas + surge)
            total_memory += memory * (replicas + surge)
            largest_cpu, largest_memory = max(largest_cpu, limit_cpu), max(largest_memory, limit_memory)
        ports = {port["port"] for port in workload.get("containerPorts", [])}
        probes = _value(workload.get("probes")) or {}
        if not probes.get("readiness") or not probes.get("liveness"):
            block(
                "PROBES_REQUIRED",
                path + ".probes",
                "Explicit readiness and liveness probes are required; paths are never guessed",
            )
        for probe_name, probe in probes.items():
            if probe and probe.get("port") not in ports:
                block(
                    "PROBE_PORT_MISMATCH",
                    path + ".probes." + probe_name,
                    "Probe ports must belong to the workload",
                )
        ingress = _value(workload.get("ingress")) or {}
        if ingress.get("enabled"):
            if not config.get("ingressVerified") or not all(
                ingress.get(key) for key in ("host", "className", "tlsSecretName")
            ):
                block(
                    "INGRESS_PREREQUISITE",
                    path + ".ingress",
                    "An installed controller, DNS host and existing TLS Secret are required",
                )
        volumes = _value(workload.get("volumes")) or []
        if volumes and not config.get("storageDriverVerified"):
            block(
                "STORAGE_DRIVER_PREREQUISITE",
                path + ".volumes",
                "The cluster storage driver and StorageClass must be verified",
            )
        for volume in volumes:
            if volume.get("claimName") is None and (
                not volume.get("storageClass") or not _positive(volume.get("sizeGiB"))
            ):
                block(
                    "VOLUME_BINDING_REQUIRED",
                    path + ".volumes",
                    "Each volume needs an existing claim or an explicit size and StorageClass for a new claim",
                )
            if valid_rollout and volume["accessMode"] == "ReadWriteOnce" and replicas + surge > 1:
                block(
                    "RWO_ROLLOUT_UNSUPPORTED",
                    path + ".volumes",
                    "ReadWriteOnce volumes require a single pod and zero surge for this template",
                )
            claim = _claim_name(workload, volume)
            if claim:
                binding = {key: volume[key] for key in ("sizeGiB", "storageClass", "accessMode")}
                binding["managed"] = volume["claimName"] is None
                if claim in claims and claims[claim] != binding:
                    block(
                        "CLAIM_BINDING_CONFLICT",
                        path + ".volumes",
                        "A shared claim must have one consistent size, StorageClass and access mode",
                    )
                claims[claim] = binding
                if valid_rollout:
                    claim_pods[claim] = claim_pods.get(claim, 0) + replicas + surge
                    if volume["accessMode"] == "ReadWriteOnce" and claim_pods[claim] > 1:
                        block(
                            "RWO_SHARED_CLAIM_UNSUPPORTED",
                            path + ".volumes",
                            "ReadWriteOnce claims cannot be shared across multiple workload pods in this template",
                        )
    capacity = config.get("availableCapacity")
    if adapter == "aws_eks":
        instance = _value(plan["recommendations"].get("instance")) or {}
        network = _value(plan["recommendations"].get("network")) or {}
        terraform = config.get("terraformInputs") or {}
        expected_instance = _INSTANCES.get(instance.get("instanceType"))
        if expected_instance != (architecture, instance.get("cpuMillicores"), instance.get("memoryMiB")):
            block(
                "INSTANCE_UNSUPPORTED",
                "recommendations.instance",
                "Instance architecture, CPU and memory must match the fixed catalog",
            )
        if (
            network.get("vpcMode") != "existing"
            or not network.get("vpcId")
            or len(set(network.get("subnetIds", []))) < 2
        ):
            block(
                "EXISTING_NETWORK_REQUIRED",
                "recommendations.network",
                "This template requires an existing VPC and two private subnets",
            )
        if len(set(network.get("availabilityZones", []))) < 2:
            block(
                "MULTI_AZ_SUBNETS_REQUIRED",
                "recommendations.network",
                "Explicit subnet availability zones must span at least two zones",
            )
        if not terraform or not all(
            terraform.get(key)
            for key in (
                "kubernetesVersion",
                "administratorRoleArn",
                "nodeDiskGiB",
                "desiredNodes",
                "privateNetworkEgressVerified",
                "executorPrivateApiReachable",
            )
        ):
            block(
                "EKS_BINDINGS_REQUIRED",
                "configuration.terraformInputs",
                "Cluster version, administrator, disk, node count, egress and private API reachability must be configured",
            )
        if not config.get("clusterName") or not _EKS_NAME.fullmatch(config["clusterName"]):
            block(
                "CLUSTER_NAME_REQUIRED",
                "configuration.clusterName",
                "An explicit new EKS cluster name of 3 to 40 characters is required",
            )
        if terraform and (not 20 <= terraform["nodeDiskGiB"] <= 1024 or instance.get("maxNodes", 0) > 100):
            block(
                "EKS_TEMPLATE_LIMIT",
                "configuration.terraformInputs",
                "Fixed EKS bounds are 20 to 1024 GiB disk and at most 100 nodes",
            )
        desired = terraform.get("desiredNodes")
        if _positive(desired) and all(_positive(instance.get(key)) for key in ("cpuMillicores", "memoryMiB")):
            if not instance.get("minNodes", 0) <= desired <= instance.get("maxNodes", 0):
                block(
                    "NODE_COUNT_INVALID",
                    "configuration.terraformInputs.desiredNodes",
                    "Desired node count must stay inside the validated scaling range",
                )
            # Controlled headroom policy: this is not measured EKS allocatable capacity.
            capacity = {
                "cpuMillicores": instance["cpuMillicores"] * desired * 4 // 5,
                "memoryMiB": instance["memoryMiB"] * desired * 4 // 5,
                "nodeCount": desired,
            }
            if (
                largest_cpu > instance["cpuMillicores"] * 4 // 5
                or largest_memory > instance["memoryMiB"] * 4 // 5
            ):
                block(
                    "POD_EXCEEDS_NODE_CAPACITY",
                    "recommendations.instance",
                    "A pod limit exceeds the node capacity after the fixed 20% overhead reservation",
                )
    elif adapter == "existing_kubernetes" and not config.get("existingClusterContext"):
        block(
            "CLUSTER_CONTEXT_REQUIRED",
            "configuration.existingClusterContext",
            "An explicit caller-selected Kubernetes context is required",
        )
    if not capacity or not all(
        _positive(capacity.get(key)) for key in ("cpuMillicores", "memoryMiB", "nodeCount")
    ):
        block(
            "CAPACITY_REQUIRED",
            "configuration.availableCapacity",
            "Explicit allocatable cluster capacity is required",
        )
    elif total_cpu > capacity["cpuMillicores"] or total_memory > capacity["memoryMiB"]:
        block(
            "INSUFFICIENT_SURGE_CAPACITY",
            "configuration.workloads",
            "Requests including simultaneous rolling surge exceed available capacity",
        )
    return errors


def _metadata(name: str, namespace: str, plan_digest: str) -> dict:
    return {
        "name": name,
        "namespace": namespace,
        "labels": {"app.kubernetes.io/managed-by": "iris"},
        "annotations": {"iris.ai/plan-digest": plan_digest},
    }


def _probe(probe: dict) -> dict:
    result = {
        key: probe[key]
        for key in ("initialDelaySeconds", "periodSeconds", "timeoutSeconds", "failureThreshold")
    }
    result["httpGet" if probe["kind"] == "http" else "tcpSocket"] = {"port": probe["port"]}
    if probe["kind"] == "http":
        result["httpGet"]["path"] = probe["path"]
    return result


def _manifests(plan: dict) -> list[dict]:
    config, plan_digest = plan["configuration"], plan["planDigest"]
    namespace = config["namespace"]
    architecture = _architecture(_value(plan["recommendations"]["target"]))
    # Namespace creation belongs to the executor. Rendering it in a Helm chart
    # conflicts with --create-namespace or an existing externally owned namespace.
    result: list[dict] = []
    claims: dict[str, dict] = {}
    for workload in config["workloads"]:
        name, replicas = workload["name"], _value(workload["replicas"])
        selector = {"app.kubernetes.io/name": name}
        resource = _value(workload["resources"])
        container = {
            "name": name,
            "image": workload["image"],
            "imagePullPolicy": "IfNotPresent",
            "ports": [
                {"name": p["name"], "containerPort": p["port"], "protocol": p["protocol"]}
                for p in workload["containerPorts"]
            ],
            "resources": {
                kind: {
                    "cpu": f"{resource[kind]['cpuMillicores']}m",
                    "memory": f"{resource[kind]['memoryMiB']}Mi",
                }
                for kind in ("requests", "limits")
            },
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
                "seccompProfile": {"type": "RuntimeDefault"},
            },
        }
        for key in ("command", "args"):
            if workload.get(key) is not None:
                container[key] = copy.deepcopy(workload[key])
        secret_refs = _value(workload["secretRefs"])
        if secret_refs:
            container["env"] = [
                {
                    "name": ref["environmentKey"],
                    "valueFrom": {
                        "secretKeyRef": {"name": ref["name"], "key": ref["key"], "optional": False}
                    },
                }
                for ref in secret_refs
            ]
        for name_key, probe in _value(workload["probes"]).items():
            if probe:
                container[name_key + "Probe"] = _probe(probe)
        pod_spec = {
            "automountServiceAccountToken": False,
            "nodeSelector": {"kubernetes.io/arch": architecture},
            "containers": [container],
            "terminationGracePeriodSeconds": 30,
            "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
        }
        if replicas > 1:
            pod_spec["affinity"] = {
                "podAntiAffinity": {
                    "preferredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "weight": 100,
                            "podAffinityTerm": {
                                "labelSelector": {"matchLabels": selector},
                                "topologyKey": "topology.kubernetes.io/zone",
                            },
                        }
                    ]
                }
            }
        volumes = _value(workload["volumes"])
        if volumes:
            container["volumeMounts"] = [
                {"name": volume["name"], "mountPath": volume["mountPath"]} for volume in volumes
            ]
            pod_spec["volumes"] = [
                {
                    "name": volume["name"],
                    "persistentVolumeClaim": {"claimName": _claim_name(workload, volume)},
                }
                for volume in volumes
            ]
            for volume in volumes:
                # Explicit claimName always references an existing externally owned
                # claim. Only a null reference requests fixed-name PVC provisioning.
                if volume["claimName"] is None:
                    claim_name = _claim_name(workload, volume)
                    claims[claim_name] = {
                        "apiVersion": "v1",
                        "kind": "PersistentVolumeClaim",
                        "metadata": _metadata(claim_name, namespace, plan_digest),
                        "spec": {
                            "accessModes": [volume["accessMode"]],
                            "storageClassName": volume["storageClass"],
                            "resources": {"requests": {"storage": f"{volume['sizeGiB']}Gi"}},
                        },
                    }
        rollout, rollback = _value(workload["rollout"]), _value(workload["rollback"])
        result.append(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": _metadata(name, namespace, plan_digest),
                "spec": {
                    "replicas": replicas,
                    "revisionHistoryLimit": rollback["revisionHistoryLimit"],
                    "progressDeadlineSeconds": rollout["progressDeadlineSeconds"],
                    "strategy": {
                        "type": "RollingUpdate",
                        "rollingUpdate": {
                            "maxSurge": rollout["maxSurge"],
                            "maxUnavailable": rollout["maxUnavailable"],
                        },
                    },
                    "selector": {"matchLabels": selector},
                    "template": {"metadata": {"labels": selector}, "spec": pod_spec},
                },
            }
        )
        service = _value(workload["service"])
        if service:
            result.append(
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": _metadata(name, namespace, plan_digest),
                    "spec": {
                        "type": "ClusterIP",
                        "selector": selector,
                        "ports": [
                            {
                                "name": "application",
                                "port": service["port"],
                                "targetPort": service["targetPort"],
                                "protocol": service["protocol"],
                            }
                        ],
                    },
                }
            )
        ingress = _value(workload["ingress"])
        if ingress["enabled"]:
            result.append(
                {
                    "apiVersion": "networking.k8s.io/v1",
                    "kind": "Ingress",
                    "metadata": _metadata(name, namespace, plan_digest),
                    "spec": {
                        "ingressClassName": ingress["className"],
                        "tls": [{"hosts": [ingress["host"]], "secretName": ingress["tlsSecretName"]}],
                        "rules": [
                            {
                                "host": ingress["host"],
                                "http": {
                                    "paths": [
                                        {
                                            "path": ingress["path"],
                                            "pathType": "Prefix",
                                            "backend": {
                                                "service": {"name": name, "port": {"number": service["port"]}}
                                            },
                                        }
                                    ]
                                },
                            }
                        ],
                    },
                }
            )
        if replicas > 1:
            result.append(
                {
                    "apiVersion": "policy/v1",
                    "kind": "PodDisruptionBudget",
                    "metadata": _metadata(name, namespace, plan_digest),
                    "spec": {"maxUnavailable": 1, "selector": {"matchLabels": selector}},
                }
            )
    result.extend(claims.values())
    return result


def compile_plan(plan: dict, *, analysis: dict | None = None) -> dict:
    """Return machine-readable blocked reasons or fixed execution configuration.

    Even a ready bundle has deploymentAuthorized=false. The backend owns cloud
    credentials, approval, Terraform state, build/test and the deployment job.
    """
    from .contracts import validate_deployment_plan

    output = _base(plan if isinstance(plan, dict) else {})
    try:
        canonical_bytes(plan)
        plan = copy.deepcopy(plan)
        validate_deployment_plan(plan, analysis=analysis)
    except (AnalyzerError, TypeError, ValueError) as exc:
        output["blockedReasons"] = [
            _reason(
                getattr(exc, "code", "DEPLOYMENT_PLAN_INVALID"),
                "plan",
                "Deployment plan failed contract or integrity validation",
            )
        ]
        return output
    if plan["adapter"] not in SUPPORTED_ADAPTERS:
        output["blockedReasons"] = [
            _reason("TEMPLATE_UNSUPPORTED", "adapter", "No fixed execution template exists for this stack")
        ]
        return output
    if plan["executionEligible"] is not True or plan["status"] != "ready":
        output["blockedReasons"] = [
            _reason(
                "PLAN_NOT_READY",
                "status",
                "Recommended values need explicit execution bindings and prerequisite validation",
            )
        ]
        output["blockedReasons"].extend(
            _reason("EXECUTION_INPUT_REQUIRED", question["field"], question["reason"])
            for question in plan["questions"]
            if question["requiredForExecution"]
        )
        return output
    errors = _preflight(plan)
    if errors:
        output["blockedReasons"] = errors
        return output
    manifests = _manifests(plan)
    config, recommendations = plan["configuration"], plan["recommendations"]
    if plan["adapter"] == "aws_eks":
        instance, network, target = (
            _value(recommendations[key]) for key in ("instance", "network", "target")
        )
        execution = config["terraformInputs"]
        output["terraform"] = {
            "moduleSource": "templates/terraform/aws_eks",
            "providerVersion": "6.14.1",
            "inputs": {
                "region": target["region"],
                "cluster_name": config["clusterName"],
                "kubernetes_version": execution["kubernetesVersion"],
                "vpc_id": network["vpcId"],
                "private_subnet_ids": network["subnetIds"],
                "administrator_role_arn": execution["administratorRoleArn"],
                "instance_type": instance["instanceType"],
                "architecture": _architecture(target),
                "nodes": {
                    "min": instance["minNodes"],
                    "desired": execution["desiredNodes"],
                    "max": instance["maxNodes"],
                },
                "node_disk_gib": execution["nodeDiskGiB"],
            },
        }
    output.update(
        {
            "status": "ready",
            "executionEligible": True,
            "helm": {
                "chart": "iris-app",
                "chartVersion": "0.1.0",
                "values": {
                    "namespace": config["namespace"],
                    "planDigest": plan["planDigest"],
                    "resources": manifests,
                },
                "releasePolicy": {
                    "atomic": True,
                    "wait": True,
                    "namespace": config["namespace"],
                    "createNamespace": True,
                    "timeoutSeconds": max(
                        _value(w["rollback"])["timeoutSeconds"] for w in config["workloads"]
                    ),
                },
            },
            "kubernetes": {
                "namespace": config["namespace"],
                "createNamespace": True,
                "manifests": copy.deepcopy(manifests),
            },
            "prerequisites": [
                "Backend credentials and deployment approval",
                "Image entrypoint and restrictive securityContext compatibility",
                "External databases, networking, TLS and storage are caller verified",
            ],
            "capacityPolicy": "AWS reserves 20% of node CPU/memory and includes simultaneous rollout surge; existing Kubernetes uses caller-supplied allocatable capacity",
        }
    )
    return output


def _copy_template(source: Any, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        if child.name == ".terraform":
            continue
        target = destination / child.name
        if child.is_dir():
            _copy_template(child, target)
        elif child.name.endswith((".tf", ".yaml", ".json", ".hcl")):
            target.write_bytes(child.read_bytes())


def write_execution_bundle(plan: dict, out: str | Path, *, analysis: dict | None = None) -> dict:
    """Save a bundle into a new/empty output directory, without invoking tools.

    Nonempty destinations are rejected so old runnable files cannot survive a
    later blocked compile. This function owns only the selected output folder.
    """
    result = compile_plan(plan, analysis=analysis)
    destination = Path(out)
    if destination.is_symlink() or (
        destination.exists() and (not destination.is_dir() or any(destination.iterdir()))
    ):
        raise AnalyzerError(
            "EXECUTION_OUTPUT_NOT_EMPTY", "Execution bundle destination must be a new or empty directory"
        )
    destination.mkdir(parents=True, exist_ok=True)
    try:
        (destination / "execution.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if result["status"] == "ready":
            templates = files("iris_analyzer.deployment").joinpath("templates")
            if result["terraform"]:
                tf_destination = destination / "terraform"
                _copy_template(templates.joinpath("terraform", "aws_eks"), tf_destination)
                (tf_destination / "deployment.tfvars.json").write_text(
                    json.dumps(result["terraform"]["inputs"], indent=2) + "\n", encoding="utf-8"
                )
            _copy_template(templates.joinpath("helm", "iris-app"), destination / "helm" / "iris-app")
            (destination / "helm" / "values.yaml").write_text(
                yaml.safe_dump(result["helm"]["values"], sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            (destination / "manifests.yaml").write_text(
                yaml.safe_dump_all(result["kubernetes"]["manifests"], sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
    except (OSError, UnicodeError) as exc:
        shutil.rmtree(destination)
        raise AnalyzerError("EXECUTION_WRITE_FAILED", "Execution bundle could not be written") from exc
    return result
