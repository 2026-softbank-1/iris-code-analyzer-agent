"""Organization requests and sealed outputs; no WAS dependency or implicit execution."""

from __future__ import annotations

import copy
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from ..contracts import AnalyzerError, canonical_bytes, digest
from ..deployment.contracts import is_secret_environment_key
from ..preprocess.redaction import redact

SERVICE_ID = {"type": "string", "minLength": 1, "maxLength": 128, "pattern": "^[a-z0-9-]+$"}
PUBLIC_ENV = {
    "type": "object",
    "additionalProperties": False,
    "required": ["key", "value"],
    "properties": {
        "key": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]{0,127}$"},
        "value": {"type": "string", "maxLength": 2048},
    },
}
SECRET_REF = {
    "type": "object",
    "additionalProperties": False,
    "required": ["key", "name", "secretKey"],
    "properties": {
        "key": PUBLIC_ENV["properties"]["key"],
        "name": {"type": "string", "pattern": "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", "maxLength": 253},
        "secretKey": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+$", "maxLength": 253},
    },
}
SERVICE_BINDING = {
    "type": "object",
    "additionalProperties": False,
    "required": ["serviceId"],
    "properties": {
        "serviceId": SERVICE_ID,
        "builder": {"enum": ["dockerfile", "railpack"]},
        "dockerfilePath": {"type": ["string", "null"], "maxLength": 512},
        "buildContext": {"type": "string", "maxLength": 512},
        "buildCommand": {"type": ["string", "null"], "maxLength": 2000},
        "startCommand": {
            "anyOf": [
                {"type": ["string", "null"], "maxLength": 2000},
                {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 2000},
                    "minItems": 1,
                    "maxItems": 30,
                },
            ]
        },
        "imageRepository": {"type": "string", "maxLength": 512},
        "imageReference": {"type": "string", "pattern": "^[^\\s@]+@sha256:[a-f0-9]{64}$"},
        "port": {"type": "integer", "minimum": 1, "maximum": 65535},
        "publicHost": {"type": "string", "maxLength": 253},
        "endpoint": {"type": "string", "maxLength": 2048},
        "replicas": {"type": "integer", "minimum": 1, "maximum": 20},
        "runAsUser": {"type": "integer", "minimum": 1, "maximum": 2147483647},
        "runtimeEnv": {"type": "array", "items": PUBLIC_ENV, "maxItems": 100},
        "buildEnv": {"type": "array", "items": PUBLIC_ENV, "maxItems": 100},
        "secretRefs": {"type": "array", "items": SECRET_REF, "maxItems": 100},
        "resources": {
            "type": "object",
            "additionalProperties": False,
            "required": ["requests", "limits"],
            "properties": {
                name: {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "cpuMillicores": {"type": "integer", "minimum": 10, "maximum": 128000},
                        "memoryMiB": {"type": "integer", "minimum": 16, "maximum": 262144},
                    },
                    "required": ["cpuMillicores", "memoryMiB"],
                }
                for name in ("requests", "limits")
            },
        },
    },
}
CONNECTION_BINDING = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fromServiceId", "toServiceId", "kind"],
    "properties": {
        "fromServiceId": SERVICE_ID,
        "toServiceId": SERVICE_ID,
        "kind": {"enum": ["http", "database", "queue", "storage", "package"]},
        "environmentKey": {"type": ["string", "null"], "pattern": "^[A-Za-z_][A-Za-z0-9_]{0,127}$"},
        "phase": {"enum": ["build", "runtime", "unknown"]},
    },
}
REQUEST_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["schemaVersion", "organization"],
    "properties": {
        "schemaVersion": {"const": "iris.organization-request.v1"},
        "organization": {"type": "string", "minLength": 1, "maxLength": 128},
        "purpose": {"type": ["string", "null"], "minLength": 1, "maxLength": 4000},
        "environment": {"enum": ["preview", "development", "production"]},
        "includeRepositories": {
            "type": "array",
            "items": {"type": "string"},
            "uniqueItems": True,
            "maxItems": 100,
        },
        "excludeRepositories": {
            "type": "array",
            "items": {"type": "string"},
            "uniqueItems": True,
            "maxItems": 100,
        },
        "refs": {
            "type": "object",
            "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "maxRepositories": {"type": "integer", "minimum": 1, "maximum": 100},
        "selectedServiceIds": {"type": "array", "items": SERVICE_ID, "uniqueItems": True, "maxItems": 100},
        "serviceBindings": {"type": "array", "items": SERVICE_BINDING, "maxItems": 100},
        "connectionBindings": {"type": "array", "items": CONNECTION_BINDING, "maxItems": 200},
        "target": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {"enum": ["existing_kubernetes", "aws_eks"]},
                "context": {"type": ["string", "null"], "maxLength": 253},
                "namespace": {
                    "type": "string",
                    "pattern": "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
                    "maxLength": 63,
                },
                "architecture": {"enum": ["amd64", "arm64"]},
            },
        },
        "deploymentRequest": {"type": ["object", "null"]},
        "httpChecks": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["url"],
                "properties": {
                    "url": {"type": "string", "maxLength": 2048},
                    "expectedStatus": {"type": "integer", "minimum": 100, "maximum": 599},
                    "timeoutSeconds": {"type": "integer", "minimum": 1, "maximum": 30},
                },
            },
        },
    },
}


