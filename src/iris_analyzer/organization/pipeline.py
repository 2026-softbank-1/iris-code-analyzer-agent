"""Standalone Organization composition: fixed sources, repo analysis, graph and system plan."""

from __future__ import annotations

import asyncio
import copy
import fnmatch
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from ..build.source import stage_local_source, verify_source
from ..contracts import AnalyzerError, canonical_bytes, digest
from ..deployment.dossier import prepare_readiness
from ..integrations import AnalysisClientError, LocalAnalysisClient
from ..pipeline import write_json
from .contracts import seal_document, validate_repository_records, validate_request, validate_seal
from .github import GithubOrganizationClient, InventoryResult, RepositorySource
from .graph import build_system_graph
from .planner import build_system_plan, system_plan_digest

ProgressSink = Callable[[dict], Awaitable[None]]


def _evidence(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    if path.stat().st_size > 2_000_000:
        raise AnalyzerError("ORGANIZATION_EVIDENCE_LIMIT", "Repository evidence exceeds the output limit")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _repository_evidence(analysis_out: Path) -> list[dict]:
    rows = _evidence(analysis_out / "evidence.jsonl") + _evidence(
        analysis_out / "readiness" / "readiness-context" / "evidence.jsonl"
    )
    by_id = {}
    for row in rows:
        identifier = row.get("evidenceId")
        if identifier in by_id and by_id[identifier] != row:
            raise AnalyzerError(
                "ORGANIZATION_EVIDENCE_CHANGED", "Evidence ID refers to different source locations"
            )
        if not isinstance(identifier, str):
            raise AnalyzerError("ORGANIZATION_EVIDENCE_INVALID", "Source evidence must have stable IDs")
        by_id[identifier] = row
    return [by_id[key] for key in sorted(by_id)]


def _public_report(report: dict) -> dict:
    keys = ("schemaVersion", "mode", "status", "verification", "durationSeconds", "errors")
    return {key: copy.deepcopy(report[key]) for key in keys if key in report}


def _local_sources(document: dict, organization: str) -> tuple[InventoryResult, list[RepositorySource]]:
    allowed = {"schemaVersion", "organization", "repositories"}
    if (
        not isinstance(document, dict)
        or set(document) - allowed
        or document.get("schemaVersion") != "iris.organization-sources.v1"
        or document.get("organization", "").lower() != organization.lower()
    ):
        raise AnalyzerError(
            "ORGANIZATION_SOURCES_INVALID", "Local source manifest must identify the requested organization"
        )
    rows = document.get("repositories")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise AnalyzerError(
            "ORGANIZATION_SOURCES_INVALID", "Local source manifest requires 1 to 100 repositories"
        )
    records, sources, ids, names = [], [], set(), set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - {
            "repositoryId",
            "fullName",
            "commitSha",
            "ref",
            "sourceRoot",
            "metadata",
        }:
            raise AnalyzerError("ORGANIZATION_SOURCES_INVALID", "Unknown local source fields")
        identity, name, sha = row.get("repositoryId"), row.get("fullName"), row.get("commitSha")
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", identity)
            or identity in ids
        ):
            raise AnalyzerError(
                "ORGANIZATION_SOURCES_INVALID", "Local repository IDs must be unique and path-safe"
            )
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", name)
            or name.split("/")[0].lower() != organization.lower()
            or name.lower() in names
        ):
            raise AnalyzerError(
                "ORGANIZATION_SOURCES_INVALID",
                "Local repositories must be unique members of the requested organization",
            )
        if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise AnalyzerError(
                "ORGANIZATION_SOURCES_INVALID", "Local sources require caller-attested pinned commit SHAs"
            )
        source = Path(row.get("sourceRoot", ""))
        if (
            not source.is_absolute()
            or source.is_symlink()
            or not source.is_dir()
            or source.resolve() != source
        ):
            raise AnalyzerError(
                "ORGANIZATION_SOURCES_INVALID", "Local sources must be ordinary absolute directories"
            )
        ids.add(identity)
        names.add(name.lower())
        ref = row.get("ref") or sha
        if not isinstance(ref, str) or not 1 <= len(ref) <= 300 or any(ord(c) < 32 for c in ref):
            raise AnalyzerError("ORGANIZATION_SOURCES_INVALID", "Invalid local source ref")
        url = "https://github.com/" + name
        coverage = {"provenance": "caller_attested_commit", "omittedFiles": []}
        sources.append(RepositorySource(identity, name, url, ref, sha, source, coverage))
        records.append(
            {
                "repositoryId": identity,
                "fullName": name,
                "repositoryUrl": url,
                "ref": ref,
                "commitSha": sha,
                "status": "selected",
                "coverage": coverage,
            }
        )
    inventory = InventoryResult(
        repositories=records,
        limitations=[
            {
                "code": "LOCAL_SOURCES_ATTESTED",
                "reason": "Local source SHAs are caller attestations; file bytes are independently hashed.",
            }
        ],
        completeness="partial",
        organization=organization,
        listed_count=len(records),
    )
    return inventory, sources


