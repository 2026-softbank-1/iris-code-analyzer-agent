"""Plan an Organization as a system without changing repository observations.

The runtime call graph is intentionally separate from the executor task graph.
This first adapter targets an explicitly selected existing Kubernetes cluster;
provisioning a database, cloud account, network or cluster remains an input.
"""

from __future__ import annotations

import copy
import re
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from iris_analyzer.build.source import safe_relative
from iris_analyzer.contracts import AnalyzerError, canonical_bytes, digest
from iris_analyzer.deployment.contracts import is_secret_environment_key
from iris_analyzer.preprocess.redaction import redact

_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")
_REPOSITORY = re.compile(r"[a-z0-9][a-z0-9._:/-]*")
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")
_SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "cmd", "cmd.exe", "pwsh", "powershell"}


def _value(field: Any) -> Any:
    return field.get("value") if isinstance(field, dict) and "value" in field else field


def system_plan_digest(plan: dict) -> str:
    return digest({key: value for key, value in plan.items() if key != "planDigest"})


def _fail(message: str, path: str = "plan") -> None:
    raise AnalyzerError("SYSTEM_PLAN_INVALID", message, {"path": path})


def _dns(value: str) -> str:
    value = re.sub(r"[^a-z0-9-]", "-", str(value).lower()).strip("-") or "service"
    return value if len(value) <= 63 else value[:54].rstrip("-") + "-" + digest(value)[:8]


def _repository_id(record: dict) -> str:
    repository = record.get("repository", {})
    identifier = record.get(
        "repositoryId", record.get("id", repository.get("id", repository.get("repositoryId")))
    )
    if identifier is None:
        _fail("Repository records require their GitHub repository ID", "repositories")
    return str(identifier)


def _literal(key: str, value: Any) -> None:
    if not isinstance(key, str) or not _ENV.fullmatch(key):
        _fail("Environment keys must be explicit identifiers", "runtimeEnv")
    if is_secret_environment_key(key):
        _fail("Credential keys require existing Secret references", "runtimeEnv." + key)
    if not isinstance(value, str) or redact(value, "public-value.txt")[1] or "<REDACTED>" in value:
        _fail("Public environment values cannot contain credentials", "runtimeEnv." + key)
    if any(ord(c) < 32 for c in value) or "$(" in value:
        _fail("Public environment values must be literal text", "runtimeEnv." + key)


def _image_reference(value: Any) -> bool:
    if not isinstance(value, str) or not _IMAGE.fullmatch(value):
        return False
    repository = value.split("@", 1)[0]
    segments = repository.split("/")
    return (
        not any(part in {"", ".", ".."} for part in segments)
        and not any(":" in part for part in segments[1:])
        and ":" not in segments[-1]
    )


def _direct_command(value: Any) -> list[str] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 64
        or any(not isinstance(item, str) or not item or "\x00" in item for item in value)
        or PurePosixPath(value[0]).name.lower() in _SHELLS
    ):
        _fail("Runtime command overrides require a direct exec array; use the image entrypoint otherwise")
    return copy.deepcopy(value)


def _resources(value: Any, *, production: bool) -> dict | None:
    if value is None:
        return (
            None
            if production
            else {
                "requests": {"cpuMillicores": 100, "memoryMiB": 128},
                "limits": {"cpuMillicores": 1000, "memoryMiB": 512},
            }
        )
    if isinstance(value, dict) and set(value) == {"cpuMillicores", "memoryMiB"}:
        if any(type(quantity) is not int or not 1 <= quantity <= 1_000_000 for quantity in value.values()):
            _fail("Resource quantities must be bounded positive integers")
        # Legacy shorthand expresses requests only; limits follow a disclosed
        # preview policy and cannot establish production readiness.
        if production:
            return None
        value = {"requests": value, "limits": {key: quantity * 2 for key, quantity in value.items()}}
    if not isinstance(value, dict) or set(value) != {"requests", "limits"}:
        _fail("Resources require requests and limits")
    for group in ("requests", "limits"):
        if not isinstance(value[group], dict) or set(value[group]) != {"cpuMillicores", "memoryMiB"}:
            _fail("Resource quantities require cpuMillicores and memoryMiB")
        for name, quantity in value[group].items():
            if type(quantity) is not int or not 1 <= quantity <= 1_000_000:
                _fail("Resource quantities must be bounded positive integers", "resources." + name)
    if any(value["requests"][key] > value["limits"][key] for key in value["requests"]):
        _fail("Resource limits cannot be smaller than requests")
    return copy.deepcopy(value)


def _target(request: dict) -> dict:
    deployment = request.get("deploymentRequest") or {}
    original = request.get("target") or {}
    if isinstance(original, str):
        original = {"kind": original}
    bindings = deployment.get("bindings", {})
    architecture = original.get("architecture") or deployment.get("target", {}).get("architecture") or "amd64"
    architecture = "amd64" if architecture == "x86_64" else architecture
    return {
        "kind": original.get("kind", original.get("stack", "existing_kubernetes")),
        "context": request.get("kubernetesContext")
        or original.get("context")
        or bindings.get("existingClusterContext"),
        "namespace": request.get("namespace")
        or original.get("namespace")
        or bindings.get("namespace")
        or _dns("iris-org-" + request["organization"]),
        "architecture": architecture,
    }


