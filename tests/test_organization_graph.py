"""Organization graph preserves uncertainty, source identity and non-app roles."""

import copy
import json

from iris_analyzer.contracts import digest
from iris_analyzer.organization.graph import build_system_graph, service_component_id


def _field(value, *, evidence="e-main", scope="source", status="detected"):
    return {
        "value": value,
        "status": status,
        "scope": scope,
        "evidenceIds": [evidence] if evidence else [],
        "reason": "Source observation.",
    }


def _service(service_id="app", role="api", root="."):
    return {
        "serviceId": service_id,
        "root": _field(root),
        "role": _field(role),
        "runtime": _field("node"),
        "buildCommand": _field(None, evidence=None, status="unknown"),
        "startCommand": _field("node index.js"),
        "workingDirectory": _field(None, evidence=None, status="unknown"),
        "outputDirectory": _field(None, evidence=None, status="unknown"),
        "ports": [_field(3000, scope="container")],
        "healthchecks": [],
        "componentRoots": [root],
    }


def _record(repository_id, name, role="api"):
    return {
        "repositoryId": str(repository_id),
        "fullName": "demo/" + name,
        "repositoryUrl": "https://github.com/demo/" + name,
        "ref": "main",
        "commitSha": (str(repository_id)[-1] or "a") * 40,
        "sourceSnapshotId": digest({"repositoryId": repository_id}),
        "status": "analyzed",
        "reason": None,
        "analysis": {
            "schemaVersion": "1",
            "status": "needs_input",
            "sourceSnapshotId": digest({"repositoryId": repository_id}),
            "contextHash": digest({"context": repository_id}),
            "services": [_service(role=role)],
            "dependencies": [],
            "connections": [],
            "environmentKeys": [],
            "apiRoutes": [],
            "questions": [],
            "coverage": {"completeForProfile": False, "limitations": []},
        },
        "readiness": {"buildTargets": [], "serviceConnections": [], "environmentVariables": []},
        "evidence": [
            {
                "evidenceId": "e-main",
                "path": "src/index.js",
                "startLine": 2,
                "endLine": 4,
                "text": "Not exported: private-source-snippet",
            }
        ],
    }


def _request(**parts):
    return {
        "organization": "demo",
        "purpose": "Deploy the shopping system.",
        "environment": "test",
        "selectedServiceIds": [],
        "connectionBindings": [],
        "serviceBindings": [],
        **parts,
    }


def test_identical_service_ids_are_qualified_without_mutating_source_analyses():
    records = [_record(1, "web", "frontend"), _record(2, "api"), _record(3, "worker", "worker")]
    original = copy.deepcopy(records)
    graph = build_system_graph(records, _request())
    assert {row["id"] for row in graph["components"]} == {
        "repo-1--app",
        "repo-2--app",
        "repo-3--app",
    }
    assert all(row["sourceServiceId"] == "app" for row in graph["components"])
    assert len(graph["evidence"]) == 3
    assert len({row["id"] for row in graph["evidence"]}) == 3
    assert records == original
    assert graph == build_system_graph(list(reversed(records)), _request())
    source = graph["evidence"][0]
    assert source["path"] == "src/index.js"
    assert source["startLine"] == 2
    assert source["commitSha"] == "1" * 40
    assert "private-source-snippet" not in json.dumps(graph)


def test_dns_normalization_remains_stable_and_collision_resistant():
    original = service_component_id("A_B", "APP/" + "a" * 100)
    assert len(original) <= 63
    assert original == service_component_id("A_B", "APP/" + "a" * 100)
    assert service_component_id("1", "app_x") != service_component_id("1", "app-x")
    assert service_component_id("1", "app-x") == "repo-1--app-x"


def test_localhost_and_matching_ports_do_not_resolve_ambiguous_apis():
    web, api, alternate = _record(1, "web", "frontend"), _record(2, "api"), _record(3, "another-api")
    web["analysis"]["connections"] = [_field({"baseUrl": "http://localhost:3000"})]
    graph = build_system_graph([web, api, alternate], _request())
    assert len(graph["relationships"]) == 1
    relation = graph["relationships"][0]
    assert relation["fromServiceId"] == "repo-1--app"
    assert relation["toServiceId"] is None
    assert relation["status"] == "unknown"
    question = next(row for row in graph["questions"] if row["key"].startswith("http-target"))
    assert question["candidateServiceIds"] == ["repo-2--app", "repo-3--app"]