def _matches(repository: str, patterns: list[str]) -> bool:
    return any(
        fnmatch.fnmatchcase(repository.lower(), pattern.lower())
        or fnmatch.fnmatchcase(repository.split("/")[1].lower(), pattern.lower())
        for pattern in patterns
    )


def _scope_records(records: list[dict], request: dict) -> list[dict]:
    """A new scope may exclude fixed records but cannot change their pinned revisions."""
    result = copy.deepcopy(records)
    for row in result:
        full_name = row["fullName"]
        if _matches(full_name, request["excludeRepositories"]) or (
            request["includeRepositories"] and not _matches(full_name, request["includeRepositories"])
        ):
            row.update(status="skipped", reason="excluded_by_request")
        override = request["refs"].get(full_name)
        if override is not None and override not in {row.get("ref"), row.get("commitSha")}:
            raise AnalyzerError(
                "ORGANIZATION_SOURCE_CHANGED",
                "Ref overrides differ from the saved pinned source; collect a new Organization snapshot",
            )
    for pattern in request["includeRepositories"]:
        if not any(_matches(row["fullName"], [pattern]) for row in result):
            raise AnalyzerError(
                "ORGANIZATION_SCOPE_UNAVAILABLE",
                "Requested repository pattern is absent from this fixed snapshot",
            )
    return result


def plan_organization(snapshot: dict, request: dict, *, scope_provenance: dict | None = None) -> dict:
    """Replan fixed saved facts after answers; this operation does not reread repositories."""
    validate_seal(snapshot, "snapshotDigest")
    request = validate_request(request)
    if (
        snapshot.get("schemaVersion") != "iris.organization-snapshot.v1"
        or snapshot.get("organization", "").lower() != request["organization"].lower()
    ):
        raise AnalyzerError("ORGANIZATION_SOURCE_CHANGED", "Snapshot belongs to another organization")
    records = _scope_records(snapshot["repositories"], request)
    validate_repository_records(records)
    graph = build_system_graph(records, request)
    graph["inventoryCoverage"] = {
        "completeness": snapshot["completeness"],
        "limitations": snapshot["limitations"],
    }
    if scope_provenance:
        graph["scopeSelection"] = copy.deepcopy(scope_provenance)
    # Hidden repos are not countable. Known truncation/failure must not masquerade as all-repo coverage.
    incomplete = [
        row
        for row in snapshot["limitations"]
        if row.get("code") not in {"credential_visibility", "LOCAL_SOURCES_ATTESTED"}
        and row.get("scope", "inventory") == "inventory"
    ]
    if (
        incomplete
        and (not request["selectedServiceIds"] or scope_provenance is not None)
        and not request["includeRepositories"]
    ):
        graph["questions"].append(
            {
                "key": "inventoryCoverage",
                "reason": "Organization inventory is incomplete; restore access or explicitly select a bounded repository/service scope.",
                "kind": "scope",
                "required": True,
                "serviceId": None,
                "candidateServiceIds": [],
            }
        )
    graph = seal_document(graph, "graphDigest")
    plan = build_system_plan(graph, records, request)
    plan["organizationSnapshotDigest"] = snapshot["snapshotDigest"]
    if scope_provenance:
        plan["scopeSelection"] = copy.deepcopy(scope_provenance)
    plan["planDigest"] = system_plan_digest(plan)
    return {
        "schemaVersion": "iris.organization-result.v1",
        "organization": request["organization"],
        "snapshotDigest": snapshot["snapshotDigest"],
        "graph": graph,
        "plan": plan,
        "deploymentAuthorized": False,
    }