def _ingress(service_id: str, binding: dict, request: dict) -> dict | None:
    deployment_bindings = (request.get("deploymentRequest") or {}).get("bindings", {})
    candidates = [
        item for item in deployment_bindings.get("ingress", []) if item.get("serviceId") == service_id
    ]
    if len(candidates) > 1:
        _fail("A system service requires one public ingress binding", "ingress." + service_id)
    configured = copy.deepcopy(candidates[0]) if candidates else None
    host = binding.get("publicHost")
    if host is not None:
        if not isinstance(host, str) or not _HOST.fullmatch(host) or "." not in host or ".." in host:
            _fail("Public hosts must be explicit DNS names", "publicHost." + service_id)
        if configured and configured.get("host") != host:
            _fail("Public host and trusted ingress binding must agree", "publicHost." + service_id)
    if configured:
        configured.pop("serviceId", None)
        configured["verified"] = deployment_bindings.get("ingressVerified") is True
        return configured
    return (
        {"host": host, "className": None, "tlsSecretName": None, "path": "/", "verified": False}
        if host
        else None
    )


def _dossiers(records: list[dict], request: dict) -> list[dict]:
    """Keep each canonical snapshot in its own existing deployment contract."""
    from iris_analyzer.deployment.dossier import build_deployment_dossier_from_readiness
    from iris_analyzer.deployment.planner import create_deployment_plan
    from iris_analyzer.deployment.render import compile_plan

    output = []
    for record in records:
        if not isinstance(record.get("analysis"), dict):
            continue
        analysis = record["analysis"]
        entry = {"repositoryId": _repository_id(record), "analysisDigest": digest(analysis)}
        if record.get("deploymentDossier"):
            entry["dossier"] = copy.deepcopy(record["deploymentDossier"])
        else:
            # No synthetic concatenated analysis: qualified IDs belong only to
            # this system plan, while the per-repository contract stays intact.
            local_request = copy.deepcopy(request.get("deploymentRequest"))
            if local_request is None:
                target = _target(request)
                local_request = {
                    "schemaVersion": "iris.planning-request.v1",
                    "target": {
                        "stack": target["kind"],
                        "architecture": "x86_64"
                        if target["architecture"] == "amd64"
                        else target["architecture"],
                        "environment": "production" if request.get("environment") == "production" else "test",
                    },
                    "bindings": {
                        "namespace": target["namespace"],
                        "existingClusterContext": target["context"],
                    },
                }
            if local_request:
                original_ids = {service["serviceId"] for service in analysis.get("services", [])}
                prefix = "repo-" + entry["repositoryId"] + "--"
                for collection in (
                    "images",
                    "secretRefs",
                    "runtimeEnv",
                    "configMapRefs",
                    "ingress",
                    "volumes",
                ):
                    local_bindings = local_request.get("bindings", {})
                    items = []
                    for item in local_bindings.get(collection, []):
                        sid = item.get("serviceId", "")
                        local_sid = sid[len(prefix) :] if sid.startswith(prefix) else sid
                        if local_sid in original_ids:
                            items.append({**item, "serviceId": local_sid})
                    local_bindings[collection] = items
                local_request.get("bindings", {})["imagePlatforms"] = {
                    sid[len(prefix) :] if sid.startswith(prefix) else sid: value
                    for sid, value in local_request.get("bindings", {}).get("imagePlatforms", {}).items()
                    if (sid[len(prefix) :] if sid.startswith(prefix) else sid) in original_ids
                }
            try:
                if isinstance(record.get("readiness"), dict):
                    entry["dossier"] = build_deployment_dossier_from_readiness(
                        analysis, record["readiness"], local_request
                    )
                else:
                    deployment_plan = create_deployment_plan(analysis, local_request)
                    entry["deploymentPlan"] = deployment_plan
                    entry["execution"] = compile_plan(deployment_plan, analysis=analysis)
            except AnalyzerError as exc:
                entry["status"] = "unavailable"
                entry["reasonCode"] = exc.code
        output.append(entry)
    return output


