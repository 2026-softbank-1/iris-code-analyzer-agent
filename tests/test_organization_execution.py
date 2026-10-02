"""Organization execution boundaries. These tests never contact real services."""

import copy
import json

import pytest

from iris_analyzer.build.source import stage_local_source
from iris_analyzer.contracts import AnalyzerError, digest
from iris_analyzer.organization import execution
from iris_analyzer.organization.execution import (
    compile_system_plan,
    execute_system_bundle,
    write_system_bundle,
)
from iris_analyzer.organization.planner import build_system_plan, system_plan_digest

IMAGE = "ghcr.io/example/api@sha256:" + "a" * 64
WEB_IMAGE = "ghcr.io/example/web@sha256:" + "b" * 64


def field(value):
    return {"value": value, "confidence": "confirmed", "evidenceIds": []}


def component(repo_id, service_id, role="api"):
    sid = f"repo-{repo_id}--{service_id}"
    return {
        "id": sid,
        "repositoryId": str(repo_id),
        "sourceServiceId": service_id,
        "name": service_id,
        "kind": "service",
        "deployable": True,
        "selected": True,
        "status": "detected",
        "root": field("."),
        "role": field(role),
        "ports": [field(8080)],
        "evidenceRefs": [],
    }


def inputs():
    components = [component(1, "api"), component(2, "web", "static")]
    graph = {
        "schemaVersion": "iris.system-graph.v1",
        "organization": "example",
        "purpose": "Two-repository application",
        "environment": "preview",
        "repositories": [],
        "components": components,
        "relationships": [],
        "questions": [],
        "limitations": [],
        "evidence": [],
    }
    records = [
        {"repositoryId": "1", "commitSha": "1" * 40, "sourceSnapshotId": "c" * 64},
        {"repositoryId": "2", "commitSha": "2" * 40, "sourceSnapshotId": "d" * 64},
    ]
    request = {
        "schemaVersion": "iris.organization-request.v1",
        "organization": "example",
        "purpose": graph["purpose"],
        "environment": "preview",
        "selectedServiceIds": [],
        "target": {
            "kind": "existing_kubernetes",
            "context": "reviewed-cluster",
            "namespace": "iris-example",
            "architecture": "amd64",
        },
        "serviceBindings": [
            {"serviceId": components[0]["id"], "imageReference": IMAGE},
            {"serviceId": components[1]["id"], "imageReference": WEB_IMAGE},
        ],
        "connectionBindings": [],
    }
    return graph, records, request


def authorize(plan, bundle, **extra):
    return {
        "planDigest": plan["planDigest"],
        "bundleDigest": bundle["bundleDigest"],
        "context": plan["target"]["context"],
        "namespace": plan["target"]["namespace"],
        "allowDeploy": True,
        "allowBuilds": True,
        "allowPushes": True,
        "approvedHttpUrls": [],
        **extra,
    }


def stage(tmp_path, record):
    original = tmp_path / "original"
    original.mkdir()
    (original / "Dockerfile").write_text('FROM scratch\nCOPY app /app\nENTRYPOINT ["/app"]\n')
    (original / "app").write_bytes(b"binary-fixture")
    staged = tmp_path / "source"
    manifest = stage_local_source(original, staged)
    manifest["origin"] = {
        "kind": "github",
        "repositoryUrl": "https://github.com/example/web",
        "revision": record["commitSha"],
        "requestedRef": "main",
        "uploadId": None,
    }
    record["buildSourceManifest"] = manifest
    return staged


def test_runtime_cycle_does_not_become_cyclic_deployment_order():
    graph, records, request = inputs()
    api, web = graph["components"]
    graph["relationships"] = [
        {
            "id": "forward",
            "fromServiceId": api["id"],
            "toServiceId": web["id"],
            "kind": "http",
            "status": "detected",
            "environmentKey": "WEB_URL",
            "phase": "runtime",
            "evidenceRefs": [],
            "reason": "Source link",
        },
        {
            "id": "reverse",
            "fromServiceId": web["id"],
            "toServiceId": api["id"],
            "kind": "http",
            "status": "user_confirmed",
            "environmentKey": "API_URL",
            "phase": "runtime",
            "evidenceRefs": [],
            "reason": "Operator link",
        },
    ]
    # This cycle represents backend callbacks; a browser consumer needs a
    # public endpoint, tested separately below.
    web["role"] = field("api")
    plan = build_system_plan(graph, records, request)
    assert plan["status"] == "ready"
    assert len(plan["runtimeBindings"]) == 2
    assert all(
        ".iris-example.svc.cluster.local:8080" in binding["value"] for binding in plan["runtimeBindings"]
    )
    assert next(task for task in plan["tasks"] if task["id"] == "apply")["dependsOn"] == [
        "namespace",
        "image:" + api["id"],
        "image:" + web["id"],
    ]
    assert not any(task["id"] == "deploy:" + api["id"] for task in plan["tasks"])