def validate_request(document: dict) -> dict:
    canonical_bytes(document)
    error = next(Draft202012Validator(REQUEST_SCHEMA).iter_errors(document), None)
    if error:
        raise AnalyzerError(
            "ORGANIZATION_REQUEST_INVALID",
            "Organization request does not match v1",
            {"path": list(error.path), "reason": error.message},
        )
    result = copy.deepcopy(document)
    from .github import parse_organization

    result["organization"] = parse_organization(result["organization"])
    result.setdefault("purpose", None)
    result.setdefault("environment", "preview")
    for field in (
        "includeRepositories",
        "excludeRepositories",
        "selectedServiceIds",
        "serviceBindings",
        "connectionBindings",
    ):
        result.setdefault(field, [])
    result.setdefault("refs", {})
    result.setdefault("maxRepositories", 50)
    result.setdefault("httpChecks", [])
    target = result.setdefault("target", {})
    target.setdefault("kind", "existing_kubernetes")
    target.setdefault("context", None)
    target.setdefault("architecture", "amd64")
    slug = result["organization"].lower()
    target.setdefault("namespace", "iris-org-" + slug[:40])
    if redact(result.get("purpose") or "", "purpose.txt")[1]:
        raise AnalyzerError("ORGANIZATION_REQUEST_INVALID", "Purpose must not contain credentials")
    seen = set()
    for binding in result["serviceBindings"]:
        if binding["serviceId"] in seen:
            raise AnalyzerError("ORGANIZATION_REQUEST_INVALID", "Duplicate service bindings")
        seen.add(binding["serviceId"])
        for key in ("buildContext", "dockerfilePath"):
            path = binding.get(key)
            if path is not None and (
                not path
                or PurePosixPath(path).is_absolute()
                or ".." in path.split("/")
                or "\\" in path
                or any(ord(c) < 32 for c in path)
            ):
                raise AnalyzerError(
                    "ORGANIZATION_REQUEST_INVALID", "Build paths must stay inside the repository"
                )
        variables = set()
        for field in ("runtimeEnv", "buildEnv", "secretRefs"):
            for item in binding.get(field, []):
                marker = ("build" if field == "buildEnv" else "runtime", item["key"])
                if marker in variables:
                    raise AnalyzerError(
                        "ORGANIZATION_REQUEST_INVALID", "Environment bindings must be unique per phase"
                    )
                variables.add(marker)
                if field != "secretRefs" and (
                    is_secret_environment_key(item["key"])
                    or redact(item["value"], "binding.txt")[1]
                    or "$(" in item["value"]
                    or any(ord(c) < 32 for c in item["value"])
                ):
                    raise AnalyzerError(
                        "ORGANIZATION_REQUEST_INVALID",
                        "Public variables cannot contain credentials or implicit expansions",
                    )
        for key in ("endpoint", "publicHost"):
            value = binding.get(key)
            if value and (redact(value, "binding.txt")[1] or any(ord(c) < 32 for c in value)):
                raise AnalyzerError(
                    "ORGANIZATION_REQUEST_INVALID", "Endpoint metadata cannot contain credentials"
                )
        for key in ("buildCommand", "startCommand"):
            value = binding.get(key)
            pieces = value if isinstance(value, list) else [value] if isinstance(value, str) else []
            if any(redact(piece, "command.txt")[1] for piece in pieces):
                raise AnalyzerError(
                    "ORGANIZATION_REQUEST_INVALID", "Command overrides cannot contain credentials"
                )
    for key, value in result["refs"].items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", key) or any(ord(c) < 32 for c in value):
            raise AnalyzerError(
                "ORGANIZATION_REQUEST_INVALID", "Repository refs require full names and ordinary ref values"
            )
    for check in result["httpChecks"]:
        parsed = urlsplit(check["url"])
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or redact(check["url"], "binding.txt")[1]
        ):
            raise AnalyzerError(
                "ORGANIZATION_REQUEST_INVALID", "HTTP checks require explicit public URLs without credentials"
            )
    deployment = result.get("deploymentRequest")
    if deployment is not None:
        from ..deployment.planner import prepare_planning_request

        result["deploymentRequest"] = prepare_planning_request(deployment)
    return result


def seal_document(document: dict, field: str) -> dict:
    value = copy.deepcopy(document)
    value.pop(field, None)
    value[field] = digest(value)
    return value


def validate_seal(document: dict, field: str) -> None:
    if not isinstance(document, dict):
        raise AnalyzerError("ORGANIZATION_ARTIFACT_CHANGED", "Expected an Organization JSON object")
    value = dict(document)
    expected = value.pop(field, None)
    if not isinstance(expected, str) or digest(value) != expected:
        raise AnalyzerError(
            "ORGANIZATION_ARTIFACT_CHANGED", "Organization artifact digest does not match", {"field": field}
        )


def validate_repository_records(records: list[dict]) -> None:
    from ..contracts import validate_result
    from ..readiness import validate_readiness

    identities = set()
    names = set()
    for record in records:
        repository_id = record.get("repositoryId")
        if not isinstance(repository_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", repository_id):
            raise AnalyzerError(
                "ORGANIZATION_RECORD_INVALID", "Repository identity must be bounded and path-safe"
            )
        if repository_id in identities or record.get("fullName", "").lower() in names:
            raise AnalyzerError("ORGANIZATION_RECORD_INVALID", "Duplicate repository identity")
        identities.add(repository_id)
        names.add(record.get("fullName", "").lower())
        if record.get("status") != "analyzed":
            continue
        if not re.fullmatch(r"[a-f0-9]{40}", record.get("commitSha", "")):
            raise AnalyzerError("ORGANIZATION_RECORD_INVALID", "Analysis must refer to a pinned commit")
        analysis, readiness = record["analysis"], record["readiness"]
        validate_result(analysis)
        validate_readiness(readiness)
        if (
            analysis["sourceSnapshotId"] != record["sourceSnapshotId"]
            or readiness["sourceSnapshotId"] != record["sourceSnapshotId"]
        ):
            raise AnalyzerError(
                "ORGANIZATION_SOURCE_CHANGED", "Repository analysis and readiness snapshots differ"
            )
