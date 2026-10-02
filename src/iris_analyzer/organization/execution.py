"""Inspectable system bundles and an explicitly authorized local executor.

Only typed Docker/Railpack and existing-context kubectl operations are supported.
No command supplied by a model is passed to a host shell. The journal records a
mutation before starting it; an interrupted/unknown result requires operator
reconciliation instead of a blind retry. Compilation never invokes these tools.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

from iris_analyzer.build.source import safe_relative, verify_source
from iris_analyzer.contracts import AnalyzerError, digest

from .planner import validate_system_plan

_BUNDLE_VERSION = "iris.system-execution.v1"
_KINDS = {"Namespace", "Deployment", "Service", "Ingress"}


def _error(code: str, message: str) -> None:
    raise AnalyzerError(code, message)


def _sealed(value: dict, field: str) -> dict:
    value[field] = digest({key: item for key, item in value.items() if key != field})
    return value


def _metadata(name: str, namespace: str, plan_digest: str, service_id: str | None = None) -> dict:
    metadata = {
        "name": name,
        "namespace": namespace,
        "labels": {"app.kubernetes.io/managed-by": "iris-organization"},
        "annotations": {"iris.dev/system-plan-digest": plan_digest},
    }
    if service_id:
        metadata["labels"]["iris.dev/service-id"] = service_id
    return metadata


def _manifests(plan: dict, image_locks: dict[str, str] | None = None) -> list[dict]:
    """Fixed resources generated from the validated plan, never caller YAML."""
    namespace = plan["target"]["namespace"]
    manifests = []
    for component in plan["components"]:
        sid, name = component["serviceId"], component["name"]
        image = (image_locks or {}).get(sid) or component.get("imageReference")
        if not image:
            _error(
                "SYSTEM_IMAGE_UNRESOLVED", "Build and verify every image before generating runnable manifests"
            )
        selector = {"iris.dev/service-id": sid}
        resources = component["resources"]
        container = {
            "name": "app",
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
            "resources": {
                group: {
                    "cpu": str(resources[group]["cpuMillicores"]) + "m",
                    "memory": str(resources[group]["memoryMiB"]) + "Mi",
                }
                for group in ("requests", "limits")
            },
            "env": [{"name": item["key"], "value": item["value"]} for item in component["environment"]]
            + [
                {
                    "name": item["key"],
                    "valueFrom": {
                        "secretKeyRef": {"name": item["name"], "key": item["secretKey"], "optional": False}
                    },
                }
                for item in component["secretRefs"]
            ],
        }
        if component.get("command"):
            container["command"] = copy.deepcopy(component["command"])
        if component.get("port"):
            container["ports"] = [{"name": "app", "containerPort": component["port"], "protocol": "TCP"}]
            container["readinessProbe"] = {
                "tcpSocket": {"port": component["port"]},
                "periodSeconds": 5,
                "timeoutSeconds": 2,
                "failureThreshold": 12,
            }
        metadata = _metadata(name, namespace, plan["planDigest"], sid)
        manifests.append(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": metadata,
                "spec": {
                    "replicas": component["replicas"],
                    "revisionHistoryLimit": 5,
                    "progressDeadlineSeconds": 300,
                    "strategy": {
                        "type": "RollingUpdate",
                        "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
                    },
                    "selector": {"matchLabels": selector},
                    "template": {
                        "metadata": {
                            "labels": {**selector, "app.kubernetes.io/managed-by": "iris-organization"},
                            "annotations": {"iris.dev/system-plan-digest": plan["planDigest"]},
                        },
                        "spec": {
                            "automountServiceAccountToken": False,
                            "securityContext": {
                                "seccompProfile": {"type": "RuntimeDefault"},
                                **(
                                    {"runAsUser": component["runAsUser"], "runAsNonRoot": True}
                                    if component.get("runAsUser")
                                    else {}
                                ),
                            },
                            "nodeSelector": {"kubernetes.io/arch": plan["target"]["architecture"]},
                            "containers": [container],
                        },
                    },
                },
            }
        )
        if component.get("port"):
            manifests.append(
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": copy.deepcopy(metadata),
                    "spec": {
                        "type": "ClusterIP",
                        "selector": selector,
                        "ports": [
                            {
                                "name": "app",
                                "port": component["port"],
                                "targetPort": component["port"],
                                "protocol": "TCP",
                            }
                        ],
                    },
                }
            )
        ingress = component.get("ingress")
        if ingress:
            manifests.append(
                {
                    "apiVersion": "networking.k8s.io/v1",
                    "kind": "Ingress",
                    "metadata": copy.deepcopy(metadata),
                    "spec": {
                        "ingressClassName": ingress["className"],
                        "tls": [{"hosts": [ingress["host"]], "secretName": ingress["tlsSecretName"]}],
                        "rules": [
                            {
                                "host": ingress["host"],
                                "http": {
                                    "paths": [
                                        {
                                            "path": ingress.get("path", "/"),
                                            "pathType": "Prefix",
                                            "backend": {
                                                "service": {
                                                    "name": name,
                                                    "port": {"number": component["port"]},
                                                }
                                            },
                                        }
                                    ]
                                },
                            }
                        ],
                    },
                }
            )
    return manifests


def compile_system_plan(plan: dict) -> dict:
    """Compile ready images; retain a sealed build recipe for build-required plans."""
    validate_system_plan(plan)
    plan = copy.deepcopy(plan)
    blocked = [
        {"code": "SYSTEM_INPUT_REQUIRED", "path": item["key"], "message": item["reason"]}
        for item in plan["questions"]
        if item["requiredForExecution"]
    ]
    manifests = _manifests(plan) if plan["status"] == "ready" else []
    result = {
        "schemaVersion": _BUNDLE_VERSION,
        "templateVersion": _BUNDLE_VERSION,
        "planDigest": plan["planDigest"],
        "graphDigest": plan["graphDigest"],
        "target": copy.deepcopy(plan["target"]),
        "status": "blocked" if blocked else plan["status"],
        "executionEligible": plan["executionEligible"],
        "buildEligible": plan["buildEligible"],
        "executionAuthorized": False,
        "blockedReasons": blocked,
        "tasks": copy.deepcopy(plan["tasks"]),
        "imageBuilds": copy.deepcopy(plan["imageBuilds"]),
        "kubernetes": {
            "namespace": plan["target"]["namespace"],
            "context": plan["target"]["context"],
            "manifests": manifests,
        },
        "helm": {
            "chart": "iris-app",
            "chartVersion": "0.1.0",
            "values": {
                "namespace": plan["target"]["namespace"],
                "planDigest": plan["planDigest"],
                "resources": manifests,
            },
        }
        if manifests
        else None,
        "runtimeBindings": copy.deepcopy(plan["runtimeBindings"]),
        "buildBindings": copy.deepcopy(plan["buildBindings"]),
        "verification": copy.deepcopy(plan["verification"]),
        "artifacts": [],
        "limitations": copy.deepcopy(plan["limitations"]),
    }
    return _sealed(result, "bundleDigest")


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _copy_chart(source, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            _copy_chart(child, target)
        elif child.name.endswith((".yaml", ".json")):
            target.write_bytes(child.read_bytes())


def write_system_bundle(plan: dict, out: str | Path) -> dict:
    """Write to a new directory; blocked plans never leave old runnable files."""
    result = compile_system_plan(plan)
    destination = Path(out)
    if destination.is_symlink() or (
        destination.exists() and (not destination.is_dir() or any(destination.iterdir()))
    ):
        _error("SYSTEM_OUTPUT_NOT_EMPTY", "System bundle destination must be a new or empty directory")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        _write_json(destination / "system-plan.json", plan)
        if result["status"] == "ready":
            _copy_chart(
                files("iris_analyzer.deployment").joinpath("templates", "helm", "iris-app"),
                destination / "helm" / "iris-app",
            )
            (destination / "helm" / "values.yaml").write_text(
                yaml.safe_dump(result["helm"]["values"], sort_keys=False), encoding="utf-8"
            )
            (destination / "manifests.yaml").write_text(
                yaml.safe_dump_all(result["kubernetes"]["manifests"], sort_keys=False), encoding="utf-8"
            )
        result["artifacts"] = [
            {
                "path": item.relative_to(destination).as_posix(),
                "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
            }
            for item in sorted(destination.rglob("*"))
            if item.is_file()
        ]
        _sealed(result, "bundleDigest")
        _write_json(destination / "execution.json", result)
    except (OSError, UnicodeError):
        shutil.rmtree(destination)
        _error("SYSTEM_WRITE_FAILED", "System bundle could not be written")
    return result


def _load_bundle(destination: Path) -> tuple[dict, dict]:
    if destination.is_symlink() or not destination.is_dir():
        _error("SYSTEM_BUNDLE_INVALID", "Select an existing regular bundle directory")
    destination = destination.resolve()
    for path in destination.rglob("*"):
        if path.is_symlink():
            _error("SYSTEM_BUNDLE_INVALID", "Bundle artifacts cannot be symbolic links")
    try:
        plan = json.loads((destination / "system-plan.json").read_text(encoding="utf-8"))
        bundle = json.loads((destination / "execution.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AnalyzerError("SYSTEM_BUNDLE_INVALID", "System bundle JSON cannot be read") from exc
    validate_system_plan(plan)
    if bundle.get("bundleDigest") != digest(
        {key: value for key, value in bundle.items() if key != "bundleDigest"}
    ):
        _error("SYSTEM_BUNDLE_CHANGED", "System bundle digest does not match")
    expected = compile_system_plan(plan)
    if {key: value for key, value in bundle.items() if key not in {"artifacts", "bundleDigest"}} != {
        key: value for key, value in expected.items() if key not in {"artifacts", "bundleDigest"}
    }:
        _error("SYSTEM_BUNDLE_CHANGED", "System bundle differs from the fixed plan compiler")
    artifact_paths = set()
    for artifact in bundle["artifacts"]:
        relative = safe_relative(artifact["path"])
        artifact_paths.add(relative)
        path = destination / relative
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
            _error("SYSTEM_BUNDLE_CHANGED", "A compiled artifact no longer matches its hash")
    required = {"system-plan.json"}
    if bundle["status"] == "ready":
        required.update(
            {
                "manifests.yaml",
                "helm/values.yaml",
                "helm/iris-app/Chart.yaml",
                "helm/iris-app/templates/resources.yaml",
            }
        )
    if not required <= artifact_paths:
        _error("SYSTEM_BUNDLE_CHANGED", "Required artifacts are missing from the bundle seal")
    return plan, bundle


def _validate_authorization(authorization: dict | None, plan: dict, bundle: dict) -> dict:
    if not isinstance(authorization, dict):
        _error("SYSTEM_AUTHORIZATION_REQUIRED", "Applying a system requires explicit scoped authorization")
    target = plan["target"]
    for key, expected in (
        ("planDigest", plan["planDigest"]),
        ("bundleDigest", bundle["bundleDigest"]),
        ("context", target["context"]),
        ("namespace", target["namespace"]),
    ):
        if authorization.get(key) != expected:
            _error(
                "SYSTEM_AUTHORIZATION_SCOPE",
                "Authorization must match the reviewed plan, bundle, context and namespace",
            )
    if authorization.get("allowDeploy") is not True:
        _error(
            "SYSTEM_AUTHORIZATION_REQUIRED",
            "This executor requires explicit permission for cluster deployment",
        )
    if plan["imageBuilds"] and (
        authorization.get("allowBuilds") is not True or authorization.get("allowPushes") is not True
    ):
        _error(
            "SYSTEM_BUILD_AUTHORIZATION_REQUIRED",
            "Source builds and registry pushes require separate explicit permission",
        )
    urls = authorization.get("approvedHttpUrls", [])
    if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls):
        _error("SYSTEM_AUTHORIZATION_SCOPE", "HTTP approval must contain exact URLs")
    for check in plan["httpChecks"]:
        if check.get("url") not in urls:
            _error(
                "SYSTEM_HTTP_AUTHORIZATION_REQUIRED",
                "Each HTTP check requires explicit approval of its exact URL",
            )
    return authorization


def _tool_environment() -> dict:
    # Source-controlled environment variables do not enter the host process.
    # Credentials remain in Docker/kubectl files owned by this local operator.
    keys = (
        "PATH",
        "HOME",
        "DOCKER_CONFIG",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "KUBECONFIG",
        "BUILDKIT_HOST",
        "AWS_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    )
    return {key: os.environ[key] for key in keys if key in os.environ}


def _run(argv: list[str], *, timeout: int = 120, json_output: bool = False) -> object:
    """Bounded timeout, no shell, no output secrets exposed in errors/journals."""
    executable = shutil.which(argv[0])
    if executable is None:
        _error("SYSTEM_TOOL_UNAVAILABLE", "Required local executable is unavailable: " + argv[0])
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            [executable, *argv[1:]],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=_tool_environment(),
            timeout=timeout,
            check=False,
            shell=False,
        )
        if result.returncode:
            _error(
                "SYSTEM_TOOL_FAILED",
                "Typed executor operation failed with exit code " + str(result.returncode),
            )
        if not json_output:
            return None
        output.seek(0)
        data = output.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            _error("SYSTEM_TOOL_OUTPUT_INVALID", "Tool JSON exceeded the bounded output limit")
        try:
            return json.loads(data)
        except (ValueError, UnicodeError) as exc:
            raise AnalyzerError("SYSTEM_TOOL_OUTPUT_INVALID", "Typed executor expected JSON output") from exc


def _kubectl(plan: dict, *arguments: str) -> list[str]:
    return [
        "kubectl",
        "--context",
        plan["target"]["context"],
        "--namespace",
        plan["target"]["namespace"],
        *arguments,
    ]


def _journal_write(path: Path, journal: dict) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(journal, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _operation(journal: dict, path: Path, task_id: str, action, *, mutation: bool) -> object:
    previous = next((item for item in journal["operations"] if item["taskId"] == task_id), None)
    if previous and previous["status"] == "complete":
        return previous.get("result")
    if previous:
        _error(
            "SYSTEM_OPERATION_RECONCILIATION_REQUIRED",
            "A previous operation needs explicit operator reconciliation before resuming: " + task_id,
        )
    operation = {
        "operationId": digest({"planDigest": journal["planDigest"], "taskId": task_id})[:32],
        "taskId": task_id,
        "status": "running",
        "mutation": mutation,
    }
    journal["operations"].append(operation)
    _journal_write(path, journal)
    try:
        result = action()
    except (Exception, KeyboardInterrupt) as exc:
        operation["status"] = "unknown_outcome" if mutation else "failed"
        operation["reasonCode"] = getattr(exc, "code", "SYSTEM_OPERATION_INTERRUPTED")
        journal["status"] = "needs_reconciliation"
        _journal_write(path, journal)
        raise
    operation["status"] = "complete"
    if result is not None:
        operation["result"] = result
    _journal_write(path, journal)
    return result


def _source_roots(plan: dict, provided: dict[str, str | Path] | None) -> dict[str, Path]:
    roots = {}
    repositories = {item["repositoryId"]: item for item in plan["repositories"]}
    for build in plan["imageBuilds"]:
        identifier = build["repositoryId"]
        if identifier not in (provided or {}):
            _error(
                "SYSTEM_BUILD_SOURCE_REQUIRED",
                "Provide the staged original-byte source root for each build repository",
            )
        root = Path(provided[identifier])
        if root.is_symlink() or not root.is_dir():
            _error(
                "SYSTEM_BUILD_SOURCE_INVALID",
                "Build roots must be existing directories without symbolic links",
            )
        root = root.resolve()
        verify_source(root, repositories[identifier]["buildSourceManifest"])
        context = root / safe_relative(build["contextPath"], allow_dot=True)
        if not context.is_dir() or not context.resolve().is_relative_to(root):
            _error(
                "SYSTEM_BUILD_SOURCE_INVALID", "Build context must exist inside its locked repository source"
            )
        dockerfile = build.get("dockerfilePath")
        if dockerfile:
            path = root / safe_relative(dockerfile)
            if (
                not path.is_file()
                or not path.resolve().is_relative_to(context.resolve())
                or hashlib.sha256(path.read_bytes()).hexdigest() != build["dockerfileSha256"]
            ):
                _error(
                    "SYSTEM_BUILD_SOURCE_CHANGED", "Selected Dockerfile differs from the approved source file"
                )
        roots[identifier] = root
    return roots


def _build_image(build: dict, root: Path, manifest: dict) -> None:
    verify_source(root, manifest)
    reference = build["imageRepository"] + ":" + build["imageTag"]
    context = str(root / build["contextPath"])
    if build["builder"] == "dockerfile":
        argv = [
            "docker",
            "build",
            "--platform",
            build["platform"],
            "--tag",
            reference,
            "--file",
            str(root / build["dockerfilePath"]),
        ]
        for argument in build["arguments"]:
            argv.extend(["--build-arg", argument["key"] + "=" + argument["value"]])
        argv.append(context)
    else:
        # CLI contract: https://railpack.com/reference/cli. BuildKit must be
        # preconfigured by the operator; this executor never starts privileged
        # build daemons or installs tools automatically.
        argv = ["railpack", "build", "--name", reference, "--platform", build["platform"]]
        for argument in build["arguments"]:
            argv.extend(["--env", argument["key"] + "=" + argument["value"]])
        argv.append(context)
    _run(argv, timeout=build["timeoutSeconds"])
    verify_source(root, manifest)


def _inspect_image(reference: str, architecture: str, *, pull: bool = True) -> str:
    if pull:
        _run(["docker", "pull", "--platform", "linux/" + architecture, reference], timeout=600)
    document = _run(["docker", "image", "inspect", "--format", "{{json .}}", reference], json_output=True)
    if (
        not isinstance(document, dict)
        or document.get("Architecture") != architecture
        or document.get("Os") != "linux"
    ):
        _error(
            "SYSTEM_IMAGE_PLATFORM_INVALID", "Image inspection did not verify the approved Linux architecture"
        )
    if reference not in document.get("RepoDigests", []):
        _error(
            "SYSTEM_IMAGE_DIGEST_INVALID",
            "Registry digest inspection did not match the approved image reference",
        )
    return reference


def _push_and_resolve(build: dict, architecture: str) -> str:
    repository = build["imageRepository"]
    reference = repository + ":" + build["imageTag"]
    _run(["docker", "push", reference], timeout=600)
    candidates = _run(
        ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", reference], json_output=True
    )
    references = (
        [item for item in candidates if isinstance(item, str) and item.startswith(repository + "@sha256:")]
        if isinstance(candidates, list)
        else []
    )
    if len(references) != 1:
        _error("SYSTEM_IMAGE_DIGEST_INVALID", "Pushed image did not resolve to one immutable registry digest")
    return _inspect_image(references[0], architecture)


def _namespace(plan: dict, directory: Path) -> None:
    document = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": plan["target"]["namespace"]}}
    path = directory / "namespace.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    _run(_kubectl(plan, "apply", "--server-side", "--field-manager=iris-organization", "-f", str(path)))


def _check_secrets(plan: dict, component: dict) -> dict:
    requirements: dict[str, set[str]] = {}
    for binding in component["secretRefs"]:
        requirements.setdefault(binding["name"], set()).add(binding["secretKey"])
    if component.get("ingress"):
        requirements.setdefault(component["ingress"]["tlsSecretName"], set()).update({"tls.crt", "tls.key"})
    verified = []
    for name, keys in sorted(requirements.items()):
        secret = _run(_kubectl(plan, "get", "secret", name, "-o", "json"), json_output=True)
        if not isinstance(secret, dict) or not keys <= set(secret.get("data", {})):
            _error(
                "SYSTEM_SECRET_KEY_REQUIRED", "An existing Secret does not contain every required data key"
            )
        # Only existence and key names enter the journal; never literal values,
        # base64 contents or hashes of low entropy secrets.
        verified.append({"name": name, "keys": sorted(keys)})
    return {"verified": verified}


def _apply(plan: dict, manifest_path: Path) -> None:
    _run(
        _kubectl(
            plan, "apply", "--server-side", "--field-manager=iris-organization", "-f", str(manifest_path)
        ),
        timeout=300,
    )


def _rollout(plan: dict) -> None:
    for component in plan["components"]:
        _run(
            _kubectl(plan, "rollout", "status", "deployment/" + component["name"], "--timeout=300s"),
            timeout=330,
        )


def _http_checks(plan: dict) -> dict:
    results = []
    with httpx.Client(follow_redirects=False, trust_env=False) as client:
        for check in plan["httpChecks"]:
            url = check["url"]
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                _error(
                    "SYSTEM_HTTP_CHECK_INVALID",
                    "Checks require exact HTTP(S) URLs without credentials or fragments",
                )
            timeout = check.get("timeoutSeconds", 10)
            expected = check.get("expectedStatus", 200)
            if (
                type(timeout) is not int
                or not 1 <= timeout <= 30
                or type(expected) is not int
                or not 100 <= expected <= 599
            ):
                _error(
                    "SYSTEM_HTTP_CHECK_INVALID",
                    "HTTP checks require bounded timeouts and explicit status codes",
                )
            # A streamed response verifies status without collecting arbitrary
            # response bodies, credentials or unbounded output.
            with client.stream("GET", url, timeout=timeout) as response:
                if response.status_code != expected:
                    _error(
                        "SYSTEM_HTTP_CHECK_FAILED",
                        "A supplied connectivity URL did not return its expected status",
                    )
                results.append({"url": url, "statusCode": response.status_code})
    return {"checks": results}


def execute_system_bundle(
    bundle_path: str | Path,
    *,
    authorization: dict | None = None,
    source_roots: dict[str, str | Path] | None = None,
    dry_run: bool = True,
) -> dict:
    """Inspect by default; apply only the exact authorized reviewed bundle.

    Source roots are local operator mappings, not paths chosen by analyzed code.
    The original plan stays immutable when builds resolve image digests. Actual
    images and generated manifests become a separately sealed system release.
    """
    directory = Path(bundle_path)
    plan, bundle = _load_bundle(directory)
    summary = {
        "schemaVersion": "iris.system-run.v1",
        "planDigest": plan["planDigest"],
        "bundleDigest": bundle["bundleDigest"],
        "dryRun": dry_run,
        "executionAuthorized": False,
        "status": "blocked" if bundle["status"] == "blocked" else "dry_run",
        "target": copy.deepcopy(plan["target"]),
        "tasks": copy.deepcopy(plan["tasks"]),
        "blockedReasons": copy.deepcopy(bundle["blockedReasons"]),
    }
    if dry_run:
        return summary
    if bundle["status"] == "blocked":
        _error("SYSTEM_PLAN_NOT_READY", "Resolve every required system-plan input before applying")
    _validate_authorization(authorization, plan, bundle)
    roots = _source_roots(plan, source_roots)
    directory = directory.resolve()
    state_directory = directory / "run"
    if state_directory.is_symlink():
        _error("SYSTEM_RUN_PATH_INVALID", "Run state cannot be a symbolic link")
    state_directory.mkdir(exist_ok=True, mode=0o700)
    if any(path.is_symlink() for path in state_directory.rglob("*")):
        _error("SYSTEM_RUN_PATH_INVALID", "Run state cannot contain symbolic links")
    lock_path = state_directory / "executor.lock"
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise AnalyzerError(
            "SYSTEM_RUN_LOCKED",
            "Another executor or an interrupted run owns this bundle; reconcile its state first",
        ) from exc
    os.close(descriptor)
    journal_path = state_directory / "journal.json"
    try:
        if journal_path.is_symlink():
            _error("SYSTEM_RUN_PATH_INVALID", "Journal cannot be a symbolic link")
        if journal_path.exists():
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            if (
                journal.get("planDigest") != plan["planDigest"]
                or journal.get("bundleDigest") != bundle["bundleDigest"]
            ):
                _error("SYSTEM_RUN_CHANGED", "Journal belongs to a different plan or bundle")
            if any(item["status"] != "complete" for item in journal.get("operations", [])):
                _error(
                    "SYSTEM_OPERATION_RECONCILIATION_REQUIRED",
                    "Interrupted or failed operations require explicit reconciliation before resume",
                )
        else:
            journal = {
                "schemaVersion": "iris.system-journal.v1",
                "planDigest": plan["planDigest"],
                "bundleDigest": bundle["bundleDigest"],
                "status": "running",
                "operations": [],
            }
            _journal_write(journal_path, journal)
        repositories = {item["repositoryId"]: item for item in plan["repositories"]}
        image_locks = {}
        for component in plan["components"]:
            sid = component["serviceId"]
            build = next((item for item in plan["imageBuilds"] if item["serviceId"] == sid), None)
            if build:
                _operation(
                    journal,
                    journal_path,
                    "build:" + sid,
                    lambda build=build: _build_image(
                        build,
                        roots[build["repositoryId"]],
                        repositories[build["repositoryId"]]["buildSourceManifest"],
                    ),
                    mutation=True,
                )
                image_locks[sid] = _operation(
                    journal,
                    journal_path,
                    "push:" + sid,
                    lambda build=build: _push_and_resolve(build, plan["target"]["architecture"]),
                    mutation=True,
                )
                # The published digest, never the mutable build tag, becomes
                # the deployment identity and is checked again on every resume.
                _operation(
                    journal,
                    journal_path,
                    "image:" + sid,
                    lambda sid=sid: _inspect_image(image_locks[sid], plan["target"]["architecture"]),
                    mutation=False,
                )
            else:
                image_locks[sid] = _operation(
                    journal,
                    journal_path,
                    "image:" + sid,
                    lambda component=component: _inspect_image(
                        component["imageReference"], plan["target"]["architecture"]
                    ),
                    mutation=False,
                )
        manifests = _manifests(plan, image_locks)
        if any(document["kind"] not in _KINDS for document in manifests):
            _error("SYSTEM_RESOURCE_UNSUPPORTED", "Release contains a resource outside the fixed adapter")
        release = _sealed(
            {
                "schemaVersion": "iris.system-release.v1",
                "planDigest": plan["planDigest"],
                "bundleDigest": bundle["bundleDigest"],
                "target": copy.deepcopy(plan["target"]),
                "images": image_locks,
                "manifestDigest": digest(manifests),
                "sourceLocks": [
                    {key: value for key, value in item.items() if key != "buildSourceManifest"}
                    for item in plan["repositories"]
                ],
                "verification": copy.deepcopy(plan["verification"]),
                "executionAuthorized": True,
            },
            "releaseDigest",
        )
        release_path = state_directory / "release.json"
        manifest_path = state_directory / "manifests.yaml"
        if release_path.is_symlink() or manifest_path.is_symlink():
            _error("SYSTEM_RUN_PATH_INVALID", "Release artifacts cannot be symbolic links")
        if release_path.exists():
            old_release = json.loads(release_path.read_text(encoding="utf-8"))
            if old_release != release:
                _error(
                    "SYSTEM_RELEASE_CHANGED",
                    "Resolved release differs from the previously journaled images or manifests",
                )
        _write_json(release_path, release)
        manifest_path.write_text(yaml.safe_dump_all(manifests, sort_keys=False), encoding="utf-8")
        manifest_path.chmod(0o600)
        _operation(
            journal, journal_path, "namespace", lambda: _namespace(plan, state_directory), mutation=True
        )
        for component in plan["components"]:
            if component["secretRefs"] or component.get("ingress"):
                _operation(
                    journal,
                    journal_path,
                    "secrets:" + component["serviceId"],
                    lambda component=component: _check_secrets(plan, component),
                    mutation=False,
                )
        # Recheck immutable bundle artifacts immediately before the first
        # workload mutation; deployment consumes freshly generated fixed YAML.
        _load_bundle(directory)
        _operation(journal, journal_path, "apply", lambda: _apply(plan, manifest_path), mutation=True)
        _operation(journal, journal_path, "rollout", lambda: _rollout(plan), mutation=False)
        if plan["httpChecks"]:
            _operation(journal, journal_path, "http", lambda: _http_checks(plan), mutation=False)
        journal["status"] = "complete"
        journal["releaseDigest"] = release["releaseDigest"]
        _journal_write(journal_path, journal)
        summary.update(
            status="complete",
            executionAuthorized=True,
            release=release,
            operations=copy.deepcopy(journal["operations"]),
        )
        summary["verification"] = {
            "rollout": "passed",
            "httpChecks": "passed" if plan["httpChecks"] else "not_configured",
            "checkedUrlCount": len(plan["httpChecks"]),
        }
        return summary
    finally:
        lock_path.unlink(missing_ok=True)


def reconcile_system_operation(
    bundle_path: str | Path,
    operation_id: str,
    *,
    authorization: dict,
    resolution: str,
    image_reference: str | None = None,
) -> dict:
    """Record an explicit operator decision after checking an unknown outcome.

    ``retry`` permits that exact fixed operation to run again. ``complete``
    records the operator's observation; a completed push/inspect also requires
    its exact pinned registry image. This function executes no source/cloud
    tools. It cannot reconcile a currently locked process; stopped-process lock
    recovery is deliberately a local operator action.
    """
    directory = Path(bundle_path)
    plan, bundle = _load_bundle(directory)
    _validate_authorization(authorization, plan, bundle)
    state_directory = directory.resolve() / "run"
    if state_directory.is_symlink() or any(path.is_symlink() for path in state_directory.rglob("*")):
        _error("SYSTEM_RUN_PATH_INVALID", "Run state cannot contain symbolic links")
    lock_path = state_directory / "executor.lock"
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise AnalyzerError(
            "SYSTEM_RUN_LOCKED",
            "Reconciliation requires the executor to be stopped and its process lock released",
        ) from exc
    os.close(descriptor)
    try:
        path = state_directory / "journal.json"
        journal = json.loads(path.read_text(encoding="utf-8"))
        if (
            journal.get("planDigest") != plan["planDigest"]
            or journal.get("bundleDigest") != bundle["bundleDigest"]
        ):
            _error("SYSTEM_RUN_CHANGED", "Journal belongs to a different plan or bundle")
        operation = next(
            (item for item in journal["operations"] if item["operationId"] == operation_id), None
        )
        if operation is None or operation["status"] not in {"running", "unknown_outcome", "failed"}:
            _error("SYSTEM_RECONCILIATION_INVALID", "Select one unresolved operation from this journal")
        if resolution not in {"retry", "complete"}:
            _error(
                "SYSTEM_RECONCILIATION_INVALID", "Choose an explicit retry or observed complete resolution"
            )
        result = None
        if resolution == "complete" and operation["taskId"].startswith(("image:", "push:")):
            sid = operation["taskId"].split(":", 1)[1]
            component = next(item for item in plan["components"] if item["serviceId"] == sid)
            build = next((item for item in plan["imageBuilds"] if item["serviceId"] == sid), None)
            expected_repository = (
                build["imageRepository"] if build else component["imageReference"].split("@", 1)[0]
            )
            if not isinstance(image_reference, str) or not re.fullmatch(
                re.escape(expected_repository) + r"@sha256:[0-9a-f]{64}", image_reference
            ):
                _error(
                    "SYSTEM_RECONCILIATION_INVALID",
                    "An observed image operation requires the exact approved repository and digest",
                )
            if build is None and image_reference != component["imageReference"]:
                _error(
                    "SYSTEM_RECONCILIATION_INVALID",
                    "Prebuilt image resolution must preserve its original approved digest",
                )
            result = image_reference
        history = journal.setdefault("reconciliations", [])
        history.append(
            {"operation": copy.deepcopy(operation), "resolution": resolution, "imageReference": result}
        )
        if resolution == "retry":
            journal["operations"].remove(operation)
        else:
            operation["status"] = "complete"
            operation.pop("reasonCode", None)
            if result:
                operation["result"] = result
        journal["status"] = "ready_to_resume"
        _journal_write(path, journal)
        return {
            "schemaVersion": "iris.system-reconciliation.v1",
            "planDigest": plan["planDigest"],
            "operationId": operation_id,
            "resolution": resolution,
            "status": "ready_to_resume",
        }
    finally:
        lock_path.unlink(missing_ok=True)
