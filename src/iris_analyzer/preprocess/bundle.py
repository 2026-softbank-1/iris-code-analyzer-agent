"""Reproducible ContextBundle construction, bounded expansion and artifacts."""

from __future__ import annotations

import copy
from pathlib import Path

from iris_analyzer.contracts import AnalyzerError, Limits, canonical_bytes, digest, validate_bundle

from .extractors.connections import extract_connections
from .extractors.docker import extract_docker
from .extractors.execution import extract_execution
from .extractors.express import extract_express
from .extractors.node import extract_node
from .observations import Observations
from .selector import Selection
from .snapshot import POLICY_VERSION, Snapshot, capture, get_snapshot, normalize_request, release_snapshot

PROFILE = "deployment_v1"


def prepare_context(
    repo: str | Path,
    profile: str = PROFILE,
    limits: Limits | None = None,
    excluded_paths: list[str] | None = None,
) -> dict:
    if profile != PROFILE:
        raise AnalyzerError("PROFILE_UNSUPPORTED", f"Unsupported preprocessing profile: {profile}")
    limits = limits or Limits()
    snapshot = capture(repo, limits, excluded_paths)
    try:
        return _build(snapshot, limits, revision=1, requested=[])
    except BaseException:
        release_snapshot(snapshot.snapshot_id)
        raise


def expand_context(bundle: dict, requested_paths: list[str], limits: Limits | None = None) -> dict:
    """Expand from immutable bytes, rejecting requests with recorded reasons.

    A rejected request is still a new, deterministic revision. Existing evidence
    remains available and coverage records why the model cannot receive it.
    """
    limits = limits or Limits()
    validate_bundle(bundle)
    if digest({key: value for key, value in bundle.items() if key != "contextHash"}) != bundle["contextHash"]:
        raise AnalyzerError("CONTEXT_HASH_INVALID", "Expansion bundle hash does not match its contents")
    snapshot = get_snapshot(bundle["source"]["snapshotId"])
    if not isinstance(requested_paths, list) or any(not isinstance(path, str) for path in requested_paths):
        raise AnalyzerError("FILE_REQUEST_REJECTED", "File requests must be an array of relative paths")
    rejects = []
    accepted = []
    manifest = {item["path"]: item for item in snapshot.manifest}
    request_paths = list(dict.fromkeys(requested_paths))
    expansion_count = bundle.get("policy", {}).get("expansionsUsed", bundle["revision"] - 1)
    for index, requested in enumerate(request_paths):
        reason = None
        if expansion_count >= limits.max_expansions:
            reason = "expansion_limit"
        elif index >= limits.max_requested_files:
            reason = "requested_file_limit"
        else:
            try:
                requested = normalize_request(requested)
            except AnalyzerError:
                reason = "invalid_relative_path"
            if reason is None and requested not in manifest:
                reason = "not_in_snapshot"
            elif reason is None and not manifest[requested]["eligible"]:
                reason = manifest[requested]["exclusionReason"]
        if reason:
            rejects.append({"path": requested, "reason": reason})
        else:
            accepted.append(requested)
    previous_requested = bundle.get("policy", {}).get("requestedPaths", [])
    try:
        result = _build(
            snapshot,
            limits,
            revision=bundle["revision"] + 1,
            requested=sorted(set(previous_requested + accepted)),
            protected_files={item["path"] for item in bundle["selectedFiles"]},
        )
    except AnalyzerError as exc:
        if exc.code != "CONTEXT_BUDGET_EXCEEDED":
            raise
        rejects.extend({"path": path, "reason": "context_budget"} for path in accepted)
        result = copy.deepcopy(bundle)
        result["revision"] = bundle["revision"] + 1
    # Registry bytes are shared by content ID; separate acquisitions can carry
    # different Git metadata for byte-identical trees. This job retains its own
    # immutable source identity rather than adopting another capture's commit.
    result["source"] = copy.deepcopy(bundle["source"])
    result["policy"]["expansionsUsed"] = expansion_count + 1
    if rejects:
        result["coverage"]["rejectedRequests"] = rejects
        result["unresolved"].extend(
            {
                "key": "requested_file",
                "reason": f"File request rejected: {item['reason']}",
                "path": item["path"],
            }
            for item in rejects
        )
    # Retain unresolved earlier policy requests after a later expansion.
    for item in bundle.get("unresolved", []):
        if item["key"] == "requested_file" and item not in result["unresolved"]:
            result["unresolved"].append(copy.deepcopy(item))
    return _finish(result, limits)


