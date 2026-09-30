"""Ground model proposals in the immutable preprocessing observations.

The model may suggest interpretations, but only the static extractor can produce
detected values. Completeness is recomputed here, never trusted from the model.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any, Iterable

from .contracts import (
    AnalyzerError,
    canonical_bytes,
    digest,
    validate_bundle,
    validate_reply,
    validate_result,
)

_SERVICE_KEYS = {
    "runtime": "runtime.name",
    "buildCommand": "build.command",
    "startCommand": "start.command",
    "workingDirectory": "docker.workdir",
    "outputDirectory": "output.directory",
    "ports": "runtime.port",
    "healthchecks": "healthcheck.path",
}
_COLLECTION_KEYS = {
    "dependencies": {"dependency.database", "dependency.volume"},
    "apiRoutes": {"api.route"},
    "environmentKeys": {"environment.key"},
    "connections": {"frontend.connection"},
}
_SCOPE_ORDER = {"container": 0, "production": 1, "source": 2, "development": 3, "host_mapping": 4}
_SUPPORTED_ROLES = {"api", "server", "web_api", "static", "web", "frontend"}
_STATIC_ROLES = {"static", "frontend"}


def _error(code: str, message: str, **details: Any) -> None:
    raise AnalyzerError(code, message, details or None)


def _relative_path(path: str, *, root: bool = False) -> bool:
    if root and path == ".":
        return True
    parts = PurePosixPath(path).parts
    return bool(path and parts) and not (
        path.startswith("/")
        or "\\" in path
        or ":" in path
        or any(part in {".", ".."} for part in parts)
        or str(PurePosixPath(path)) != path
    )


def _unique_index(items: list[dict], key: str) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for item in items:
        value = item[key]
        if value in index:
            _error("RESULT_EVIDENCE_INVALID", "Duplicate identity in context bundle", key=key, value=value)
        index[value] = item
    return index


def _references(value: Any, evidence: dict[str, dict], manifests: dict[str, dict]) -> None:
    """Inspect every nested reference, including arbitrary Field.value objects."""
    if isinstance(value, list):
        for item in value:
            _references(item, evidence, manifests)
    elif isinstance(value, dict):
        if "evidenceIds" in value:
            ids = value["evidenceIds"]
            if not isinstance(ids, list) or any(not isinstance(x, str) or x not in evidence for x in ids):
                _error("RESULT_EVIDENCE_INVALID", "A result references evidence that was not provided")
        if "evidenceId" in value:
            item = evidence.get(value["evidenceId"]) if isinstance(value["evidenceId"], str) else None
            if item is None:
                _error("RESULT_EVIDENCE_INVALID", "A nested evidence reference does not exist")
            for name in ("path", "sourceDigest", "contentDigest", "startLine", "endLine"):
                if name in value and value[name] != item[name]:
                    _error(
                        "RESULT_EVIDENCE_INVALID",
                        "A nested evidence reference changes its source",
                        field=name,
                    )
        if "sourceDigest" in value:
            path = value.get("path")
            manifest = manifests.get(path) if isinstance(path, str) else None
            if manifest is None or value["sourceDigest"] != manifest["digest"]:
                _error("RESULT_EVIDENCE_INVALID", "A source digest does not match the snapshot manifest")
        if "evidenceIds" in value and any(name in value for name in ("sourceDigest", "contentDigest")):
            for identifier in value["evidenceIds"]:
                for name in ("path", "sourceDigest", "contentDigest", "startLine", "endLine"):
                    if name in value and value[name] != evidence[identifier][name]:
                        _error(
                            "RESULT_EVIDENCE_INVALID",
                            "A grouped evidence reference changes its source",
                            field=name,
                        )
        for item in value.values():
            _references(item, evidence, manifests)


def _check_context(bundle: dict) -> None:
    validate_bundle(bundle)
    actual_hash = digest({key: value for key, value in bundle.items() if key != "contextHash"})
    if bundle["contextHash"] != actual_hash:
        _error("RESULT_EVIDENCE_INVALID", "Context hash does not match its serialized content")
    manifest = _unique_index(bundle["manifest"], "path")
    _unique_index(bundle["manifest"], "fileId")
    evidence = _unique_index(bundle["evidence"], "evidenceId")
    selected = _unique_index(bundle["selectedFiles"], "path")
    candidates = _unique_index(bundle["deploymentCandidates"], "candidateId")
    for path in manifest:
        if not _relative_path(path):
            _error("RESULT_EVIDENCE_INVALID", "Manifest has an unsafe relative path", path=path)
    for path, item in selected.items():
        source = manifest.get(path)
        if source is None or not source["eligible"] or source["fileId"] != item["fileId"]:
            _error("RESULT_EVIDENCE_INVALID", "Selected file is not eligible in the manifest", path=path)
        for supplied_range in item["providedRanges"]:
            if supplied_range["endLine"] < supplied_range["startLine"]:
                _error("RESULT_EVIDENCE_INVALID", "Provided line range is reversed", path=path)
    for item in evidence.values():
        path = item["path"]
        source = manifest.get(path)
        selection = selected.get(path)
        if source is None or selection is None or not source["eligible"]:
            _error("RESULT_EVIDENCE_INVALID", "Evidence is not from a provided eligible file", path=path)
        if item["sourceDigest"] != source["digest"]:
            _error("RESULT_EVIDENCE_INVALID", "Evidence digest belongs to a different source", path=path)
        if hashlib.sha256(item["text"].encode("utf-8")).hexdigest() != item["contentDigest"]:
            _error("RESULT_EVIDENCE_INVALID", "Evidence content digest does not match its text", path=path)
        if item["endLine"] < item["startLine"] or not any(
            supplied["startLine"] <= item["startLine"] <= item["endLine"] <= supplied["endLine"]
            for supplied in selection["providedRanges"]
        ):
            _error("RESULT_EVIDENCE_INVALID", "Evidence is outside the supplied line ranges", path=path)
        # Snippets join selected source lines with LF, without adding a final
        # delimiter. A trailing blank source line therefore ends in LF and must
        # count as a line; str.splitlines() would silently discard it.
        if item["text"].count("\n") + 1 != item["endLine"] - item["startLine"] + 1:
            _error(
                "RESULT_EVIDENCE_INVALID", "Evidence text does not preserve the source line count", path=path
            )
    if set(bundle["coverage"]["providedEvidenceIds"]) != set(evidence):
        _error("RESULT_EVIDENCE_INVALID", "Coverage does not identify exactly the provided evidence")
    components = set(bundle["componentRoots"])
    if any(not _relative_path(path, root=True) for path in components):
        _error("RESULT_EVIDENCE_INVALID", "Component root is not a safe relative path")
    for candidate in bundle["deploymentCandidates"]:
        if (
            not _relative_path(candidate["root"], root=True)
            or not set(candidate["componentRoots"]) <= components
        ):
            _error("RESULT_EVIDENCE_INVALID", "Deployment candidate refers to an unknown component")
    for fact in bundle["facts"]:
        if "component" in fact and fact["component"] not in components:
            _error("RESULT_EVIDENCE_INVALID", "Observation refers to an unknown component")
        if "candidateId" in fact:
            candidate = candidates.get(fact["candidateId"])
            if candidate is None:
                _error("RESULT_EVIDENCE_INVALID", "Observation refers to an unknown deployment candidate")
            if fact.get("component", ".") not in set(candidate["componentRoots"]) | {candidate["root"]}:
                _error(
                    "RESULT_EVIDENCE_INVALID",
                    "Observation component does not belong to its deployment candidate",
                )
    _references(bundle, evidence, manifest)


def _field(
    value: Any,
    evidence_ids: Iterable[str],
    scope: str = "source",
    reason: str = "Observed in the provided source",
) -> dict:
    return {
        "value": deepcopy(value),
        "status": "detected",
        "scope": scope,
        "evidenceIds": sorted(set(evidence_ids)),
        "reason": reason,
    }


def _unknown(reason: str) -> dict:
    return {"value": None, "status": "unknown", "scope": "source", "evidenceIds": [], "reason": reason}


def _fact_field(fact: dict, value: Any = None) -> dict:
    condition = f"; condition: {fact['condition']}" if fact.get("condition") else ""
    return _field(
        fact["value"] if value is None else value,
        fact["evidenceIds"],
        fact["scope"],
        f"Static observation: {fact['key']}{condition}",
    )


def _question(key: str, reason: str, kind: str = "code_review") -> dict:
    return {"key": key, "reason": reason, "kind": kind}


def _listener_observation(bundle: dict, fact: dict) -> bool:
    """Recognize trusted Express listen facts without reparsing excerpt text.

    The source extractor is the only v1 extractor emitting container ports with
    JavaScript/TypeScript evidence. Its excerpt may begin at a multiline argument,
    so searching for the literal method name would discard valid observations.
    """
    if fact["key"] != "runtime.port" or fact["scope"] != "container":
        return False
    ids = set(fact["evidenceIds"])
    source_paths = {item["path"] for item in bundle["manifest"] if item["kind"] == "source"}
    return any(
        item["evidenceId"] in ids
        and item["path"] in source_paths
        and PurePosixPath(item["path"]).suffix
        in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
        for item in bundle["evidence"]
    )


def _facts(bundle: dict, key: str, candidate: dict | None = None) -> list[dict]:
    components = set(candidate["componentRoots"]) | {candidate["root"]} if candidate else None
    values = [
        fact
        for fact in bundle["facts"]
        if fact["key"] == key
        and (components is None or fact.get("component", ".") in components)
        and (
            candidate is None or "candidateId" not in fact or fact["candidateId"] == candidate["candidateId"]
        )
    ]
    if candidate:
        runtime_components = set(candidate["componentRoots"])
        values = [
            fact
            for fact in values
            if "candidateId" in fact
            or not _listener_observation(bundle, fact)
            or fact.get("component", ".") in runtime_components
        ]
        targeted_scopes = {
            fact["scope"] for fact in values if fact.get("candidateId") == candidate["candidateId"]
        }
        # Target metadata supersedes broad defaults, while a proven source
        # listener remains an independent observation that may contradict EXPOSE.
        values = [
            fact
            for fact in values
            if "candidateId" in fact
            or fact["scope"] not in targeted_scopes
            or _listener_observation(bundle, fact)
        ]
    return sorted(
        values,
        key=lambda f: (
            _SCOPE_ORDER[f["scope"]],
            0 if candidate and f.get("component", ".") == candidate["root"] else 1,
            f.get("component", "."),
            canonical_bytes(f["value"]),
            canonical_bytes(f["evidenceIds"]),
        ),
    )


def _collection_value(fact: dict) -> Any:
    value = deepcopy(fact["value"])
    if isinstance(value, dict):
        if "component" in fact:
            value["component"] = fact["component"]
        if fact["key"].startswith("dependency."):
            value["kind"] = fact["key"].split(".", 1)[1]
    return value


def _deduplicate(items: list[dict], *, fields: bool = False) -> list[dict]:
    unique: dict[bytes, dict] = {}
    for item in items:
        key = canonical_bytes({name: item[name] for name in ("value", "status", "scope")} if fields else item)
        if key in unique and fields:
            unique[key]["evidenceIds"] = sorted(set(unique[key]["evidenceIds"]) | set(item["evidenceIds"]))
        else:
            unique[key] = deepcopy(item)
    return [unique[key] for key in sorted(unique)]


def _contains_redaction(value: Any) -> bool:
    if isinstance(value, str):
        return "<REDACTED>" in value
    if isinstance(value, list):
        return any(_contains_redaction(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_redaction(item) for item in value.values())
    return False


def _complete(result: dict, bundle: dict) -> dict:
    """Recompute mandatory coverage from detected facts, not model assertions."""
    limitations = list(result["coverage"]["limitations"])
    questions = list(result["questions"])
    for item in bundle["unresolved"]:
        questions.append(_question(item["key"], item["reason"]))
        limitations.append(item["reason"])
    coverage = bundle["coverage"]
    if coverage["omittedRelevantFiles"]:
        limitations.append("Relevant files were omitted from the context")
        questions.append(
            _question(
                "coverage.omittedRelevantFiles", "Provide the omitted relevant files within the input budget"
            )
        )
    if coverage["unresolvedReferences"]:
        limitations.append("Source references remain unresolved")
        questions.append(
            _question(
                "coverage.unresolvedReferences",
                "Resolve referenced code before generating a complete deployment configuration",
            )
        )
    if coverage["truncated"]:
        limitations.append("The analysis context was truncated")
        questions.append(_question("coverage.truncated", "Review context omitted by the input budget"))
    for collection in _COLLECTION_KEYS:
        for field in result[collection]:
            if field["status"] == "unknown":
                questions.append(_question(collection, field["reason"]))
    for service in result["services"]:
        service_id = service["serviceId"]
        if "workingDirectory" in service and _contains_redaction(service["workingDirectory"]["value"]):
            service["workingDirectory"].update(
                value=None, status="unknown", reason="Working directory contains redacted configuration"
            )
            questions.append(
                _question(
                    f"{service_id}.workingDirectory",
                    "Supply the redacted working directory securely",
                    "user_configuration",
                )
            )
        required = ["root", "role", "buildCommand"]
        if service["role"]["value"] in _STATIC_ROLES:
            required.append("outputDirectory")
        else:
            required.append("startCommand")
            for port in service["ports"]:
                if port["scope"] == "container" and _contains_redaction(port["value"]):
                    port.update(
                        value=None, status="unknown", reason="Container port configuration was redacted"
                    )
                    questions.append(
                        _question(
                            f"{service_id}.ports",
                            "Supply the redacted container port securely",
                            "user_configuration",
                        )
                    )
            service["ports"] = _deduplicate(service["ports"], fields=True)
            if not any(
                port["status"] == "detected" and port["scope"] == "container" for port in service["ports"]
            ):
                questions.append(
                    _question(
                        f"{service_id}.ports", "A container port must be confirmed", "user_configuration"
                    )
                )
                limitations.append(f"{service_id}: container port is unknown")
        for key in required:
            if _contains_redaction(service[key]["value"]):
                service[key].update(
                    value=None, status="unknown", reason=f"Required {key} contains redacted configuration"
                )
                questions.append(
                    _question(
                        f"{service_id}.{key}",
                        f"Supply the redacted {key} configuration securely",
                        "user_configuration",
                    )
                )
            if service[key]["status"] != "detected" or service[key]["value"] is None:
                if "redacted" not in service[key]["reason"].lower():
                    questions.append(_question(f"{service_id}.{key}", f"Confirm the deployment {key}"))
                limitations.append(f"{service_id}: {key} is not statically confirmed")
    if questions:
        limitations.extend(item["reason"] for item in questions)
    candidates = {candidate["candidateId"]: candidate for candidate in bundle["deploymentCandidates"]}
    supported_ids: set[str] = set()
    for service in result["services"]:
        candidate = candidates[service["serviceId"]]
        runtimes = _facts(bundle, "runtime.name", candidate)
        primary_runtime = runtimes[0]["value"] if runtimes else None
        has_supported_runtime = primary_runtime == "node"
        frameworks = [
            fact["value"] for fact in _facts(bundle, "framework", candidate) if isinstance(fact["value"], str)
        ]
        has_supported_framework = (
            primary_runtime is None and any(value in {"vite", "express"} for value in frameworks)
        ) or (service["role"]["value"] in _STATIC_ROLES and "vite" in frameworks)
        if service["role"]["value"] in _SUPPORTED_ROLES and (
            has_supported_runtime or has_supported_framework
        ):
            supported_ids.add(service["serviceId"])
        else:
            limitations.append(
                f"{service['serviceId']}: runtime or deployment role is outside the supported profile"
            )
            questions.append(
                _question(
                    f"{service['serviceId']}.support", "Review the unsupported runtime or deployment role"
                )
            )
    supported = bool(supported_ids)
    if not supported:
        limitations.append("No supported Node.js deployment candidate was identified")
    result["questions"] = _deduplicate(questions)
    result["coverage"] = {
        "completeForProfile": supported and not limitations,
        "limitations": sorted(set(limitations)),
    }
    result["status"] = (
        "complete"
        if result["coverage"]["completeForProfile"]
        else "needs_input"
        if supported
        else "unsupported"
    )
    return validate_result(result)


def static_analysis(bundle: dict) -> dict:
    """Create an honest fallback retaining every observation supported by v1."""
    _check_context(bundle)
    result = {
        "schemaVersion": "1",
        "status": "needs_input",
        "sourceSnapshotId": bundle["source"]["snapshotId"],
        "contextHash": bundle["contextHash"],
        "services": [],
        "dependencies": [],
        "apiRoutes": [],
        "environmentKeys": [],
        "connections": [],
        "questions": [],
        "coverage": {"completeForProfile": False, "limitations": []},
    }
    for candidate in sorted(bundle["deploymentCandidates"], key=lambda x: x["candidateId"]):
        service = {
            "serviceId": candidate["candidateId"],
            "root": _field(candidate["root"], candidate["evidenceIds"]),
            "role": _field(candidate["role"], candidate["evidenceIds"]),
            "componentRoots": sorted(candidate["componentRoots"]),
        }
        for field, key in _SERVICE_KEYS.items():
            observations = _facts(bundle, key, candidate)
            if field in {"ports", "healthchecks"}:
                service[field] = _deduplicate([_fact_field(fact) for fact in observations], fields=True)
                if field == "ports":
                    grouped: dict[tuple, set[bytes]] = {}
                    for fact in observations:
                        group = (fact.get("component", "."), fact["scope"])
                        grouped.setdefault(group, set()).add(canonical_bytes(fact["value"]))
                    for (component, scope), values in grouped.items():
                        if len(values) > 1:
                            result["questions"].append(
                                _question(
                                    f"{candidate['candidateId']}.ports",
                                    f"Conflicting {scope} ports for {component}: "
                                    + ", ".join(value.decode("utf-8") for value in sorted(values)),
                                )
                            )
            else:
                service[field] = (
                    _fact_field(observations[0])
                    if observations
                    else _unknown(f"No static observation for {key}")
                )
                if observations:
                    first = observations[0]
                    alternatives = [
                        fact
                        for fact in observations[1:]
                        if fact["scope"] == first["scope"]
                        and fact.get("component") == first.get("component")
                        and canonical_bytes(fact["value"]) != canonical_bytes(first["value"])
                    ]
                    if alternatives:
                        values = [first["value"]] + [fact["value"] for fact in alternatives]
                        result["questions"].append(
                            _question(
                                f"{candidate['candidateId']}.{field}",
                                f"Static observations conflict within {first['scope']}: {canonical_bytes(values).decode('utf-8')}",
                            )
                        )
        result["services"].append(service)
    for collection, keys in _COLLECTION_KEYS.items():
        result[collection] = _deduplicate(
            [_fact_field(fact, _collection_value(fact)) for fact in bundle["facts"] if fact["key"] in keys],
            fields=True,
        )
    return _complete(result, bundle)


def _matching_fact(field: dict, facts: list[dict], *, collection: bool = False) -> bool:
    supporting_ids: set[str] = set()
    for fact in facts:
        values = [fact["value"]]
        if collection:
            values.append(_collection_value(fact))
        if field["scope"] == fact["scope"] and any(
            canonical_bytes(field["value"]) == canonical_bytes(value) for value in values
        ):
            supporting_ids.update(fact["evidenceIds"])
    return bool(field["evidenceIds"]) and set(field["evidenceIds"]) <= supporting_ids


def _collection_conflict(name: str, proposed: dict, observed: dict) -> bool:
    """Detect changes to the same identified dependency/route/connection."""
    if proposed["status"] != "suggested" or proposed["scope"] != observed["scope"]:
        return False
    proposal, source = proposed["value"], observed["value"]
    if not isinstance(proposal, dict) or not isinstance(source, dict):
        return False
    if "component" in proposal and proposal["component"] != source.get("component"):
        return False
    if name == "dependencies":
        if not proposal.get("name") or proposal["name"] != source.get("name"):
            return False
        kind = proposal.get("kind") or (
            "database" if "engine" in proposal else "volume" if "mountPath" in proposal else None
        )
        if kind and kind != source.get("kind"):
            return False
    elif name == "apiRoutes":
        if any(proposal.get(key) != source.get(key) for key in ("method", "path")):
            return False
    elif name == "connections":
        if "component" not in proposal and not set(proposed["evidenceIds"]) & set(observed["evidenceIds"]):
            return False
    else:
        return False
    comparable = deepcopy(source)
    for key in ("component", "kind"):
        if key not in proposal:
            comparable.pop(key, None)
    return canonical_bytes(proposal) != canonical_bytes(comparable)


def _check_model(result: dict, bundle: dict) -> None:
    if (
        result["sourceSnapshotId"] != bundle["source"]["snapshotId"]
        or result["contextHash"] != bundle["contextHash"]
    ):
        _error("RESULT_CONTEXT_MISMATCH", "Model output belongs to a different snapshot or context revision")
    evidence = {item["evidenceId"]: item for item in bundle["evidence"]}
    manifest = {item["path"]: item for item in bundle["manifest"]}
    _references(result, evidence, manifest)
    candidates = {item["candidateId"]: item for item in bundle["deploymentCandidates"]}
    _unique_index(result["services"], "serviceId")
    for service in result["services"]:
        candidate = candidates.get(service["serviceId"])
        if candidate is None:
            _error(
                "RESULT_OBSERVATION_INVALID",
                "Model fabricated a deployment service",
                serviceId=service["serviceId"],
            )
        if set(service["componentRoots"]) != set(candidate["componentRoots"]):
            _error(
                "RESULT_OBSERVATION_INVALID", "Model changed a deployment candidate's constituent components"
            )
        for name in ("root", "role"):
            field = service[name]
            if field["status"] == "detected" and (
                field["value"] != candidate[name]
                or field["scope"] != "source"
                or not set(field["evidenceIds"]) <= set(candidate["evidenceIds"])
            ):
                _error(
                    "RESULT_OBSERVATION_INVALID",
                    "Detected service identity lacks matching candidate evidence",
                    field=name,
                )
        for name, key in _SERVICE_KEYS.items():
            if name not in service:
                continue  # Optional additions remain compatible with earlier model replies.
            fields = service[name] if isinstance(service[name], list) else [service[name]]
            for field in fields:
                if field["status"] == "detected" and not _matching_fact(
                    field, _facts(bundle, key, candidate)
                ):
                    _error(
                        "RESULT_OBSERVATION_INVALID",
                        "Detected service value does not match its semantic observation",
                        field=name,
                    )
    for name, keys in _COLLECTION_KEYS.items():
        facts = [fact for fact in bundle["facts"] if fact["key"] in keys]
        for field in result[name]:
            if field["status"] == "detected" and not _matching_fact(field, facts, collection=True):
                _error(
                    "RESULT_OBSERVATION_INVALID",
                    "Detected value does not match the corresponding observation",
                    field=name,
                )


def validate_analysis(reply: dict, bundle: dict) -> dict:
    """Validate provenance and merge required static facts into a model analysis."""
    validate_reply(reply)
    if reply["kind"] != "analysis":
        _error("RESULT_SCHEMA_INVALID", "An additional-file request is not an analysis result")
    observed = static_analysis(bundle)
    model = deepcopy(reply["result"])
    _check_model(model, bundle)
    result = deepcopy(observed)
    # Preserve model questions and limitations; models may identify real uncertainty.
    result["questions"].extend(model["questions"])
    result["coverage"]["limitations"].extend(model["coverage"]["limitations"])
    proposed = {service["serviceId"]: service for service in model["services"]}
    for service in result["services"]:
        proposal = proposed.get(service["serviceId"])
        if proposal is None:
            continue
        for name in (
            "root",
            "role",
            "runtime",
            "buildCommand",
            "startCommand",
            "outputDirectory",
            "workingDirectory",
        ):
            if name not in proposal:
                continue
            static_field, model_field = service[name], proposal[name]
            if static_field["status"] == "unknown":
                service[name] = deepcopy(model_field)
            elif model_field["status"] == "detected" and model_field["scope"] != static_field["scope"]:
                # A source build script and a container-stage command may both
                # be correct. Preserve the deployment observation selected by
                # static policy without calling different scopes a conflict.
                continue
            elif model_field["status"] != "unknown" and canonical_bytes(
                model_field["value"]
            ) != canonical_bytes(static_field["value"]):
                result["questions"].append(
                    _question(
                        f"{service['serviceId']}.{name}",
                        f"Model proposed {canonical_bytes(model_field['value']).decode('utf-8')}; static value "
                        f"{canonical_bytes(static_field['value']).decode('utf-8')} is preserved",
                    )
                )
        for name in ("ports", "healthchecks"):
            static_items = list(service[name])
            for field in proposal[name]:
                same_scope = [item for item in static_items if item["scope"] == field["scope"]]
                if (
                    field["status"] == "suggested"
                    and same_scope
                    and not any(
                        canonical_bytes(item["value"]) == canonical_bytes(field["value"])
                        for item in same_scope
                    )
                ):
                    result["questions"].append(
                        _question(
                            f"{service['serviceId']}.{name}",
                            f"Model suggested {canonical_bytes(field['value']).decode('utf-8')} in {field['scope']}; observed values are preserved",
                        )
                    )
                service[name].append(deepcopy(field))
            service[name] = _deduplicate(service[name], fields=True)
    for name in _COLLECTION_KEYS:
        for proposal in model[name]:
            for observation in result[name]:
                if _collection_conflict(name, proposal, observation):
                    result["questions"].append(
                        _question(
                            name,
                            f"Model suggested {canonical_bytes(proposal['value']).decode('utf-8')}; static value "
                            f"{canonical_bytes(observation['value']).decode('utf-8')} is preserved",
                        )
                    )
        # Every detected collection value is already represented by its complete
        # static observation, including the originating component. A model may
        # cite the short (unenriched) form; adding it would duplicate that fact.
        result[name] = _deduplicate(
            result[name] + [field for field in model[name] if field["status"] != "detected"],
            fields=True,
        )
    return _complete(result, bundle)