def test_missing_images_cannot_be_execution_ready_and_dry_run_runs_nothing(tmp_path, monkeypatch):
    graph, records, request = inputs()
    request["serviceBindings"][1].pop("imageReference")
    request["serviceBindings"][1]["imageRepository"] = "ghcr.io/example/web"
    stage(tmp_path, records[1])
    plan = build_system_plan(graph, records, request)
    assert plan["status"] == "build_required"
    assert plan["executionEligible"] is False and plan["buildEligible"] is True
    bundle = write_system_bundle(plan, tmp_path / "bundle")
    assert bundle["kubernetes"]["manifests"] == [] and bundle["helm"] is None
    assert not (tmp_path / "bundle/manifests.yaml").exists()
    monkeypatch.setattr(execution, "_run", lambda *args, **kwargs: pytest.fail("dry run executed a tool"))
    result = execute_system_bundle(tmp_path / "bundle")
    assert result["status"] == "dry_run" and result["executionAuthorized"] is False


def test_build_time_frontend_connection_uses_public_url_before_image_build(tmp_path):
    graph, records, request = inputs()
    api, web = graph["components"]
    stage(tmp_path, records[1])
    request["serviceBindings"][1] = {
        "serviceId": web["id"],
        "imageRepository": "ghcr.io/example/web",
        "builder": "dockerfile",
    }
    request["serviceBindings"][0]["publicHost"] = "api.example.com"
    request["deploymentRequest"] = {
        "bindings": {
            "ingressVerified": True,
            "ingress": [
                {
                    "serviceId": api["id"],
                    "host": "api.example.com",
                    "className": "nginx",
                    "tlsSecretName": "api-tls",
                    "path": "/",
                }
            ],
        }
    }
    graph["relationships"] = [
        {
            "id": "frontend-api",
            "fromServiceId": web["id"],
            "toServiceId": api["id"],
            "kind": "http",
            "status": "detected",
            "environmentKey": "VITE_API_URL",
            "phase": "build",
            "evidenceRefs": [],
            "reason": "Build variable",
        }
    ]
    plan = build_system_plan(graph, records, request)
    assert plan["status"] == "build_required"
    assert plan["imageBuilds"][0]["arguments"] == [
        {"key": "VITE_API_URL", "value": "https://api.example.com"}
    ]
    assert plan["runtimeBindings"] == []
    assert plan["buildBindings"][0]["visibility"] == "public"


def test_frontend_internal_dns_binding_is_blocked_without_public_endpoint():
    graph, records, request = inputs()
    api, web = graph["components"]
    graph["relationships"] = [
        {
            "id": "browser",
            "fromServiceId": web["id"],
            "toServiceId": api["id"],
            "kind": "http",
            "status": "detected",
            "environmentKey": "API_URL",
            "phase": "runtime",
            "evidenceRefs": [],
            "reason": "Browser fetch",
        }
    ]
    plan = build_system_plan(graph, records, request)
    assert plan["status"] == "needs_input"
    assert plan["runtimeBindings"] == []
    assert any("Browser/build-time" in question["reason"] for question in plan["questions"])


def test_skipped_repository_does_not_block_selected_service_commit_locks():
    graph, records, request = inputs()
    records.append({"repositoryId": "3", "fullName": "example/docs", "status": "skipped", "commitSha": None})
    plan = build_system_plan(graph, records, request)
    assert plan["status"] == "ready"
    assert not any(question["key"] == "repositories.3.commitSha" for question in plan["questions"])


def test_existing_secret_satisfies_external_database_without_provisioning():
    graph, records, request = inputs()
    api = graph["components"][0]
    graph["components"].append(
        {
            "id": "repo-1--postgres",
            "repositoryId": "1",
            "sourceServiceId": "postgres",
            "kind": "database",
            "deployable": False,
            "selected": False,
        }
    )
    graph["relationships"] = [
        {
            "id": "database",
            "fromServiceId": api["id"],
            "toServiceId": "repo-1--postgres",
            "kind": "database",
            "status": "detected",
            "environmentKey": "DATABASE_URL",
            "phase": "runtime",
            "evidenceRefs": [],
            "reason": "Existing DB",
        }
    ]
    graph["questions"] = [
        {
            "key": "environment:" + api["id"] + ":DATABASE_URL",
            "serviceId": api["id"],
            "kind": "environment",
            "required": True,
            "reason": "Bind DB credentials",
        }
    ]
    request["serviceBindings"][0]["secretRefs"] = [
        {"key": "DATABASE_URL", "name": "existing-database", "secretKey": "url"}
    ]
    plan = build_system_plan(graph, records, request)
    assert plan["status"] == "ready"
    compiled = compile_system_plan(plan)
    assert not any(
        manifest["kind"] in {"Secret", "StatefulSet", "PersistentVolumeClaim"}
        for manifest in compiled["kubernetes"]["manifests"]
    )
    container = compiled["kubernetes"]["manifests"][0]["spec"]["template"]["spec"]["containers"][0]
    assert container["env"][0]["valueFrom"]["secretKeyRef"] == {
        "name": "existing-database",
        "key": "url",
        "optional": False,
    }