def _build(
    snapshot: Snapshot,
    limits: Limits,
    revision: int,
    requested: list[str],
    protected_files: set[str] | None = None,
) -> dict:
    selection = Selection(snapshot).run(requested)
    observations = Observations(snapshot)
    components = extract_node(selection, observations)
    extract_express(selection, observations)
    extract_connections(selection, observations)
    candidates = extract_docker(selection, observations, components)
    extract_execution(selection, observations)
    selected = []
    manifest = {item["path"]: item for item in snapshot.manifest}
    for path in selection.ordered():
        if path in requested:
            observations.snippet(path)
        if not any(item["path"] == path for item in observations.evidence.values()):
            count = 10 if selection.selected[path]["role"] == "documentation" else 6
            observations.snippet(path, 1, count)
        selected.append(
            {
                "fileId": manifest[path]["fileId"],
                "path": path,
                "role": selection.selected[path]["role"],
                "selectionReason": selection.selected[path]["selectionReason"],
                "providedRanges": [],
            }
        )
    for reference in selection.unresolved:
        observations.unknown(
            reference["key"],
            reference["reason"],
            **{key: value for key, value in reference.items() if key not in {"key", "reason"}},
        )
    if not candidates:
        observations.unknown(
            "deployment.support", "No supported Node.js application execution candidate was observed"
        )
    component_roots = set(selection.roots if components else [])
    component_roots.update(fact["component"] for fact in observations.facts if "component" in fact)
    for candidate in candidates:
        component_roots.add(candidate["root"])
        component_roots.update(candidate["componentRoots"])
    bundle = {
        "schemaVersion": "1",
        "preprocessorVersion": "1",
        "policyVersion": POLICY_VERSION,
        "profile": PROFILE,
        "revision": revision,
        "source": {"snapshotId": snapshot.snapshot_id, "commit": snapshot.commit},
        "manifest": copy.deepcopy(list(snapshot.manifest)),
        "componentRoots": sorted(component_roots),
        "deploymentCandidates": candidates,
        "selectedFiles": selected,
        "facts": observations.facts,
        "relations": observations.relations,
        "unresolved": observations.unresolved,
        "coverage": {
            "providedEvidenceIds": [],
            "omittedRelevantFiles": [],
            "unresolvedReferences": selection.unresolved,
            "truncated": False,
        },
        "policy": {
            "selectionVersion": "deployment_v1.1",
            "excludedPaths": list(snapshot.excluded_paths),
            "requestedPaths": requested,
            "expansionsUsed": max(0, revision - 1),
            "maxBundleBytes": limits.max_bundle_bytes,
            "maxInputTokens": limits.max_input_tokens,
            "maxFileBytes": limits.max_file_bytes,
            "maxExpansions": limits.max_expansions,
            "maxRequestedFiles": limits.max_requested_files,
            # This conservative upper-bound estimate remains portable across
            # models. No actual provider tokenizer is claimed.
            "tokenizer": "conservative_utf8_bytes_v1" if limits.max_input_tokens is not None else None,
        },
        "evidence": list(observations.evidence.values()),
    }
    _update_ranges(bundle)
    _sort(bundle)
    # Remove whole lower-priority files, never silently truncate an observation
    # and leave its evidence citation pointing to omitted text.
    priorities = selection.selected
    removable = sorted(
        [
            item["path"]
            for item in bundle["selectedFiles"]
            if priorities[item["path"]]["priority"] > 1
            and item["path"] not in requested
            and item["path"] not in (protected_files or set())
        ],
        key=lambda path: (priorities[path]["priority"], path),
        reverse=True,
    )
    while (
        _size(bundle) > limits.max_bundle_bytes
        or limits.max_input_tokens is not None
        and _size(bundle) > limits.max_input_tokens
    ):
        if not removable:
            raise AnalyzerError(
                "CONTEXT_BUDGET_EXCEEDED",
                "Required source inventory and manifest evidence exceed the configured input budget",
                {
                    "bundleBytes": _size(bundle),
                    "maxBundleBytes": limits.max_bundle_bytes,
                    "maxInputTokens": limits.max_input_tokens,
                },
            )
        path = removable.pop(0)
        removed_ids = {item["evidenceId"] for item in bundle["evidence"] if item["path"] == path}
        bundle["evidence"] = [item for item in bundle["evidence"] if item["path"] != path]
        bundle["selectedFiles"] = [item for item in bundle["selectedFiles"] if item["path"] != path]
        bundle["facts"] = [item for item in bundle["facts"] if not set(item["evidenceIds"]) & removed_ids]
        bundle["relations"] = [
            item for item in bundle["relations"] if not set(item["evidenceIds"]) & removed_ids
        ]
        for candidate in bundle["deploymentCandidates"]:
            candidate["evidenceIds"] = [
                identifier for identifier in candidate["evidenceIds"] if identifier not in removed_ids
            ]
        bundle["deploymentCandidates"] = [
            candidate for candidate in bundle["deploymentCandidates"] if candidate["evidenceIds"]
        ]
        bundle["unresolved"] = [
            item for item in bundle["unresolved"] if not set(item.get("evidenceIds", [])) & removed_ids
        ]
        bundle["coverage"]["omittedRelevantFiles"].append(path)
        bundle["coverage"]["truncated"] = True
        bundle["unresolved"].append(
            {
                "key": "input_budget",
                "path": path,
                "reason": "Relevant selected file omitted to satisfy configured byte/token budget",
            }
        )
        _update_ranges(bundle)
        _sort(bundle)
    return _finish(bundle, limits)


