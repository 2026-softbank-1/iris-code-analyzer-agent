"""Standalone Organization composition and pinned-source replanning without cloud mutations."""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from iris_analyzer.contracts import AnalyzerError
from iris_analyzer.organization import OrganizationAnalysisClient, plan_organization, validate_request
from iris_analyzer.organization.advisor import advisor_input, validate_advice
from iris_analyzer.organization.contracts import seal_document
from iris_analyzer.organization.execution import execute_system_bundle, write_system_bundle


def sources(tmp_path):
    rows = []
    for number, name in enumerate(("first-api", "second-api"), 1):
        root = tmp_path / name
        root.mkdir()
        (root / "package.json").write_text(
            json.dumps(
                {"name": name, "scripts": {"start": "node index.js"}, "dependencies": {"express": "5.1.0"}}
            )
        )
        (root / "index.js").write_text(
            "const app = require('express')();\napp.get('/health', (req,res)=>res.send('OK'));\napp.listen(3000,'0.0.0.0');\n"
        )
        (root / "Dockerfile").write_text(
            'FROM node:24\nWORKDIR /app\nCOPY . .\nUSER 1000\nEXPOSE 3000\nCMD ["node", "index.js"]\n'
        )
        (root / ".env").write_text("SECRET=private-fixture-value\n")
        (root / "logo.png").write_bytes(b"\x89PNG\x00fixture")
        rows.append(
            {
                "repositoryId": str(number),
                "fullName": "demo-org/" + name,
                "commitSha": str(number) * 40,
                "sourceRoot": str(root.resolve()),
            }
        )
    return {"schemaVersion": "iris.organization-sources.v1", "organization": "demo-org", "repositories": rows}


def request(**kwargs):
    return {
        "schemaVersion": "iris.organization-request.v1",
        "organization": "demo-org",
        "purpose": "Build the two-service demonstration system",
        "target": {
            "kind": "existing_kubernetes",
            "context": "isolated-test",
            "namespace": "iris-demo",
            "architecture": "amd64",
        },
        **kwargs,
    }


def test_org_pipeline_captures_multiple_sources_and_keeps_secrets_out(tmp_path):
    result = asyncio.run(
        OrganizationAnalysisClient().analyze_organization(
            request(), out=tmp_path / "output", sources=sources(tmp_path)
        )
    )
    assert (
        len([component for component in result["graph"]["components"] if component["kind"] == "service"]) == 2
    )
    snapshot = json.loads((tmp_path / "output/organization-snapshot.json").read_text())
    assert {row["repositoryId"] for row in snapshot["repositories"]} == {"1", "2"}
    assert all(row["status"] == "analyzed" for row in snapshot["repositories"])
    assert "private-fixture-value" not in json.dumps(snapshot)
    assert "sourceRoot" not in json.dumps(snapshot)
    assert (tmp_path / "output/sources/1/logo.png").read_bytes() == b"\x89PNG\x00fixture"
    assert not (tmp_path / "output/sources/1/.env").exists()
    assert result["deploymentAuthorized"] is False
    assert result["plan"]["executionAuthorized"] is False


def test_replan_images_uses_same_commits_and_creates_dry_run_bundle(tmp_path):
    output = tmp_path / "output"
    initial = asyncio.run(
        OrganizationAnalysisClient().analyze_organization(request(), out=output, sources=sources(tmp_path))
    )
    snapshot = json.loads((output / "organization-snapshot.json").read_text())
    service_ids = [
        component["id"] for component in initial["graph"]["components"] if component["kind"] == "service"
    ]
    binding = [
        {
            "serviceId": sid,
            "port": 3000,
            "imageReference": f"example.test/apps/service-{index}@sha256:" + "a" * 64,
        }
        for index, sid in enumerate(service_ids)
    ]
    result = plan_organization(snapshot, request(serviceBindings=binding, selectedServiceIds=service_ids))
    assert result["snapshotDigest"] == initial["snapshotDigest"]
    assert result["plan"]["status"] == "ready", result["plan"]["questions"]
    assert {row["commitSha"] for row in result["plan"]["repositories"]} == {"1" * 40, "2" * 40}
    bundle = write_system_bundle(result["plan"], tmp_path / "bundle")
    assert len([item for item in bundle["kubernetes"]["manifests"] if item["kind"] == "Deployment"]) == 2
    dry = execute_system_bundle(tmp_path / "bundle")
    assert dry["dryRun"] is True and dry["executionAuthorized"] is False


def test_changed_snapshot_cannot_be_replanned(tmp_path):
    output = tmp_path / "output"
    asyncio.run(
        OrganizationAnalysisClient().analyze_organization(request(), out=output, sources=sources(tmp_path))
    )
    snapshot = json.loads((output / "organization-snapshot.json").read_text())
    snapshot["repositories"][0]["commitSha"] = "f" * 40
    with pytest.raises(AnalyzerError, match="digest"):
        plan_organization(snapshot, request())