def test_api_environment_key_is_a_suggestion_even_with_one_candidate():
    web, api = _record(1, "web", "frontend"), _record(2, "api")
    web["readiness"]["environmentVariables"] = [
        {
            "key": "VITE_API_URL",
            "component": ".",
            "serviceName": None,
            "phase": "build",
            "evidenceIds": ["e-main"],
        }
    ]
    graph = build_system_graph([web, api], _request())
    relation = graph["relationships"][0]
    assert relation["status"] == "suggested"
    assert relation["toServiceId"] == "repo-2--app"
    assert relation["phase"] == "build"
    assert any(row["required"] for row in graph["questions"])
    assert graph["questions"][0]["key"] == "environment:repo-1--app:VITE_API_URL"


def test_explicit_user_bindings_allow_runtime_cycles_without_deployment_order():
    first, second = _record(1, "api"), _record(2, "callback-api")
    bindings = [
        {
            "fromServiceId": "repo-1--app",
            "toServiceId": "repo-2--app",
            "kind": "http",
            "environmentKey": "CALLBACK_API_URL",
            "phase": "runtime",
        },
        {
            "fromServiceId": "repo-2--app",
            "toServiceId": "repo-1--app",
            "kind": "http",
            "environmentKey": "API_URL",
            "phase": "runtime",
        },
    ]
    graph = build_system_graph([first, second], _request(connectionBindings=bindings))
    assert len(graph["relationships"]) == 2
    assert {row["status"] for row in graph["relationships"]} == {"user_confirmed"}
    assert not graph["questions"]
    assert "deploymentOrder" not in graph


def test_exact_user_endpoint_binding_resolves_source_connection():
    web, api = _record(1, "web", "frontend"), _record(2, "api")
    web["analysis"]["connections"] = [_field({"baseUrl": "https://api.example.test/v1/"})]
    graph = build_system_graph(
        [web, api],
        _request(
            serviceBindings=[
                {
                    "serviceId": "repo-2--app",
                    "endpoint": "https://api.example.test/v1",
                }
            ]
        ),
    )
    relation = graph["relationships"][0]
    assert relation["status"] == "detected"
    assert relation["toServiceId"] == "repo-2--app"
    assert relation["evidenceRefs"]
    assert not graph["questions"]


def test_different_paths_on_same_origin_do_not_match_endpoint_binding():
    web, api = _record(1, "web", "frontend"), _record(2, "api")
    web["analysis"]["connections"] = [_field({"baseUrl": "https://api.example.test/admin"})]
    graph = build_system_graph(
        [web, api],
        _request(
            serviceBindings=[
                {
                    "serviceId": "repo-2--app",
                    "endpoint": "https://api.example.test/public",
                }
            ]
        ),
    )
    assert graph["relationships"][0]["toServiceId"] is None
    assert graph["relationships"][0]["status"] == "unknown"


def test_openapi_server_observation_carries_both_repositories_evidence():
    web, api = _record(1, "web", "frontend"), _record(2, "api")
    web["analysis"]["connections"] = [_field({"baseUrl": "https://api.example.test/v1"})]
    api["analysis"]["apiRoutes"] = [
        _field(
            {
                "component": ".",
                "servers": [{"url": "https://api.example.test/v1"}],
            }
        )
    ]
    graph = build_system_graph([web, api], _request())
    relation = graph["relationships"][0]
    assert relation["status"] == "detected"
    assert len(relation["evidenceRefs"]) == 2
    assert {row["repositoryId"] for row in graph["evidence"] if row["id"] in relation["evidenceRefs"]} == {
        "1",
        "2",
    }


def test_database_client_declaration_does_not_prove_required_live_database():
    api = _record(1, "api")
    api["analysis"]["dependencies"] = [_field({"name": "postgresql", "engine": "postgresql"})]
    graph = build_system_graph([api], _request())
    dependency = next(row for row in graph["components"] if row["kind"] == "database")
    assert dependency["deployable"] is False
    assert dependency["provisioning"] == "unbound"
    assert graph["relationships"][0]["status"] == "suggested"
    assert graph["relationships"][0]["toServiceId"] == dependency["id"]
    assert any(row["key"].startswith("dependency-binding") for row in graph["questions"])