def _size(bundle: dict) -> int:
    # Include the final fixed-length hash field in the hard limit calculation.
    value = dict(bundle)
    value["contextHash"] = "0" * 64
    return len(canonical_bytes(value))


def _update_ranges(bundle: dict) -> None:
    for item in bundle["selectedFiles"]:
        ranges = {
            (evidence["startLine"], evidence["endLine"])
            for evidence in bundle["evidence"]
            if evidence["path"] == item["path"]
        }
        item["providedRanges"] = [{"startLine": start, "endLine": end} for start, end in sorted(ranges)]
    bundle["coverage"]["providedEvidenceIds"] = sorted(item["evidenceId"] for item in bundle["evidence"])


def _sort(bundle: dict) -> None:
    bundle["evidence"].sort(
        key=lambda item: (item["path"], item["startLine"], item["endLine"], item["evidenceId"])
    )
    for name in ("facts", "relations", "unresolved"):
        bundle[name].sort(key=canonical_bytes)
    bundle["coverage"]["omittedRelevantFiles"].sort()


def _finish(bundle: dict, limits: Limits) -> dict:
    bundle.pop("contextHash", None)
    _update_ranges(bundle)
    _sort(bundle)
    bundle["contextHash"] = digest(bundle)
    if (
        len(canonical_bytes(bundle)) > limits.max_bundle_bytes
        or limits.max_input_tokens is not None
        and len(canonical_bytes(bundle)) > limits.max_input_tokens
    ):
        raise AnalyzerError(
            "CONTEXT_BUDGET_EXCEEDED", "Bundle including coverage exceeds the configured input budget"
        )
    return validate_bundle(bundle)


def compact_model_input(bundle: dict) -> dict:
    """Portable selected payload; adapter may wrap it in its platform prompt."""
    selected_paths = {item["path"] for item in bundle["selectedFiles"]}
    model = copy.deepcopy(bundle)
    model["manifest"] = [item for item in model["manifest"] if item["path"] in selected_paths]
    model["availablePaths"] = [
        {"path": item["path"], "kind": item["kind"]}
        for item in bundle["manifest"]
        if item["eligible"] and item["path"] not in selected_paths
    ]
    # Selected means some snippets are present, not that the whole file was
    # supplied. Keep bounded expansion possible for an omitted declaration.
    from iris_analyzer.readiness.source import _verified_text

    already_requested = set(bundle.get("policy", {}).get("requestedPaths", []))
    model["expandableSelectedPaths"] = [
        {"path": item["path"], "kind": item["kind"], "reason": "incomplete_supplied_ranges"}
        for item in bundle["manifest"]
        if item["eligible"]
        and item["path"] in selected_paths
        and item["path"] not in already_requested
        and _verified_text([e for e in bundle["evidence"] if e["path"] == item["path"]], item["digest"])
        is None
    ]
    return model


def save_bundle(bundle: dict, out: str | Path) -> None:
    validate_bundle(bundle)
    output = Path(out)
    output.mkdir(parents=True, exist_ok=True)
    (output / "manifest.json").write_bytes(canonical_bytes(bundle["manifest"]) + b"\n")
    context = {key: value for key, value in bundle.items() if key not in {"manifest", "evidence"}}
    (output / "context.json").write_bytes(canonical_bytes(context) + b"\n")
    (output / "evidence.jsonl").write_bytes(
        b"".join(canonical_bytes(evidence) + b"\n" for evidence in bundle["evidence"])
    )
    (output / "model-input.json").write_bytes(canonical_bytes(compact_model_input(bundle)) + b"\n")