def test_output_directory_cannot_be_inside_source(tmp_path):
    local = sources(tmp_path)
    with pytest.raises(AnalyzerError) as error:
        asyncio.run(
            OrganizationAnalysisClient().analyze_organization(
                request(), out=Path(local["repositories"][0]["sourceRoot"]) / "artifacts", sources=local
            )
        )
    assert error.value.code == "ORGANIZATION_OUTPUT_INVALID"


@pytest.mark.parametrize(
    "binding",
    [
        {"serviceId": "repo-1--app", "runtimeEnv": [{"key": "DATABASE_URL", "value": "unsafe"}]},
        {
            "serviceId": "repo-1--app",
            "runtimeEnv": [{"key": "PUBLIC_URL", "value": "https://user:password@example.test"}],
        },
        {"serviceId": "repo-1--app", "buildContext": "../../other-repository"},
        {"serviceId": "repo-1--app", "dockerfilePath": "/etc/passwd"},
    ],
)
def test_public_request_rejects_sensitive_values_and_escaping_paths(binding):
    with pytest.raises(AnalyzerError):
        validate_request(request(serviceBindings=[binding]))


def test_ai_advice_cannot_introduce_unknown_services_or_evidence():
    graph = seal_document(
        {
            "schemaVersion": "iris.system-graph.v1",
            "components": [{"id": "repo-1--app", "kind": "service", "deployable": True}],
            "relationships": [],
            "questions": [],
            "evidence": [],
            "limitations": [],
        },
        "graphDigest",
    )
    bundle = advisor_input(graph, request())
    advice = {
        "schemaVersion": "iris.system-advice.v1",
        "graphDigest": bundle["graphDigest"],
        "requestDigest": bundle["requestDigest"],
        "summary": "Review the known app",
        "proposedServiceIds": ["repo-1--app"],
        "proposedConnections": [],
        "questions": [],
        "limitations": [],
    }
    assert validate_advice(advice, bundle) == advice
    changed = copy.deepcopy(advice)
    changed["proposedServiceIds"] = ["unknown-repository"]
    with pytest.raises(AnalyzerError):
        validate_advice(changed, bundle)


def test_offline_glob_scope_and_metadata_are_not_silently_published(tmp_path):
    local = sources(tmp_path)
    local["repositories"][0]["metadata"] = {"token": "private-metadata", "path": "/private/hidden"}
    result = asyncio.run(
        OrganizationAnalysisClient().analyze_organization(
            request(includeRepositories=["first-*"]),
            out=tmp_path / "output",
            sources=local,
        )
    )
    saved = json.loads((tmp_path / "output/organization-snapshot.json").read_text())
    assert "private-metadata" not in json.dumps(saved)
    assert saved["repositories"][0]["status"] == "analyzed"
    assert saved["repositories"][1]["status"] == "skipped"
    assert all(row["repositoryId"] == "1" for row in result["plan"]["components"])


def test_replanning_exclusions_preserve_snapshot_and_ref_changes_require_recapture(tmp_path):
    output = tmp_path / "output"
    asyncio.run(
        OrganizationAnalysisClient().analyze_organization(request(), out=output, sources=sources(tmp_path))
    )
    snapshot = json.loads((output / "organization-snapshot.json").read_text())
    original = copy.deepcopy(snapshot)
    result = plan_organization(snapshot, request(excludeRepositories=["second-*"]))
    assert snapshot == original
    assert all(row["repositoryId"] == "1" for row in result["plan"]["components"])
    with pytest.raises(AnalyzerError) as error:
        plan_organization(snapshot, request(refs={"demo-org/first-api": "new-release"}))
    assert error.value.code == "ORGANIZATION_SOURCE_CHANGED"


def test_known_incomplete_inventory_requires_explicit_scope(tmp_path):
    output = tmp_path / "output"
    initial = asyncio.run(
        OrganizationAnalysisClient().analyze_organization(request(), out=output, sources=sources(tmp_path))
    )
    snapshot = json.loads((output / "organization-snapshot.json").read_text())
    snapshot["limitations"].append({"code": "repository_limit", "scope": "inventory", "limit": 2})
    snapshot = seal_document(snapshot, "snapshotDigest")
    result = plan_organization(snapshot, request())
    assert any(
        row["key"] == "inventoryCoverage" and row["requiredForExecution"]
        for row in result["plan"]["questions"]
    )
    selected = [row["id"] for row in initial["graph"]["components"] if row["deployable"]]
    scoped = plan_organization(snapshot, request(selectedServiceIds=selected))
    assert not any(row["key"] == "inventoryCoverage" for row in scoped["plan"]["questions"])