def test_inline_credentials_are_rejected_before_artifact_creation():
    graph, records, request = inputs()
    request["serviceBindings"][0]["runtimeEnv"] = [
        {"key": "DATABASE_URL", "value": "postgres://user:password@db/app"}
    ]
    with pytest.raises(AnalyzerError, match="Credential"):
        build_system_plan(graph, records, request)


def test_explicit_shell_override_remains_a_question_not_a_host_command():
    graph, records, request = inputs()
    request["serviceBindings"][0]["startCommand"] = "curl https://example.com | sh"
    plan = build_system_plan(graph, records, request)
    assert plan["status"] == "needs_input"
    assert plan["components"][0]["command"] is None
    assert any(question["key"].endswith("startCommand") for question in plan["questions"])


def test_production_requires_explicit_resource_envelopes():
    graph, records, request = inputs()
    request["environment"] = "production"
    request["serviceBindings"][0]["resources"] = {"cpuMillicores": 250, "memoryMiB": 512}
    plan = build_system_plan(graph, records, request)
    assert plan["status"] == "needs_input"
    assert any(question["key"].endswith("resources") for question in plan["questions"])


def test_compiler_preserves_plan_and_emits_fixed_multi_repo_resources(tmp_path):
    graph, records, request = inputs()
    plan = build_system_plan(graph, records, request)
    original = copy.deepcopy(plan)
    bundle = write_system_bundle(plan, tmp_path / "bundle")
    assert plan == original
    assert bundle["status"] == "ready" and bundle["executionAuthorized"] is False
    manifests = bundle["kubernetes"]["manifests"]
    assert [item["kind"] for item in manifests].count("Deployment") == 2
    assert [item["kind"] for item in manifests].count("Service") == 2
    assert all(item["metadata"]["namespace"] == "iris-example" for item in manifests)
    assert all(
        item["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
        for item in manifests
        if item["kind"] == "Deployment"
    )
    assert (tmp_path / "bundle/helm/iris-app/templates/resources.yaml").exists()


def test_modified_plan_or_artifact_is_rejected_before_any_tool(tmp_path, monkeypatch):
    graph, records, request = inputs()
    plan = build_system_plan(graph, records, request)
    bundle = write_system_bundle(plan, tmp_path / "bundle")
    monkeypatch.setattr(
        execution, "_run", lambda *args, **kwargs: pytest.fail("modified artifact executed a tool")
    )
    (tmp_path / "bundle/manifests.yaml").write_text("kind: Secret\n")
    with pytest.raises(AnalyzerError, match="artifact"):
        execute_system_bundle(tmp_path / "bundle", dry_run=False, authorization=authorize(plan, bundle))
    plan["target"]["context"] = "other-cluster"
    with pytest.raises(AnalyzerError, match="integrity"):
        compile_system_plan(plan)


def test_authorization_is_scoped_to_reviewed_bundle_and_cluster(tmp_path, monkeypatch):
    graph, records, request = inputs()
    plan = build_system_plan(graph, records, request)
    bundle = write_system_bundle(plan, tmp_path / "bundle")
    monkeypatch.setattr(
        execution, "_run", lambda *args, **kwargs: pytest.fail("unapproved executor ran a tool")
    )
    with pytest.raises(AnalyzerError, match="explicit scoped"):
        execute_system_bundle(tmp_path / "bundle", dry_run=False)
    with pytest.raises(AnalyzerError, match="must match"):
        execute_system_bundle(
            tmp_path / "bundle", dry_run=False, authorization=authorize(plan, bundle, context="wrong")
        )


def test_changed_build_source_is_rejected_before_source_execution(tmp_path, monkeypatch):
    graph, records, request = inputs()
    source = stage(tmp_path, records[1])
    request["serviceBindings"][1] = {
        "serviceId": graph["components"][1]["id"],
        "imageRepository": "ghcr.io/example/web",
    }
    plan = build_system_plan(graph, records, request)
    bundle = write_system_bundle(plan, tmp_path / "bundle")
    (source / "app").write_bytes(b"modified")
    monkeypatch.setattr(
        execution, "_run", lambda *args, **kwargs: pytest.fail("changed source executed a tool")
    )
    with pytest.raises(AnalyzerError, match="snapshot"):
        execute_system_bundle(
            tmp_path / "bundle",
            dry_run=False,
            authorization=authorize(plan, bundle),
            source_roots={"2": source},
        )


def test_ambiguous_cluster_mutation_is_journaled_and_never_blindly_retried(tmp_path, monkeypatch):
    graph, records, request = inputs()
    plan = build_system_plan(graph, records, request)
    bundle = write_system_bundle(plan, tmp_path / "bundle")
    calls = []
    fail = {"apply": True}

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["docker", "image", "inspect"]:
            return {"Architecture": "amd64", "Os": "linux", "RepoDigests": [argv[-1]]}
        if argv[0] == "kubectl":
            assert argv[1:5] == ["--context", "reviewed-cluster", "--namespace", "iris-example"]
            if argv[-1].endswith("run/manifests.yaml") and fail["apply"]:
                raise AnalyzerError("SYSTEM_TOOL_FAILED", "Ambiguous apply response")
        return None

    monkeypatch.setattr(execution, "_run", fake_run)
    with pytest.raises(AnalyzerError, match="Ambiguous"):
        execute_system_bundle(tmp_path / "bundle", dry_run=False, authorization=authorize(plan, bundle))
    journal = json.loads((tmp_path / "bundle/run/journal.json").read_text())
    assert journal["operations"][-1]["taskId"] == "apply"
    assert journal["operations"][-1]["status"] == "unknown_outcome"
    previous_calls = len(calls)
    with pytest.raises(AnalyzerError, match="reconciliation"):
        execute_system_bundle(tmp_path / "bundle", dry_run=False, authorization=authorize(plan, bundle))
    assert len(calls) == previous_calls
    operation_id = journal["operations"][-1]["operationId"]
    decision = execution.reconcile_system_operation(
        tmp_path / "bundle",
        operation_id,
        authorization=authorize(plan, bundle),
        resolution="retry",
    )
    assert decision["status"] == "ready_to_resume"
    fail["apply"] = False
    result = execute_system_bundle(tmp_path / "bundle", dry_run=False, authorization=authorize(plan, bundle))
    assert result["status"] == "complete"
    journal = json.loads((tmp_path / "bundle/run/journal.json").read_text())
    assert journal["reconciliations"][0]["operation"]["status"] == "unknown_outcome"


def test_explicit_uid_preserves_operator_runtime_binding():
    graph, records, request = inputs()
    request["serviceBindings"][0]["runAsUser"] = 10001
    plan = build_system_plan(graph, records, request)
    compiled = compile_system_plan(plan)
    pod = compiled["kubernetes"]["manifests"][0]["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsUser"] == 10001
    assert pod["securityContext"]["runAsNonRoot"] is True
    other = next(
        manifest
        for manifest in compiled["kubernetes"]["manifests"]
        if manifest["kind"] == "Deployment" and manifest["metadata"]["name"] == "repo-2--web"
    )
    assert "runAsUser" not in other["spec"]["template"]["spec"]["securityContext"]


def test_http_checks_require_exact_url_approval_and_do_not_follow_redirects(tmp_path, monkeypatch):
    graph, records, request = inputs()
    request["httpChecks"] = [{"url": "https://app.example.com/health", "timeoutSeconds": 5}]
    plan = build_system_plan(graph, records, request)
    bundle = write_system_bundle(plan, tmp_path / "bundle")
    monkeypatch.setattr(
        execution, "_run", lambda *args, **kwargs: pytest.fail("unapproved HTTP run invoked tools")
    )
    with pytest.raises(AnalyzerError, match="exact URL"):
        execute_system_bundle(tmp_path / "bundle", dry_run=False, authorization=authorize(plan, bundle))


def test_new_infrastructure_adapter_is_a_blocked_bundle(tmp_path):
    graph, records, request = inputs()
    request["target"]["kind"] = "aws_eks"
    plan = build_system_plan(graph, records, request)
    bundle = write_system_bundle(plan, tmp_path / "blocked")
    assert bundle["status"] == "blocked"
    assert {path.name for path in (tmp_path / "blocked").iterdir()} == {"system-plan.json", "execution.json"}


def test_resealed_plan_with_missing_image_is_still_not_execution_ready():
    graph, records, request = inputs()
    plan = build_system_plan(graph, records, request)
    plan["components"][0]["imageReference"] = None
    plan["planDigest"] = system_plan_digest(plan)
    with pytest.raises(AnalyzerError, match="Missing images"):
        compile_system_plan(plan)


def test_output_cannot_overwrite_previous_runnable_bundle(tmp_path):
    graph, records, request = inputs()
    plan = build_system_plan(graph, records, request)
    destination = tmp_path / "bundle"
    destination.mkdir()
    (destination / "stale.yaml").write_text("stale")
    with pytest.raises(AnalyzerError, match="new or empty"):
        write_system_bundle(plan, destination)
    assert (destination / "stale.yaml").read_text() == "stale"


def test_digest_pins_repository_identity_as_well_as_commit():
    graph, records, request = inputs()
    plan = build_system_plan(graph, records, request)
    assert plan["planDigest"] == digest({key: value for key, value in plan.items() if key != "planDigest"})
    assert plan["graphDigest"] == digest(graph)
    assert {(item["repositoryId"], item["commitSha"]) for item in plan["repositories"]} == {
        ("1", "1" * 40),
        ("2", "2" * 40),
    }


def test_generic_source_secrets_and_phase_inputs_are_required_independently_of_http_graph():
    graph, records, request = inputs()
    sid = graph["components"][0]["id"]
    records[0]["analysis"] = {
        "services": [{"serviceId": "api", "componentRoots": ["."], "root": field(".")}],
    }
    records[0]["readiness"] = {
        "environmentVariables": [
            {
                "key": key,
                "component": ".",
                "serviceName": None,
                "phase": phase,
                "required": None,
                "condition": None,
                "origin": "source",
                "evidenceIds": [],
            }
            for key, phase in [
                ("DATABASE_URL", "runtime"),
                ("SESSION_SECRET", "runtime"),
                ("PORT", "runtime"),
                ("ASSET_BASE_URL", "build"),
            ]
        ],
    }
    plan = build_system_plan(graph, records, request)
    assert plan["status"] == "needs_input"
    required = {item["key"] for item in plan["questions"] if item["requiredForExecution"]}
    assert "environment:" + sid + ":runtime:DATABASE_URL" in required
    assert "environment:" + sid + ":runtime:SESSION_SECRET" in required
    assert "environment:" + sid + ":build:ASSET_BASE_URL" in required
    assert any(
        item["serviceId"] == sid and item["key"] == "PORT" and item["value"] == "8080"
        for item in plan["runtimeBindings"]
    )
    request["serviceBindings"][0]["secretRefs"] = [
        {"key": "DATABASE_URL", "name": "existing-database", "secretKey": "url"},
        {"key": "SESSION_SECRET", "name": "existing-session", "secretKey": "secret"},
    ]
    request["serviceBindings"][0]["runtimeEnv"] = [
        {"key": "ASSET_BASE_URL", "value": "https://assets.example.com"}
    ]
    plan = build_system_plan(graph, records, request)
    required = {item["key"] for item in plan["questions"] if item["requiredForExecution"]}
    assert "environment:" + sid + ":runtime:DATABASE_URL" not in required
    assert "environment:" + sid + ":runtime:SESSION_SECRET" not in required
    assert "environment:" + sid + ":build:ASSET_BASE_URL" in required
    request["serviceBindings"][0]["runtimeEnv"].append({"key": "PORT", "value": "9090"})
    with pytest.raises(AnalyzerError, match="listening port"):
        build_system_plan(graph, records, request)


def test_default_repository_dossier_preserves_existing_cluster_target(monkeypatch):
    from iris_analyzer.deployment import planner as repository_planner
    from iris_analyzer.deployment import render as repository_render
    from iris_analyzer.organization.planner import _dossiers

    graph, records, request = inputs()
    records[0]["analysis"] = {"services": [{"serviceId": "api"}]}
    seen = []

    def capture(analysis, local_request):
        seen.append(local_request)
        return {"adapter": local_request["target"]["stack"]}

    monkeypatch.setattr(repository_planner, "create_deployment_plan", capture)
    monkeypatch.setattr(repository_render, "compile_plan", lambda plan, **kwargs: {"status": "blocked"})
    dossiers = _dossiers(records, request)
    assert dossiers[0]["deploymentPlan"]["adapter"] == "existing_kubernetes"
    assert seen[0]["target"] == {
        "stack": "existing_kubernetes",
        "architecture": "x86_64",
        "environment": "test",
    }
    assert seen[0]["bindings"]["existingClusterContext"] == "reviewed-cluster"
    assert seen[0]["bindings"]["namespace"] == "iris-example"