def test_compose_consumer_metadata_detects_resource_link_without_provisioning():
    api = _record(1, "api")
    service_id = "svc-" + digest({"service": "backend", "root": "."})[:16]
    api["analysis"]["services"] = [_service(service_id)]
    api["analysis"]["dependencies"] = [_field({"name": "mongo", "engine": "mongodb"}, scope="container")]
    api["readiness"]["buildTargets"] = [{"component": ".", "serviceName": "backend"}]
    api["readiness"]["serviceConnections"] = [
        {
            "fromComponent": ".",
            "fromService": "backend",
            "toService": "mongo",
            "protocol": "mongodb",
            "port": 27017,
            "environmentKey": "MONGO_URI",
            "evidenceIds": ["e-main"],
        }
    ]
    graph = build_system_graph([api], _request())
    relation = graph["relationships"][0]
    assert relation["fromServiceId"] == service_component_id("1", service_id)
    assert relation["kind"] == "database"
    assert relation["status"] == "detected"
    dependency = next(row for row in graph["components"] if row["id"] == relation["toServiceId"])
    assert dependency["provisioning"] == "unbound"
    assert any(row["key"].startswith("resource-binding") for row in graph["questions"])


def test_compose_declaration_without_consumer_does_not_link_every_app():
    api = _record(1, "api")
    api["analysis"]["services"].append(_service("worker", "worker"))
    api["analysis"]["dependencies"] = [_field({"name": "mongo", "engine": "mongodb"}, scope="container")]
    graph = build_system_graph([api], _request())
    assert not graph["relationships"]
    assert any(row["kind"] == "database" for row in graph["components"])


def test_non_application_repositories_preserve_source_classification():
    app, infra, docs = _record(1, "shop"), _record(2, "arbitrary-name"), _record(3, "docs")
    for record in (infra, docs):
        record["analysis"]["services"] = []
    infra["evidence"][0]["path"] = "main.tf"
    docs["classification"] = {
        "kind": "documentation",
        "status": "detected",
        "evidenceIds": ["e-main"],
        "reason": "Explicit source document declares this repository is the system documentation.",
    }
    graph = build_system_graph([app, infra, docs], _request())
    assert {row["kind"] for row in graph["components"]} == {"service", "infrastructure", "documentation"}
    assert [row["id"] for row in graph["components"] if row["deployable"]] == ["repo-1--app"]
    assert {row["repositoryId"]: row["classification"]["kind"] for row in graph["repositories"]} == {
        "1": "application",
        "2": "infrastructure",
        "3": "documentation",
    }


def test_repository_name_alone_cannot_classify_library_or_infrastructure():
    library = _record(1, "shared-library-infra")
    library["analysis"]["services"] = []
    library["classification"] = {"kind": "package", "status": "detected", "evidenceIds": []}
    graph = build_system_graph([library], _request())
    assert graph["repositories"][0]["classification"]["kind"] == "unknown"
    assert not graph["components"]
    assert graph["questions"][0]["required"] is False


def test_missing_repository_and_invalid_binding_remain_visible():
    api, failed = _record(1, "api"), _record(2, "private-service")
    failed.update(status="failed", analysis=None, reason="/local/private/path: token-secret")
    graph = build_system_graph(
        [api, failed],
        _request(
            connectionBindings=[
                {
                    "fromServiceId": "repo-1--app",
                    "toServiceId": "repo-2--app",
                    "kind": "http",
                }
            ]
        ),
    )
    assert graph["repositories"][1]["status"] == "failed"
    assert not graph["relationships"]
    assert graph["questions"]
    assert any("private-service" in row for row in graph["limitations"])
    assert "token-secret" not in json.dumps(graph)
    assert "/local/private/path" not in json.dumps(graph)


def test_secret_url_credentials_are_never_copied_into_graph_relationships():
    web = _record(1, "web", "frontend")
    web["analysis"]["connections"] = [
        _field({"baseUrl": "https://user:private-secret@api.example.test/?token=secret"})
    ]
    graph = build_system_graph([web], _request())
    assert graph["relationships"][0]["status"] == "unknown"
    assert "private-secret" not in json.dumps(graph)
    assert "observedUrl" not in graph["relationships"][0]


def test_missing_purpose_requires_system_intent_before_execution():
    graph = build_system_graph([_record(1, "api")], _request(purpose=" "))
    assert next(row for row in graph["questions"] if row["key"] == "purpose")["required"] is True