def build_system_plan(graph: dict, records: list[dict], request: dict) -> dict:
    """Create immutable source/image/task bindings; no build or cloud calls."""
    canonical_bytes(graph)
    canonical_bytes(request)
    if graph.get("schemaVersion") != "iris.system-graph.v1":
        _fail("Expected iris.system-graph.v1", "graph")
    if request.get("schemaVersion") != "iris.organization-request.v1":
        _fail("Expected iris.organization-request.v1", "request")
    if request.get("organization") != graph.get("organization"):
        _fail("Graph and request belong to different Organizations", "organization")
    target = _target(request)
    questions: list[dict] = []

    def question(key: str, reason: str, sid: str | None = None, *, required: bool = True) -> None:
        if not any(item["key"] == key for item in questions):
            questions.append(
                {"key": key, "reason": reason, "serviceId": sid, "requiredForExecution": required}
            )

    if target["kind"] != "existing_kubernetes":
        question(
            "target.kind",
            "This system executor supports an existing Kubernetes cluster; new cloud infrastructure requires a provider adapter.",
        )
    if (
        not isinstance(target["context"], str)
        or not target["context"]
        or target["context"].startswith("-")
        or any(ord(c) < 32 for c in target["context"])
    ):
        question("target.context", "Select an explicit existing kubectl context.")
    if not isinstance(target["namespace"], str) or not _NAME.fullmatch(target["namespace"]):
        _fail("Namespace must be a DNS label", "target.namespace")
    if target["architecture"] not in {"amd64", "arm64"}:
        question("target.architecture", "Select amd64 or arm64 for every image and node placement.")
    record_map = {_repository_id(record): record for record in records}
    if len(record_map) != len(records):
        _fail("Repository records must be unique", "repositories")
    all_components = {item["id"]: item for item in graph.get("components", [])}
    selected_ids = request.get("selectedServiceIds") or [
        sid for sid, item in all_components.items() if item.get("selected")
    ]
    selected_repository_ids = {
        str(all_components[sid]["repositoryId"]) for sid in selected_ids if sid in all_components
    }
    repositories = []
    for identifier, record in sorted(record_map.items()):
        manifest = record.get("buildSourceManifest")
        analysis = record.get("analysis") or {}
        commit = (
            record.get("commitSha")
            or record.get("revision")
            or record.get("resolvedCommit")
            or (manifest or {}).get("origin", {}).get("revision")
        )
        repositories.append(
            {
                "repositoryId": identifier,
                "commitSha": commit,
                "sourceSnapshotId": record.get("sourceSnapshotId") or analysis.get("sourceSnapshotId"),
                "analysisDigest": digest(analysis) if analysis else record.get("analysisDigest"),
                "sourceManifestDigest": (manifest or {}).get("sourceManifestSha256"),
                "buildSourceManifest": copy.deepcopy(manifest),
            }
        )
        if identifier in selected_repository_ids and (
            not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)
        ):
            question(
                "repositories." + identifier + ".commitSha",
                "Lock this repository to its exact GitHub commit.",
            )
        if manifest and manifest.get("origin", {}).get("revision") != commit:
            _fail("Build source and repository commit differ", "repositories." + identifier)
    if len(all_components) != len(graph.get("components", [])):
        _fail("Graph component IDs must be unique", "components")
    bindings = {item["serviceId"]: item for item in request.get("serviceBindings", [])}
    if len(bindings) != len(request.get("serviceBindings", [])):
        _fail("Service bindings must be unique", "serviceBindings")
    if set(bindings) - set(all_components):
        _fail("Service binding refers to an unknown graph component", "serviceBindings")
    bindings = copy.deepcopy(bindings)
    # Reuse explicit bindings from the existing planning contract only when
    # they name a qualified system component. An unqualified repository service
    # ID is never guessed across repositories.
    deployment_bindings = (request.get("deploymentRequest") or {}).get("bindings", {})
    for item in deployment_bindings.get("images", []):
        if item.get("serviceId") in all_components:
            bindings.setdefault(item["serviceId"], {"serviceId": item["serviceId"]}).setdefault(
                "imageReference", item["reference"]
            )
    for collection in ("runtimeEnv", "secretRefs"):
        for item in deployment_bindings.get(collection, []):
            if item.get("serviceId") not in all_components:
                continue
            binding = bindings.setdefault(item["serviceId"], {"serviceId": item["serviceId"]})
            key = item["environmentKey"]
            existing = {
                entry["key"] for name in ("runtimeEnv", "secretRefs") for entry in binding.get(name, [])
            }
            if key in existing:
                continue
            value = (
                {"key": key, "value": item["value"]}
                if collection == "runtimeEnv"
                else {"key": key, "name": item["name"], "secretKey": item.get("key", item.get("secretKey"))}
            )
            binding.setdefault(collection, []).append(value)
    if set(selected_ids) - set(all_components):
        _fail("Selected services must exist in the graph", "selectedServiceIds")
    selected = [all_components[sid] for sid in sorted(set(selected_ids))]
    selected_ids = {item["id"] for item in selected}
    if not selected:
        question("selectedServiceIds", "Select at least one runnable service for this system.")
    components, builds, runtime_bindings, build_bindings = [], [], [], []
    bound_keys: set[tuple[str, str]] = set()
    for component in selected:
        sid = component["id"]
        binding = bindings.get(sid, {})
        if not component.get("deployable") or component.get("kind") != "service":
            question(
                "components." + sid,
                "This component needs an existing external resource binding or a supported provisioning adapter.",
                sid,
            )
            continue
        repository_id = str(component["repositoryId"])
        record = record_map.get(repository_id)
        if record is None:
            _fail("Service refers to a repository outside the source lock", "components." + sid)
        root = safe_relative(_value(component.get("root")) or ".", allow_dot=True)
        ports = [
            int(value)
            for item in component.get("ports", [])
            if type(value := _value(item)) is int and 1 <= value <= 65535
        ]
        port = binding.get("port")
        if port is not None and (type(port) is not int or not 1 <= port <= 65535):
            _fail("Port must be an integer from 1 to 65535", "serviceBindings." + sid)
        if port is None and len(set(ports)) == 1:
            port = ports[0]
        role = _value(component.get("role"))
        if port is None and role not in {"worker", "job", "cron"}:
            question(
                "components." + sid + ".port",
                "Choose the container listening port; source does not identify a unique port.",
                sid,
            )
        if role in {"job", "cron"}:
            question(
                "components." + sid + ".job",
                "Migration, one-shot and scheduled job adapters need explicit lifecycle configuration.",
                sid,
            )
        resources = _resources(
            binding.get("resources"), production=request.get("environment") == "production"
        )
        if resources is None:
            question(
                "components." + sid + ".resources",
                "Production requests and limits require explicit operator sizing or measured capacity.",
                sid,
            )
        replicas = binding.get("replicas", 1)
        if type(replicas) is not int or not 1 <= replicas <= 50:
            _fail("Replicas must be between 1 and 50", "serviceBindings." + sid)
        run_as_user = binding.get("runAsUser")
        if run_as_user is not None and (
            type(run_as_user) is not int or not 1 <= run_as_user <= 2_147_483_647
        ):
            _fail("runAsUser must be an explicit positive numeric UID", "serviceBindings." + sid)
        command_input = binding.get("startCommand")
        if isinstance(command_input, str) and command_input:
            command = None
            question(
                "components." + sid + ".startCommand",
                "Commit the shell start command in the image/source, or supply a direct exec array; proposed shell strings are not executor commands.",
                sid,
            )
        else:
            command = _direct_command(command_input)
        ingress = _ingress(sid, binding, request)
        if ingress and (
            not ingress.get("verified") or not ingress.get("className") or not ingress.get("tlsSecretName")
        ):
            question(
                "components." + sid + ".ingress",
                "Public DNS, ingress controller and existing TLS Secret need a verified ingress binding.",
                sid,
            )
        image = binding.get("imageReference")
        if image is not None and not _image_reference(image):
            _fail("Prebuilt images must be pinned by sha256 digest", "imageReference." + sid)
        entry = {
            "serviceId": sid,
            "repositoryId": repository_id,
            "sourceServiceId": component["sourceServiceId"],
            "name": _dns(sid),
            "root": root,
            "role": role,
            "port": port,
            "imageReference": image,
            "command": command,
            "replicas": replicas,
            "runAsUser": run_as_user,
            "resources": resources,
            "resourceBasis": "operator"
            if isinstance(binding.get("resources"), dict) and "limits" in binding["resources"]
            else "preview_policy",
            "ingress": ingress,
            "environment": [],
            "secretRefs": [],
            "evidenceRefs": copy.deepcopy(component.get("evidenceRefs", [])),
        }
        for item in binding.get("runtimeEnv", []):
            key, value = item["key"], item["value"]
            _literal(key, value)
            if key == "PORT" and port is not None and value != str(port):
                _fail("Runtime PORT must match the selected container listening port", "runtimeEnv." + sid)
            if (sid, key) in bound_keys:
                _fail("Environment keys must be unique per service", "runtimeEnv." + sid)
            bound_keys.add((sid, key))
            entry["environment"].append({"key": key, "value": value})
            runtime_bindings.append(
                {"serviceId": sid, "key": key, "value": value, "basis": "operator", "toServiceId": None}
            )
        for item in binding.get("secretRefs", []):
            key = item["key"]
            if key == "PORT":
                _fail(
                    "PORT must be a public literal matching the selected listening port", "secretRefs." + sid
                )
            if not isinstance(key, str) or not _ENV.fullmatch(key) or (sid, key) in bound_keys:
                _fail("Secret environment keys must be valid and unique", "secretRefs." + sid)
            if not _NAME.fullmatch(item.get("name", "")) or not re.fullmatch(
                r"[A-Za-z0-9._-]+", item.get("secretKey", "")
            ):
                _fail("Secret references require an existing Secret name and data key", "secretRefs." + sid)
            bound_keys.add((sid, key))
            entry["secretRefs"].append(copy.deepcopy(item))
        components.append(entry)
        if image is None:
            manifest = record.get("buildSourceManifest")
            files = {item["path"]: item for item in (manifest or {}).get("files", [])}
            context = safe_relative(binding.get("buildContext") or root, allow_dot=True)
            default_dockerfile = str(PurePosixPath(root) / "Dockerfile")
            dockerfile = binding.get("dockerfilePath") or (
                default_dockerfile if default_dockerfile in files else None
            )
            if dockerfile:
                dockerfile = safe_relative(dockerfile)
                if dockerfile not in files or (
                    context != "." and not dockerfile.startswith(context.rstrip("/") + "/")
                ):
                    _fail("Dockerfile must be in the locked build context", "dockerfilePath." + sid)
            builder = binding.get("builder") or ("dockerfile" if dockerfile else "railpack")
            if builder not in {"dockerfile", "railpack"}:
                _fail("Builder must be dockerfile or railpack", "builder." + sid)
            if not manifest:
                question(
                    "imageBuilds." + sid + ".source",
                    "Image builds require the original-byte staged source manifest.",
                    sid,
                )
            if builder == "dockerfile" and dockerfile is None:
                question(
                    "imageBuilds." + sid + ".dockerfile",
                    "Select a source Dockerfile or explicitly select Railpack.",
                    sid,
                )
            repository = binding.get("imageRepository")
            if repository is not None and (
                not isinstance(repository, str)
                or not _REPOSITORY.fullmatch(repository)
                or "@" in repository
                or ":" in repository.rsplit("/", 1)[-1]
                or any(p in {"", ".", ".."} for p in repository.split("/"))
            ):
                _fail(
                    "Image repository must be a registry repository without a tag", "imageRepository." + sid
                )
            if repository is None or "/" not in repository:
                question(
                    "imageBuilds." + sid + ".imageRepository",
                    "Select the registry repository to receive this image; cluster availability requires a registry push.",
                    sid,
                )
            if binding.get("buildCommand"):
                question(
                    "imageBuilds." + sid + ".buildCommand",
                    "Commit build command configuration in source; the executor does not run proposed shell overrides.",
                    sid,
                )
            arguments = []
            for item in binding.get("buildEnv", []):
                _literal(item["key"], item["value"])
                if any(argument["key"] == item["key"] for argument in arguments):
                    _fail("Build keys must be unique per service", "buildEnv." + sid)
                arguments.append(copy.deepcopy(item))
                build_bindings.append(
                    {
                        "serviceId": sid,
                        "key": item["key"],
                        "value": item["value"],
                        "basis": "operator",
                        "toServiceId": None,
                    }
                )
            builds.append(
                {
                    "serviceId": sid,
                    "repositoryId": repository_id,
                    "builder": builder,
                    "contextPath": context,
                    "dockerfilePath": dockerfile if builder == "dockerfile" else None,
                    "dockerfileSha256": files.get(dockerfile, {}).get("sha256")
                    if builder == "dockerfile"
                    else None,
                    "imageRepository": repository,
                    "imageTag": "iris-"
                    + digest(
                        {
                            "repositoryId": repository_id,
                            "commit": next(
                                item["commitSha"]
                                for item in repositories
                                if item["repositoryId"] == repository_id
                            ),
                            "serviceId": sid,
                        }
                    )[:24],
                    "platform": "linux/" + target["architecture"],
                    "arguments": arguments,
                    "timeoutSeconds": 900,
                }
            )
    component_map = {item["serviceId"]: item for item in components}
    explicit_connections = request.get("connectionBindings", [])
    connections = copy.deepcopy(graph.get("relationships", []))
    for override in explicit_connections:
        if (
            override.get("fromServiceId") not in all_components
            or override.get("toServiceId") not in all_components
        ):
            _fail("Connection binding refers to an unknown graph component", "connectionBindings")
        matches = [
            item
            for item in connections
            if item.get("fromServiceId") == override["fromServiceId"]
            and item.get("environmentKey") == override.get("environmentKey")
            and item.get("kind") == override.get("kind")
        ]
        for item in matches:
            item.update(
                copy.deepcopy(override), status="confirmed", reason="Explicit operator connection binding"
            )
        if not matches:
            connections.append(
                {
                    **copy.deepcopy(override),
                    "id": "connection-" + digest(override)[:16],
                    "status": "confirmed",
                    "evidenceRefs": [],
                    "reason": "Explicit operator connection binding",
                }
            )
    for relation in connections:
        sid, destination = relation.get("fromServiceId"), relation.get("toServiceId")
        if sid not in component_map:
            continue
        if relation.get("kind") == "package":
            continue  # Library links are build inputs, not deployable services.
        key, phase = relation.get("environmentKey"), relation.get("phase", "unknown")
        if key and (sid, key) in bound_keys and phase != "build":
            continue  # Operator values or existing Secrets intentionally override inference.
        if phase == "build" and any(
            item["serviceId"] == sid and any(argument["key"] == key for argument in item["arguments"])
            for item in builds
        ):
            continue
        if not key:
            resolved_sibling = any(
                item.get("fromServiceId") == sid
                and item.get("toServiceId") == destination
                and item.get("kind") == relation.get("kind")
                and item.get("status") in {"confirmed", "detected", "user_confirmed"}
                and item.get("environmentKey")
                and (
                    (sid, item["environmentKey"]) in bound_keys
                    or any(
                        build["serviceId"] == sid
                        and any(argument["key"] == item["environmentKey"] for argument in build["arguments"])
                        for build in builds
                    )
                )
                for item in connections
            )
            if resolved_sibling:
                continue
            if (
                relation.get("status") not in {"confirmed", "detected", "user_confirmed"}
                or destination not in selected_ids
            ):
                question(
                    "relationships." + relation["id"],
                    relation.get("reason") or "Resolve this required service relationship.",
                    sid,
                )
            continue
        if relation.get("status") not in {"confirmed", "detected", "user_confirmed"} or phase not in {
            "build",
            "runtime",
        }:
            question(
                "relationships." + relation["id"],
                "Confirm the consumer, provider and build/runtime phase before binding this environment key.",
                sid,
            )
            continue
        consumer, provider = component_map[sid], component_map.get(destination)
        endpoint = bindings.get(destination, {}).get("endpoint")
        if provider is None and endpoint and relation.get("kind") == "http":
            parsed = urlsplit(endpoint)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                _fail(
                    "Existing external endpoints require literal HTTP(S) URLs without credentials",
                    "endpoint." + str(destination),
                )
        elif provider is None or provider.get("port") is None:
            question(
                "relationships." + relation["id"],
                "Bind the external dependency through an existing Secret, or include its runnable provider service.",
                sid,
            )
            continue
        if is_secret_environment_key(key):
            question(
                "relationships." + relation["id"],
                "Credential connection strings require an existing Secret reference.",
                sid,
            )
            continue
        browser = consumer["role"] in {"static", "frontend", "web", "spa"}
        public = phase == "build" or browser
        if endpoint and provider is None:
            value = endpoint
            public = True
        elif public:
            if not provider.get("ingress") or not provider["ingress"].get("host"):
                question(
                    "relationships." + relation["id"],
                    "Browser/build-time URLs need the provider's public ingress hostname; internal cluster DNS is not browser reachable.",
                    sid,
                )
                continue
            value = "https://" + provider["ingress"]["host"]
        else:
            value = f"http://{provider['name']}.{target['namespace']}.svc.cluster.local:{provider['port']}"
        _literal(key, value)
        if phase == "runtime":
            bound_keys.add((sid, key))
        binding = {
            "serviceId": sid,
            "key": key,
            "value": value,
            "toServiceId": destination,
            "basis": "confirmed_relationship",
            "visibility": "public" if public else "cluster_internal",
        }
        if phase == "build":
            build = next((item for item in builds if item["serviceId"] == sid), None)
            if build is None:
                question(
                    "relationships." + relation["id"],
                    "A prebuilt consumer image cannot be changed to include this build-time URL; rebuild it or supply an explicitly verified image.",
                    sid,
                )
            else:
                build["arguments"].append({"key": key, "value": value})
                build_bindings.append(binding)
        else:
            consumer["environment"].append({"key": key, "value": value})
            runtime_bindings.append(binding)
    # Generic source requirements (for example SESSION_SECRET or DATABASE_URL)
    # are independent of inferred HTTP relationships. Keep their owning source
    # component and phase instead of treating repository-wide names as values
    # needed by every workload.
    from iris_analyzer.deployment.planner import _environment_owners
    from iris_analyzer.organization.graph import service_component_id

    for repository_id, record in sorted(record_map.items()):
        source_services = (record.get("analysis") or {}).get("services", [])
        selected_source_services = [
            service
            for service in source_services
            if service_component_id(repository_id, service["serviceId"]) in component_map
        ]
        if not selected_source_services:
            continue
        for observation in (record.get("readiness") or {}).get("environmentVariables", []):
            if observation.get("required") is False or observation.get("condition"):
                continue
            if observation.get("origin") == "example" and observation.get("required") is not True:
                continue
            key = observation["key"]
            phase = observation.get("phase", "unknown")
            owners = [
                service_component_id(repository_id, owner)
                for owner in _environment_owners(observation, source_services)
            ]
            selected_owners = [owner for owner in owners if owner in component_map]
            if owners and not selected_owners:
                continue
            if not owners:
                component_path = str(observation.get("component", ""))
                # Host-side Compose DB initialization and unrelated tooling do
                # not establish environment requirements of an application.
                if component_path.startswith("compose:"):
                    continue
                intersects = any(
                    component_path == root or root == "." or component_path.startswith(root.rstrip("/") + "/")
                    for service in selected_source_services
                    for root in service.get("componentRoots", []) or [_value(service.get("root")) or "."]
                )
                if intersects:
                    question(
                        "environment-owner:" + repository_id + ":" + key,
                        "Confirm the selected consumer service and build/runtime phase for required source environment key "
                        + key
                        + ".",
                    )
                continue
            for sid in selected_owners:
                component = component_map[sid]
                if phase == "runtime":
                    if key == "PORT" and component.get("port") is not None and (sid, key) not in bound_keys:
                        value = str(component["port"])
                        component["environment"].append({"key": key, "value": value})
                        runtime_bindings.append(
                            {
                                "serviceId": sid,
                                "key": key,
                                "value": value,
                                "basis": "confirmed_port",
                                "toServiceId": None,
                            }
                        )
                        bound_keys.add((sid, key))
                    if (sid, key) in bound_keys:
                        continue
                elif phase == "build":
                    if any(
                        build["serviceId"] == sid
                        and any(argument["key"] == key for argument in build["arguments"])
                        for build in builds
                    ):
                        continue
                question(
                    "environment:" + sid + ":" + phase + ":" + key,
                    "Bind required "
                    + phase
                    + " environment key "
                    + key
                    + (
                        " through an existing Secret reference."
                        if phase == "runtime" and is_secret_environment_key(key)
                        else "; build inputs require a source image build and cannot be supplied by runtime bindings."
                        if phase == "build"
                        else "; confirm its build/runtime phase before execution."
                        if phase == "unknown"
                        else " for this source service before execution."
                    ),
                    sid,
                )
    for item in graph.get("questions", []):
        sid = item.get("serviceId")
        if sid and sid not in selected_ids:
            continue
        key = item.get("environmentKey")
        if not key and item.get("key", "").startswith("environment:"):
            parts = item["key"].split(":", 2)
            if len(parts) == 3:
                sid, key = parts[1], parts[2]
        if key and (sid, key) in bound_keys:
            continue
        if key and any(
            build["serviceId"] == sid and any(argument["key"] == key for argument in build["arguments"])
            for build in builds
        ):
            continue
        # Relationship questions are handled with the operator-resolved graph
        # above. Other unresolved source/scope questions remain visible.
        if item.get("kind") in {"connection", "relationship", "dependency"}:
            continue
        question(item["key"], item["reason"], sid, required=item.get("required", True))
    tasks = [{"id": "namespace", "kind": "namespace", "dependsOn": []}]
    for build in builds:
        sid = build["serviceId"]
        tasks.extend(
            [
                {"id": "build:" + sid, "kind": "image_build", "serviceId": sid, "dependsOn": []},
                {"id": "push:" + sid, "kind": "image_push", "serviceId": sid, "dependsOn": ["build:" + sid]},
                {
                    "id": "image:" + sid,
                    "kind": "image_verify",
                    "serviceId": sid,
                    "dependsOn": ["push:" + sid],
                },
            ]
        )
    for item in components:
        sid = item["serviceId"]
        if not any(build["serviceId"] == sid for build in builds):
            tasks.append({"id": "image:" + sid, "kind": "image_verify", "serviceId": sid, "dependsOn": []})
        if item["secretRefs"] or item.get("ingress"):
            tasks.append(
                {
                    "id": "secrets:" + sid,
                    "kind": "secret_verify",
                    "serviceId": sid,
                    "dependsOn": ["namespace"],
                }
            )
    apply_dependencies = [
        item["id"] for item in tasks if item["kind"] in {"namespace", "image_verify", "secret_verify"}
    ]
    tasks.append({"id": "apply", "kind": "kubernetes_apply", "dependsOn": apply_dependencies})
    tasks.append({"id": "rollout", "kind": "rollout_verify", "dependsOn": ["apply"]})
    checks = []
    for check in request.get("httpChecks", []):
        # Probing is opt-in, limited to exact URLs included in later approval.
        parsed = urlsplit(check.get("url", ""))
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            _fail("HTTP checks require explicit credential-free HTTP(S) URLs", "httpChecks")
        if type(check.get("timeoutSeconds", 10)) is not int or not 1 <= check.get("timeoutSeconds", 10) <= 30:
            _fail("HTTP checks require a timeout between 1 and 30 seconds", "httpChecks")
        if (
            type(check.get("expectedStatus", 200)) is not int
            or not 100 <= check.get("expectedStatus", 200) <= 599
        ):
            _fail("HTTP checks require an explicit valid expected status", "httpChecks")
        checks.append(copy.deepcopy(check))
    if checks:
        tasks.append({"id": "http", "kind": "http_verify", "dependsOn": ["rollout"]})
    required = any(item["requiredForExecution"] for item in questions)
    status = "needs_input" if required else "build_required" if builds else "ready"
    plan = {
        "schemaVersion": "iris.system-plan.v1",
        "organization": request["organization"],
        "purpose": request.get("purpose"),
        "environment": request.get("environment", "preview"),
        "graphDigest": digest({key: value for key, value in graph.items() if key != "graphDigest"}),
        "requestDigest": digest(request),
        "request": copy.deepcopy(request),
        "target": target,
        "repositories": repositories,
        "components": components,
        "relationships": connections,
        "imageBuilds": builds,
        "runtimeBindings": runtime_bindings,
        "buildBindings": build_bindings,
        "tasks": tasks,
        "httpChecks": checks,
        "questions": questions,
        "status": status,
        "executionEligible": status == "ready",
        "buildEligible": status == "build_required",
        "executionAuthorized": False,
        "repositoryDossiers": _dossiers(records, request),
        "verification": {
            "status": "planned",
            "rollout": True,
            "http": bool(checks),
            "serviceConnectivity": "explicit_http_checks" if checks else "not_configured",
        },
        "limitations": [
            "Existing Kubernetes only: databases, queues, storage, network and cluster provisioning need explicit external resources or future adapters.",
            "Preview resource defaults are policy, not measured application capacity; production sizing must be explicitly bound.",
            "Runtime call cycles do not impose deployment ordering. Image/build and cluster prerequisites define the separate task DAG.",
            "Rollout checks establish Kubernetes readiness only. Functional and cross-service behavior require explicitly supplied HTTP checks.",
            "Source Dockerfile/Railpack builds execute trusted repository code after separate scoped authorization; mutable dependencies may change image bytes.",
            "Container UID follows the approved image unless runAsUser is explicitly supplied; the executor does not assume a nonroot-compatible image entrypoint.",
            "Applying this release does not run migrations or delete old resources, and failure is journaled without automatic database rollback.",
        ],
    }
    plan["planDigest"] = system_plan_digest(plan)
    validate_system_plan(plan)
    return plan