class OrganizationAnalysisClient:
    """Async library facade usable independently of the IRIS Control API."""

    def __init__(
        self,
        *,
        github: GithubOrganizationClient | None = None,
        analyzer: LocalAnalysisClient | None = None,
        advisor=None,
    ):
        self.github = github
        self.analyzer = analyzer or LocalAnalysisClient()
        self.advisor = advisor

    async def analyze_organization(
        self,
        request: dict,
        *,
        out: str | Path,
        sources: dict | None = None,
        on_progress: ProgressSink | None = None,
    ) -> dict:
        request = validate_request(request)
        destination = Path(out).absolute()
        if destination.exists() or destination.is_symlink():
            raise AnalyzerError(
                "ORGANIZATION_OUTPUT_EXISTS", "Use a new output directory for every Organization snapshot"
            )
        # Local materialization may not place generated artifacts inside a source input.
        if sources is not None:
            if not isinstance(sources, dict) or not isinstance(sources.get("repositories"), list):
                raise AnalyzerError(
                    "ORGANIZATION_SOURCES_INVALID", "Local source manifest requires a repository list"
                )
            for item in sources.get("repositories", []):
                if not isinstance(item, dict) or not isinstance(item.get("sourceRoot"), str):
                    raise AnalyzerError(
                        "ORGANIZATION_SOURCES_INVALID", "Every local source requires an absolute sourceRoot"
                    )
                source = Path(item.get("sourceRoot", "")).resolve()
                if destination.resolve() == source or destination.resolve().is_relative_to(source):
                    raise AnalyzerError(
                        "ORGANIZATION_OUTPUT_INVALID",
                        "Organization output must stay outside all source repositories",
                    )
        destination.mkdir(parents=True, mode=0o700)
        destination.chmod(0o700)

        async def event(stage: str, **fields) -> None:
            if on_progress:
                await on_progress({"stage": stage, **fields})

        owned = self.github is None
        github = self.github or GithubOrganizationClient()
        workspace = TemporaryDirectory(prefix="iris-org-sources-")
        try:
            await event("discovering")
            if sources is None:
                inventory = await github.discover(
                    request["organization"],
                    include=request["includeRepositories"] or None,
                    exclude=request["excludeRepositories"],
                    refs=request["refs"],
                    max_repositories=request["maxRepositories"],
                )
                materialized = await github.materialize(inventory, Path(workspace.name))
            else:
                inventory, materialized = _local_sources(sources, request["organization"])
                if len(materialized) > request["maxRepositories"]:
                    raise AnalyzerError("ORGANIZATION_SOURCE_LIMIT", "Local sources exceed maxRepositories")
                _scope_records(
                    inventory.repositories, request
                )  # Reject inconsistent ref or unseen include promises.
                materialized = [
                    source
                    for source in materialized
                    if not _matches(source.full_name, request["excludeRepositories"])
                    and (
                        not request["includeRepositories"]
                        or _matches(source.full_name, request["includeRepositories"])
                    )
                ]
                selected = {source.repository_id for source in materialized}
                for row in inventory.repositories:
                    if row["repositoryId"] not in selected:
                        row.update(status="skipped", reason="excluded_by_request")
            write_json(destination / "request.json", request)
            records = copy.deepcopy(inventory.repositories)
            for row in records:
                row.setdefault("repositoryUrl", row.get("url") or "https://github.com/" + row["fullName"])
                if row.get("status") == "failed":
                    row.setdefault("reason", row.get("errorCode", "ORGANIZATION_SOURCE_UNAVAILABLE"))
            by_id = {str(item["repositoryId"]): item for item in records}
            source_roots = {}
            for source in sorted(materialized, key=lambda item: item.repository_id):
                row = by_id[source.repository_id]
                await event("analyzing", repositoryId=source.repository_id, fullName=source.full_name)
                staged = destination / "sources" / source.repository_id
                try:
                    manifest = await asyncio.to_thread(stage_local_source, source.source_root, staged)
                    manifest["origin"] = {
                        "kind": "github" if sources is None else "caller_attested_commit",
                        "repositoryUrl": source.url,
                        "requestedRef": source.ref,
                        "revision": source.commit_sha,
                        "uploadId": None,
                    }
                    analysis_out = destination / "repository-artifacts" / source.repository_id
                    outcome = await self.analyzer.analyze_repository(staged, out=analysis_out)
                    readiness = await asyncio.to_thread(
                        prepare_readiness,
                        staged,
                        analysis=outcome.analysis_result,
                        out=analysis_out / "readiness",
                    )
                    await asyncio.to_thread(verify_source, staged, manifest)
                    row.update(
                        status="analyzed",
                        reason=None,
                        analysis=outcome.analysis_result,
                        readiness=readiness,
                        sourceSnapshotId=outcome.analysis_result["sourceSnapshotId"],
                        analysisDigest=digest(outcome.analysis_result),
                        buildSourceManifest=manifest,
                        runReport=_public_report(outcome.run_report),
                        evidence=await asyncio.to_thread(_repository_evidence, analysis_out),
                    )
                    source_roots[source.repository_id] = str(staged)
                except (AnalysisClientError, AnalyzerError) as error:
                    row.update(status="failed", reason=error.code, analysis=None, readiness=None)
                except OSError:
                    row.update(
                        status="failed", reason="ORGANIZATION_SOURCE_IO_FAILED", analysis=None, readiness=None
                    )
            validate_repository_records(records)
            snapshot = seal_document(
                {
                    "schemaVersion": "iris.organization-snapshot.v1",
                    "organization": request["organization"],
                    "repositories": records,
                    "completeness": inventory.completeness
                    if all(row.get("status") != "failed" for row in records)
                    else "partial",
                    "limitations": copy.deepcopy(inventory.limitations),
                    "inventoryDigest": digest(inventory.to_dict()),
                },
                "snapshotDigest",
            )
            write_json(destination / "organization-snapshot.json", snapshot)
            private = destination / "local-workspace.json"
            private.write_bytes(
                canonical_bytes(
                    {
                        "schemaVersion": "iris.organization-workspace.v1",
                        "snapshotDigest": snapshot["snapshotDigest"],
                        "sourceRoots": source_roots,
                    }
                )
                + b"\n"
            )
            private.chmod(0o600)
            await event("planning")
            result = plan_organization(snapshot, request)
            if self.advisor is not None:
                try:
                    advice, report = await self.advisor.suggest(result["graph"], request)
                    from .advisor import advisor_input, validate_advice

                    advice = validate_advice(advice, advisor_input(result["graph"], request))
                    if not request["selectedServiceIds"] and advice["proposedServiceIds"]:
                        proposed = {
                            **copy.deepcopy(request),
                            "selectedServiceIds": advice["proposedServiceIds"],
                        }
                        provenance = {
                            "basis": "ai_proposal",
                            "originalRequestDigest": digest(request),
                            "adviceDigest": digest(advice),
                            "adviceGraphDigest": advice["graphDigest"],
                        }
                        result = plan_organization(snapshot, proposed, scope_provenance=provenance)
                    result["systemAdvice"] = advice
                    result["advisorReport"] = report
                    # Proposals cannot silently become source facts or execution permissions.
                    write_json(destination / "system-advice.json", advice)
                except AnalyzerError as error:
                    result["advisorReport"] = {
                        "schemaVersion": "iris.system-advice-run.v1",
                        "mode": "ai",
                        "status": "failed",
                        "errorCode": error.code,
                    }
                    result["status"] = "advisor_failed"
                    result["plan"]["questions"].append(
                        {
                            "key": "systemAdvice",
                            "reason": "Requested AI system advice failed: " + error.code,
                            "serviceId": None,
                            "requiredForExecution": True,
                        }
                    )
                    result["plan"].update(status="needs_input", executionEligible=False, buildEligible=False)
                    result["plan"]["planDigest"] = system_plan_digest(result["plan"])
            write_json(destination / "system-graph.json", result["graph"])
            write_json(destination / "system-plan.json", result["plan"])
            write_json(destination / "organization-result.json", result)
            await event(
                "completed", status=result["plan"].get("status"), snapshotDigest=snapshot["snapshotDigest"]
            )
            return result
        finally:
            await asyncio.to_thread(workspace.cleanup)
            if owned:
                await github.aclose()