def test_explicit_port_binding_resolves_required_source_port_obligation():
    api = _record(1, "api")
    api["analysis"]["questions"] = [
        {
            "key": "app.ports",
            "reason": "Confirm the container port.",
            "kind": "user_configuration",
        },
        {
            "key": "app.buildCommand",
            "reason": "Confirm the build command.",
            "kind": "code_review",
        },
    ]
    unbound = build_system_graph([api], _request())
    assert next(row for row in unbound["questions"] if row["key"].endswith("app.ports"))["required"] is True
    bound = build_system_graph([api], _request(serviceBindings=[{"serviceId": "repo-1--app", "port": 3000}]))
    assert not any(row["required"] for row in bound["questions"])


def test_explicit_binding_cannot_suppress_source_grounded_error():
    api = _record(1, "api")
    api["readiness"]["findings"] = [
        {
            "ruleId": "json.syntax",
            "severity": "error",
            "status": "detected",
            "reason": "Source JSON syntax is invalid.",
            "path": "package.json",
            "evidenceIds": ["e-main"],
        }
    ]
    api["runReport"] = {
        "verification": {
            "reviewFindings": [
                {
                    "reason": "Required source declaration conflicts with the selected runtime.",
                    "decision": "supported",
                    "blocking": True,
                    "evidenceIds": ["e-main"],
                }
            ]
        }
    }
    graph = build_system_graph([api], _request(serviceBindings=[{"serviceId": "repo-1--app", "port": 3000}]))
    source_questions = [row for row in graph["questions"] if row["key"].startswith("source-review")]
    assert len(source_questions) == 2
    assert all(row["required"] and row["kind"] == "code_review" for row in source_questions)


def test_unselected_repositories_keep_questions_without_blocking_selected_system():
    api, other, failed = _record(1, "api"), _record(2, "other-web", "frontend"), _record(3, "failed")
    other["readiness"]["environmentVariables"] = [
        {
            "key": "API_URL",
            "component": ".",
            "phase": "runtime",
            "evidenceIds": ["e-main"],
        }
    ]
    failed.update(status="failed", analysis=None)
    graph = build_system_graph([api, other, failed], _request(selectedServiceIds=["repo-1--app"]))
    assert graph["questions"]
    assert not any(row["required"] for row in graph["questions"])


def test_manifest_classification_is_qualified_but_keeps_content_uncertainty():
    infra, docs = _record(1, "infra"), _record(2, "docs")
    for record in (infra, docs):
        record["analysis"]["services"] = []
        record["evidence"] = []
    infra["buildSourceManifest"] = {"files": [{"path": "main.tf", "sha256": "a" * 64}]}
    docs["buildSourceManifest"] = {"files": [{"path": "README.md", "sha256": "b" * 64}]}
    graph = build_system_graph([infra, docs], _request())
    assert {row["classification"]["kind"] for row in graph["repositories"]} == {
        "infrastructure",
        "documentation",
    }
    assert all(row["classification"]["status"] == "suggested" for row in graph["repositories"])
    assert {row["path"] for row in graph["evidence"]} == {"main.tf", "README.md"}
    assert all(row["startLine"] is None for row in graph["evidence"])


def test_synthetic_repository_nodes_cannot_replace_same_named_source_component():
    package = _record(1, "shared-package", "library")
    package["analysis"]["services"] = [_service("repository", "library")]
    graph = build_system_graph([package], _request())
    assert {row["id"] for row in graph["components"]} == {"repo-1--repository", "repository-1"}
    assert (
        next(row for row in graph["components"] if row["id"] == "repo-1--repository")["sourceServiceId"]
        == "repository"
    )


def test_explicit_external_dependency_binding_is_validated_after_nodes_exist():
    api = _record(1, "api")
    api["analysis"]["dependencies"] = [_field({"name": "mongo", "engine": "mongodb"})]
    graph = build_system_graph(
        [api],
        _request(
            connectionBindings=[
                {
                    "fromServiceId": "repo-1--app",
                    "toServiceId": "resource-repo-1-database-mongo",
                    "kind": "database",
                    "environmentKey": "MONGO_URI",
                    "phase": "runtime",
                }
            ]
        ),
    )
    assert any(
        row["status"] == "user_confirmed" and row["environmentKey"] == "MONGO_URI"
        for row in graph["relationships"]
    )
    assert not any(row["key"].startswith("connection-binding-") for row in graph["questions"])
    assert (
        next(row for row in graph["questions"] if row["key"].startswith("dependency-binding-"))["kind"]
        == "dependency"
    )