def validate_system_plan(plan: dict) -> dict:
    """Enforce the fixed executor contract as well as its content seal."""
    canonical_bytes(plan)
    if plan.get("schemaVersion") != "iris.system-plan.v1" or plan.get("planDigest") != system_plan_digest(
        plan
    ):
        _fail("System plan integrity validation failed")
    if plan.get("executionAuthorized") is not False:
        _fail("A plan cannot carry execution authorization")
    if plan.get("status") not in {"ready", "build_required", "needs_input"}:
        _fail("Unknown system plan status")
    if plan.get("requestDigest") != digest(plan.get("request")):
        _fail("Request digest differs from the sealed request")
    target = plan.get("target", {})
    if not _NAME.fullmatch(target.get("namespace", "")):
        _fail("Invalid namespace")
    components = plan.get("components", [])
    ids = {item["serviceId"] for item in components}
    names = {item["name"] for item in components}
    if len(ids) != len(components) or len(names) != len(components):
        _fail("Workload IDs and DNS names must be unique")
    repositories = {str(item["repositoryId"]): item for item in plan.get("repositories", [])}
    for repository in repositories.values():
        manifest = repository.get("buildSourceManifest")
        if manifest:
            if manifest.get("sourceManifestSha256") != digest(manifest.get("files", [])) or repository.get(
                "sourceManifestDigest"
            ) != manifest.get("sourceManifestSha256"):
                _fail("Source manifest digest failed")
            if manifest.get("origin", {}).get("revision") != repository.get("commitSha"):
                _fail("Source revision differs from its commit lock")
            paths = [item["path"] for item in manifest["files"]]
            if len(paths) != len(set(paths)):
                _fail("Source file paths must be unique")
            for path in paths:
                safe_relative(path)
    for component in components:
        sid = component["serviceId"]
        if str(component["repositoryId"]) not in repositories or not _NAME.fullmatch(component["name"]):
            _fail("Unknown workload repository or invalid DNS name", sid)
        safe_relative(component["root"], allow_dot=True)
        image = component.get("imageReference")
        if image is not None and not _image_reference(image):
            _fail("Images must be immutable references", sid)
        _direct_command(component.get("command"))
        uid = component.get("runAsUser")
        if uid is not None and (type(uid) is not int or not 1 <= uid <= 2_147_483_647):
            _fail("Explicit container UIDs must be positive bounded integers")
        _resources(component.get("resources"), production=plan["environment"] == "production")
        seen = set()
        for binding in component["environment"]:
            _literal(binding["key"], binding["value"])
            if binding["key"] in seen:
                _fail("Duplicate workload environment key", sid)
            seen.add(binding["key"])
        for binding in component["secretRefs"]:
            if binding["key"] in seen or not _ENV.fullmatch(binding["key"]):
                _fail("Duplicate or invalid Secret key", sid)
            seen.add(binding["key"])
            if not _NAME.fullmatch(binding["name"]) or not re.fullmatch(
                r"[A-Za-z0-9._-]+", binding["secretKey"]
            ):
                _fail("Invalid existing Secret reference", sid)
        ingress = component.get("ingress")
        if ingress and ingress.get("verified"):
            if (
                not _HOST.fullmatch(ingress.get("host", ""))
                or not _NAME.fullmatch(ingress.get("className", ""))
                or not _NAME.fullmatch(ingress.get("tlsSecretName", ""))
                or not ingress.get("path", "/").startswith("/")
            ):
                _fail("Invalid ingress configuration", sid)
    build_ids = set()
    for build in plan.get("imageBuilds", []):
        sid = build["serviceId"]
        if sid not in ids or sid in build_ids or build["builder"] not in {"dockerfile", "railpack"}:
            _fail("Invalid image build service or builder")
        build_ids.add(sid)
        safe_relative(build["contextPath"], allow_dot=True)
        if build.get("dockerfilePath"):
            safe_relative(build["dockerfilePath"])
        for binding in build["arguments"]:
            _literal(binding["key"], binding["value"])
        if len({item["key"] for item in build["arguments"]}) != len(build["arguments"]):
            _fail("Build argument keys must be unique")
        if build["platform"] not in {"linux/amd64", "linux/arm64"}:
            _fail("Unsupported image build platform")
        if type(build["timeoutSeconds"]) is not int or not 1 <= build["timeoutSeconds"] <= 3600:
            _fail("Unbounded image build timeout")
    tasks = plan.get("tasks", [])
    supported_tasks = {
        "namespace",
        "image_build",
        "image_push",
        "image_verify",
        "secret_verify",
        "kubernetes_apply",
        "rollout_verify",
        "http_verify",
    }
    for task in tasks:
        if task.get("kind") not in supported_tasks or (
            task.get("serviceId") is not None and task["serviceId"] not in ids
        ):
            _fail("Execution tasks must use fixed supported operations and known workloads")
    task_ids = {task["id"] for task in tasks}
    if len(task_ids) != len(tasks):
        _fail("Execution task IDs must be unique")
    completed = set()
    pending = list(tasks)
    while pending:
        ready = [task for task in pending if set(task.get("dependsOn", [])) <= completed]
        if not ready:
            _fail("Execution tasks must form a complete acyclic dependency graph")
        completed.update(task["id"] for task in ready)
        pending = [task for task in pending if task not in ready]
    required = any(item["requiredForExecution"] for item in plan["questions"])
    if plan["status"] != (
        "needs_input" if required else "build_required" if plan["imageBuilds"] else "ready"
    ):
        _fail("Status does not match unresolved prerequisites or image builds")
    if plan["executionEligible"] != (plan["status"] == "ready") or plan["buildEligible"] != (
        plan["status"] == "build_required"
    ):
        _fail("Eligibility flags must match the plan status")
    if plan["status"] in {"ready", "build_required"}:
        if (
            target.get("kind") != "existing_kubernetes"
            or not isinstance(target.get("context"), str)
            or not target["context"]
            or target["context"].startswith("-")
            or any(ord(c) < 32 for c in target["context"])
        ):
            _fail("Runnable plans require an explicit existing context")
        if target.get("architecture") not in {"amd64", "arm64"} or not components:
            _fail("Runnable plans require a supported architecture and workloads")
        for component in components:
            if component.get("imageReference") is None and component["serviceId"] not in build_ids:
                _fail("Missing images cannot be execution-ready")
            if component.get("resources") is None:
                _fail("Runnable workloads require resources")
        for build in plan["imageBuilds"]:
            repository = build.get("imageRepository")
            if (
                not isinstance(repository, str)
                or not _REPOSITORY.fullmatch(repository)
                or "/" not in repository
                or ":" in repository.rsplit("/", 1)[-1]
                or any(part in {"", ".", ".."} for part in repository.split("/"))
            ):
                _fail("Image builds require an explicit registry repository")
            source = repositories.get(str(build["repositoryId"]), {}).get("buildSourceManifest")
            if not source or (build["builder"] == "dockerfile" and not build.get("dockerfilePath")):
                _fail("Image builds require original source and selected Dockerfile")
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", build.get("imageTag", "")):
                _fail("Build image tags must be bounded literal identifiers")
            if build.get("dockerfilePath"):
                file = next(
                    (item for item in source["files"] if item["path"] == build["dockerfilePath"]), None
                )
                if not file or file["sha256"] != build.get("dockerfileSha256"):
                    _fail("Dockerfile digest differs from its locked source manifest")
                context = build["contextPath"]
                if context != "." and not build["dockerfilePath"].startswith(context.rstrip("/") + "/"):
                    _fail("Dockerfile must remain inside its approved build context")
        for repository_id in {str(component["repositoryId"]) for component in components}:
            repository = repositories[repository_id]
            if not isinstance(repository.get("commitSha"), str) or not re.fullmatch(
                r"[0-9a-f]{40}", repository["commitSha"]
            ):
                _fail("Runnable workloads require exact repository commit locks")
    for check in plan.get("httpChecks", []):
        parsed = urlsplit(check.get("url", ""))
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            _fail("HTTP verification URLs must be explicit and credential-free")
        if type(check.get("timeoutSeconds", 10)) is not int or not 1 <= check.get("timeoutSeconds", 10) <= 30:
            _fail("HTTP verification timeouts must be bounded")
        if (
            type(check.get("expectedStatus", 200)) is not int
            or not 100 <= check.get("expectedStatus", 200) <= 599
        ):
            _fail("HTTP verification statuses must be valid")
    return plan
