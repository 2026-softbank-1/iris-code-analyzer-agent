"""The model trust boundary: provenance, semantic grounding and completeness."""

from copy import deepcopy
from hashlib import sha256

import pytest
from jsonschema import Draft202012Validator

from iris_analyzer.contracts import (
    ANALYSIS_RESULT_SCHEMA,
    CONTEXT_BUNDLE_SCHEMA,
    MODEL_REPLY_SCHEMA,
    AnalyzerError,
    Limits,
    canonical_bytes,
    digest,
    validate_bundle,
    validate_reply,
)
from iris_analyzer.result import static_analysis, validate_analysis


def rehash(bundle):
    bundle["contextHash"] = digest({key: value for key, value in bundle.items() if key != "contextHash"})
    return bundle


@pytest.fixture
def bundle():
    text = 'app.listen(4000, "0.0.0.0")'
    source_digest = sha256(text.encode()).hexdigest()
    data = {
        "schemaVersion": "1",
        "preprocessorVersion": "1",
        "policyVersion": "1",
        "profile": "deployment_v1",
        "revision": 1,
        "source": {"snapshotId": digest("fixture"), "commit": None},
        "manifest": [
            {
                "fileId": "f-main",
                "path": "server/main.js",
                "size": len(text),
                "digest": source_digest,
                "kind": "source",
                "eligible": True,
                "exclusionReason": None,
            }
        ],
        "componentRoots": ["server"],
        "deploymentCandidates": [
            {
                "candidateId": "app",
                "root": "server",
                "role": "api",
                "evidenceIds": ["e-main"],
                "componentRoots": ["server"],
            }
        ],
        "selectedFiles": [
            {
                "fileId": "f-main",
                "path": "server/main.js",
                "role": "entrypoint",
                "selectionReason": "Runtime entrypoint",
                "providedRanges": [{"startLine": 1, "endLine": 1}],
            }
        ],
        "facts": [
            {"key": key, "value": value, "scope": scope, "component": "server", "evidenceIds": ["e-main"]}
            for key, value, scope in [
                ("runtime.name", "node", "source"),
                ("build.command", "none", "source"),
                ("start.command", "node main.js", "container"),
                ("runtime.port", 4000, "container"),
                ("runtime.port", 5173, "development"),
                ("runtime.port", 8080, "host_mapping"),
                ("healthcheck.path", "/health", "source"),
                ("environment.key", "PORT", "source"),
                ("api.route", {"method": "GET", "path": "/health"}, "source"),
                ("frontend.connection", {"baseUrl": "/api"}, "source"),
                ("dependency.database", {"name": "mongo", "engine": "mongodb"}, "container"),
                ("dependency.volume", {"name": "uploads", "mountPath": "/data"}, "container"),
            ]
        ],
        "relations": [],
        "unresolved": [],
        "coverage": {
            "providedEvidenceIds": ["e-main"],
            "omittedRelevantFiles": [],
            "unresolvedReferences": [],
            "truncated": False,
        },
        "evidence": [
            {
                "evidenceId": "e-main",
                "path": "server/main.js",
                "startLine": 1,
                "endLine": 1,
                "sourceDigest": source_digest,
                "contentDigest": source_digest,
                "redacted": False,
                "text": text,
            }
        ],
    }
    return rehash(data)


def reply(bundle):
    return {"kind": "analysis", "result": static_analysis(bundle)}


def test_schemas_are_valid_and_self_contained():
    for schema in (ANALYSIS_RESULT_SCHEMA, CONTEXT_BUNDLE_SCHEMA, MODEL_REPLY_SCHEMA):
        Draft202012Validator.check_schema(schema)
    assert ANALYSIS_RESULT_SCHEMA["$defs"]["service"] == MODEL_REPLY_SCHEMA["$defs"]["service"]


def test_canonical_digest_and_limits():
    assert canonical_bytes({"b": 2, "a": "한글"}) == '{"a":"한글","b":2}'.encode()
    assert digest({"b": 2, "a": 1}) == digest({"a": 1, "b": 2})
    with pytest.raises(AnalyzerError, match="finite"):
        canonical_bytes({"value": float("nan")})
    for params in (
        {"max_bundle_bytes": 0},
        {"max_expansions": -1},
        {"max_file_bytes": True},
        {"max_input_tokens": 0},
    ):
        with pytest.raises(ValueError):
            Limits(**params)


def test_static_preserves_all_deployment_observations(bundle):
    result = static_analysis(bundle)
    assert result["status"] == "complete"
    assert result["coverage"]["completeForProfile"] is True
    assert len(result["services"]) == 1
    service = result["services"][0]
    assert service["startCommand"]["value"] == "node main.js"
    assert {(port["value"], port["scope"]) for port in service["ports"]} == {
        (4000, "container"),
        (5173, "development"),
        (8080, "host_mapping"),
    }
    assert {dep["value"]["kind"] for dep in result["dependencies"]} == {"database", "volume"}
    assert result["apiRoutes"][0]["value"] == {"method": "GET", "path": "/health", "component": "server"}
    assert result["environmentKeys"][0]["value"] == "PORT"


def test_model_omissions_are_restored(bundle):
    model = reply(bundle)
    for name in ("services", "dependencies", "apiRoutes", "environmentKeys", "connections"):
        model["result"][name] = []
    result = validate_analysis(model, bundle)
    assert result == static_analysis(bundle)


@pytest.mark.parametrize(
    "target",
    ["runtime", "ports", "healthchecks", "dependencies", "apiRoutes", "environmentKeys", "connections"],
)
def test_unknown_evidence_is_rejected_in_every_collection(bundle, target):
    model = reply(bundle)
    if target in {"runtime", "ports", "healthchecks"}:
        field = model["result"]["services"][0][target]
        field = field[0] if isinstance(field, list) else field
    else:
        field = model["result"][target][0]
    field["evidenceIds"] = ["fabricated"]
    with pytest.raises(AnalyzerError) as raised:
        validate_analysis(model, bundle)
    assert raised.value.code == "RESULT_EVIDENCE_INVALID"


@pytest.mark.parametrize(
    "nested",
    [
        {"nested": [{"evidenceIds": ["fabricated"]}]},
        {"evidenceId": "e-main", "sourceDigest": "0" * 64, "path": "server/main.js"},
        {"evidenceId": "e-main", "path": "other.js"},
        {"sourceDigest": "0" * 64, "path": "server/main.js"},
        {"evidenceId": "e-main", "startLine": 900},
    ],
)
def test_recursive_evidence_references_are_checked(bundle, nested):
    model = reply(bundle)
    model["result"]["connections"].append(
        {
            "value": nested,
            "status": "suggested",
            "scope": "source",
            "evidenceIds": ["e-main"],
            "reason": "Proposal",
        }
    )
    with pytest.raises(AnalyzerError) as raised:
        validate_analysis(model, bundle)
    assert raised.value.code == "RESULT_EVIDENCE_INVALID"


@pytest.mark.parametrize("mutation", ["digest", "content", "path", "range", "coverage", "duplicate"])
def test_bundle_provenance_checks(bundle, mutation):
    changed = deepcopy(bundle)
    if mutation == "digest":
        changed["evidence"][0]["sourceDigest"] = "0" * 64
    elif mutation == "content":
        changed["evidence"][0]["text"] = "Fabricated evidence"
    elif mutation == "path":
        changed["evidence"][0]["path"] = "other.js"
    elif mutation == "range":
        changed["evidence"][0]["endLine"] = 2
    elif mutation == "coverage":
        changed["coverage"]["providedEvidenceIds"] = []
    else:
        changed["manifest"].append(deepcopy(changed["manifest"][0]))
    rehash(changed)
    with pytest.raises(AnalyzerError) as raised:
        static_analysis(changed)
    assert raised.value.code == "RESULT_EVIDENCE_INVALID"


def test_context_hash_is_verified(bundle):
    bundle["revision"] = 2
    with pytest.raises(AnalyzerError) as raised:
        static_analysis(bundle)
    assert raised.value.code == "RESULT_EVIDENCE_INVALID"


@pytest.mark.parametrize("text,end_line", [("first line\n", 2), ("", 1), ("first line\n\n", 3)])
def test_excerpt_line_count_preserves_trailing_and_empty_source_lines(bundle, text, end_line):
    evidence = bundle["evidence"][0]
    evidence.update(text=text, endLine=end_line, contentDigest=sha256(text.encode()).hexdigest())
    bundle["selectedFiles"][0]["providedRanges"][0]["endLine"] = end_line
    assert static_analysis(rehash(bundle))["status"] == "complete"


@pytest.mark.parametrize("name", ["contextHash", "sourceSnapshotId"])
def test_model_snapshot_binding(bundle, name):
    model = reply(bundle)
    model["result"][name] = "0" * 64
    with pytest.raises(AnalyzerError) as raised:
        validate_analysis(model, bundle)
    assert raised.value.code == "RESULT_CONTEXT_MISMATCH"


def test_detected_wrong_value_scope_and_semantic_field_rejected(bundle):
    for mutation in ("value", "scope", "semantic"):
        model = reply(bundle)
        if mutation == "value":
            model["result"]["services"][0]["runtime"]["value"] = "python"
        elif mutation == "scope":
            container = next(
                port for port in model["result"]["services"][0]["ports"] if port["scope"] == "container"
            )
            container["scope"] = "development"
        else:
            model["result"]["environmentKeys"][0]["value"] = "/health"
        with pytest.raises(AnalyzerError) as raised:
            validate_analysis(model, bundle)
        assert raised.value.code == "RESULT_OBSERVATION_INVALID"


def test_valid_unrelated_evidence_does_not_ground_a_detected_value(bundle):
    second = deepcopy(bundle["evidence"][0])
    second["evidenceId"] = "e-unrelated"
    bundle["evidence"].append(second)
    bundle["coverage"]["providedEvidenceIds"].append("e-unrelated")
    rehash(bundle)
    model = reply(bundle)
    model["result"]["services"][0]["runtime"]["evidenceIds"] = ["e-unrelated"]
    with pytest.raises(AnalyzerError) as raised:
        validate_analysis(model, bundle)
    assert raised.value.code == "RESULT_OBSERVATION_INVALID"


def test_detected_field_rejects_unrelated_citation_alongside_valid_one(bundle):
    second = deepcopy(bundle["evidence"][0])
    second["evidenceId"] = "e-unrelated"
    bundle["evidence"].append(second)
    bundle["coverage"]["providedEvidenceIds"].append("e-unrelated")
    rehash(bundle)
    model = reply(bundle)
    model["result"]["services"][0]["runtime"]["evidenceIds"].append("e-unrelated")
    with pytest.raises(AnalyzerError) as raised:
        validate_analysis(model, bundle)
    assert raised.value.code == "RESULT_OBSERVATION_INVALID"


def test_short_detected_collection_values_do_not_duplicate_component_facts(bundle):
    model = reply(bundle)
    for name in ("apiRoutes", "connections", "dependencies"):
        for field in model["result"][name]:
            field["value"].pop("component", None)
            field["value"].pop("kind", None)
    assert validate_analysis(model, bundle) == static_analysis(bundle)


def test_explicit_unknown_model_collection_blocks_complete(bundle):
    model = reply(bundle)
    model["result"]["dependencies"].append(
        {
            "value": None,
            "status": "unknown",
            "scope": "production",
            "evidenceIds": [],
            "reason": "Storage target requires configuration",
        }
    )
    result = validate_analysis(model, bundle)
    assert result["status"] == "complete"
    assert not any(question["key"] == "dependencies" for question in result["questions"])
    assert not any(field["status"] == "unknown" for field in result["dependencies"])


def test_unsupported_suggested_conflict_preserves_static_value_without_false_blocker(bundle):
    model = reply(bundle)
    model["result"]["services"][0]["startCommand"].update(status="suggested", value="npm run different")
    result = validate_analysis(model, bundle)
    assert result["services"][0]["startCommand"]["value"] == "node main.js"
    assert result["services"][0]["startCommand"]["status"] == "detected"
    assert not any("npm run different" in question["reason"] for question in result["questions"])
    assert result["status"] == "complete"


def test_valid_detected_alternatives_in_different_scopes_are_not_conflicts(bundle):
    bundle["facts"].append(
        {
            "key": "build.command",
            "value": "npm run container-build",
            "scope": "container",
            "component": "server",
            "evidenceIds": ["e-main"],
        }
    )
    rehash(bundle)
    model = reply(bundle)
    model["result"]["services"][0]["buildCommand"].update(value="none", scope="source")
    result = validate_analysis(model, bundle)
    assert result["services"][0]["buildCommand"]["value"] == "npm run container-build"
    assert result["status"] == "complete"
    assert not result["questions"]


@pytest.mark.parametrize(
    "collection,change",
    [
        ("dependencies", {"engine": "postgresql"}),
        ("connections", {"baseUrl": "https://example.invalid/api"}),
        ("apiRoutes", {"authenticated": True}),
    ],
)
def test_unsupported_collection_proposal_is_quarantined_without_poisoning_baseline(
    bundle, collection, change
):
    model = reply(bundle)
    proposal = next(
        field
        for field in model["result"][collection]
        if collection != "dependencies" or field["value"].get("kind") == "database"
    )
    proposal["status"] = "suggested"
    proposal["value"].update(change)
    result = validate_analysis(model, bundle)
    assert any(field["status"] == "detected" for field in result[collection])
    assert not any(field["status"] == "suggested" for field in result[collection])
    assert not any(question["key"] == collection for question in result["questions"])
    assert result["status"] == "complete"


def test_suggestions_never_elevate_unknown_build_to_complete(bundle):
    bundle["facts"] = [fact for fact in bundle["facts"] if fact["key"] != "build.command"]
    rehash(bundle)
    model = reply(bundle)
    model["result"]["services"][0]["buildCommand"].update(
        value="npm run build",
        status="suggested",
        evidenceIds=["e-main"],
        reason="Model recommendation",
    )
    model["result"]["status"] = "complete"
    model["result"]["coverage"] = {"completeForProfile": True, "limitations": []}
    result = validate_analysis(model, bundle)
    assert result["services"][0]["buildCommand"]["status"] == "unknown"
    assert result["status"] == "needs_input"


def test_fabricated_service_and_component_association_rejected(bundle):
    for mutation in ("service", "component"):
        model = reply(bundle)
        if mutation == "service":
            model["result"]["services"][0]["serviceId"] = "mongo-as-app"
        else:
            model["result"]["services"][0]["componentRoots"] = ["other"]
        with pytest.raises(AnalyzerError) as raised:
            validate_analysis(model, bundle)
        assert raised.value.code == "RESULT_OBSERVATION_INVALID"


@pytest.mark.parametrize(
    "condition",
    ["unresolved", "omitted", "references", "truncated", "no_build", "no_start", "no_container_port"],
)
def test_complete_is_recomputed_for_every_mandatory_gap(bundle, condition):
    if condition == "unresolved":
        bundle["unresolved"].append({"key": "api.route", "reason": "Dynamic route expression"})
    elif condition == "omitted":
        bundle["coverage"]["omittedRelevantFiles"].append("missing.js")
    elif condition == "references":
        bundle["coverage"]["unresolvedReferences"].append("./unknown.js")
    elif condition == "truncated":
        bundle["coverage"]["truncated"] = True
    else:
        key = {"no_build": "build.command", "no_start": "start.command", "no_container_port": "runtime.port"}[
            condition
        ]
        bundle["facts"] = [
            fact
            for fact in bundle["facts"]
            if not (fact["key"] == key and (condition != "no_container_port" or fact["scope"] == "container"))
        ]
    rehash(bundle)
    result = static_analysis(bundle)
    assert result["status"] == "needs_input"
    assert result["coverage"]["completeForProfile"] is False
    assert result["questions"]


def test_unsupported_and_static_app_requirements(bundle):
    unsupported = deepcopy(bundle)
    unsupported["deploymentCandidates"] = []
    assert static_analysis(rehash(unsupported))["status"] == "unsupported"
    bundle["deploymentCandidates"][0]["role"] = "static"
    assert static_analysis(rehash(bundle))["status"] == "needs_input"
    bundle["facts"].append(
        {
            "key": "output.directory",
            "value": "dist",
            "scope": "source",
            "component": "server",
            "evidenceIds": ["e-main"],
        }
    )
    assert static_analysis(rehash(bundle))["status"] == "complete"


def test_known_deployment_role_does_not_make_unsupported_runtime_complete(bundle):
    for fact in bundle["facts"]:
        if fact["key"] == "runtime.name":
            fact["value"] = "python"
    result = static_analysis(rehash(bundle))
    assert result["status"] == "unsupported"
    assert result["coverage"]["completeForProfile"] is False
    assert result["services"][0]["startCommand"]["status"] == "detected"


def test_malformed_unknown_framework_value_remains_unsupported_without_crash(bundle):
    bundle["facts"] = [fact for fact in bundle["facts"] if fact["key"] != "runtime.name"]
    bundle["facts"].append(
        {
            "key": "framework",
            "value": {"unrecognized": True},
            "scope": "source",
            "component": "server",
            "evidenceIds": ["e-main"],
        }
    )
    assert static_analysis(rehash(bundle))["status"] == "unsupported"


def test_static_conflicting_ports_are_preserved_and_block_completion(bundle):
    fact = deepcopy(
        next(
            item for item in bundle["facts"] if item["key"] == "runtime.port" and item["scope"] == "container"
        )
    )
    fact["value"] = 3000
    bundle["facts"].append(fact)
    result = static_analysis(rehash(bundle))
    assert {port["value"] for port in result["services"][0]["ports"] if port["scope"] == "container"} == {
        3000,
        4000,
    }
    assert result["status"] == "needs_input"
    assert any("Conflicting container ports" in question["reason"] for question in result["questions"])


def test_strict_reply_schema_rejects_extra_and_evidence_less_fields(bundle):
    model = reply(bundle)
    model["result"]["confidence"] = 1
    with pytest.raises(AnalyzerError) as raised:
        validate_reply(model)
    assert raised.value.code == "RESULT_SCHEMA_INVALID"
    model = reply(bundle)
    model["result"]["services"][0]["runtime"]["evidenceIds"] = []
    with pytest.raises(AnalyzerError):
        validate_reply(model)
    model = reply(bundle)
    model["result"]["services"][0]["runtime"].update(status="unknown", value="node")
    with pytest.raises(AnalyzerError):
        validate_reply(model)


def test_excluded_unread_manifest_entry_may_have_no_digest(bundle):
    bundle["manifest"].append(
        {
            "fileId": "f-env",
            "path": ".env",
            "size": 10,
            "digest": None,
            "kind": "secret",
            "eligible": False,
            "exclusionReason": "Secret",
        }
    )
    assert validate_bundle(rehash(bundle)) is bundle
    assert static_analysis(bundle)["status"] == "complete"


def shared_context_bundle(bundle):
    data = deepcopy(bundle)
    data["componentRoots"] = ["."]
    data["deploymentCandidates"] = [
        {
            "candidateId": candidate_id,
            "root": ".",
            "role": role,
            "evidenceIds": ["e-main"],
            "componentRoots": ["."],
        }
        for candidate_id, role in [("web", "static"), ("api", "api")]
    ]
    data["facts"] = [
        {
            "key": key,
            "value": value,
            "scope": scope,
            "component": ".",
            "evidenceIds": ["e-main"],
            "candidateId": target,
        }
        for key, value, scope, target in [
            ("framework", "vite", "source", "web"),
            ("runtime.name", "nginx", "container", "web"),
            ("build.command", "npm run build-web", "container", "web"),
            ("start.command", "nginx -g 'daemon off;'", "container", "web"),
            ("output.directory", "dist", "production", "web"),
            ("runtime.port", 8080, "container", "web"),
            ("runtime.name", "node", "container", "api"),
            ("build.command", "npm run build-api", "container", "api"),
            ("start.command", "node api.js", "container", "api"),
            ("runtime.port", 4000, "container", "api"),
        ]
    ]
    # A broad Docker default cannot replace explicit facts for either Compose target.
    text = "EXPOSE 9999"
    source_digest = sha256(text.encode()).hexdigest()
    data["manifest"].append(
        {
            "fileId": "f-default",
            "path": "Dockerfile.default",
            "size": len(text),
            "digest": source_digest,
            "kind": "configuration",
            "eligible": True,
            "exclusionReason": None,
        }
    )
    data["selectedFiles"].append(
        {
            "fileId": "f-default",
            "path": "Dockerfile.default",
            "role": "build",
            "selectionReason": "Broad default metadata",
            "providedRanges": [{"startLine": 1, "endLine": 1}],
        }
    )
    data["evidence"].append(
        {
            "evidenceId": "e-default",
            "path": "Dockerfile.default",
            "startLine": 1,
            "endLine": 1,
            "sourceDigest": source_digest,
            "contentDigest": source_digest,
            "redacted": False,
            "text": text,
        }
    )
    data["coverage"]["providedEvidenceIds"].append("e-default")
    data["facts"].append(
        {
            "key": "runtime.port",
            "value": 9999,
            "scope": "container",
            "component": ".",
            "evidenceIds": ["e-default"],
        }
    )
    return rehash(data)


def test_same_build_context_keeps_candidate_specific_execution_facts_separate(bundle):
    data = shared_context_bundle(bundle)
    result = static_analysis(data)
    services = {service["serviceId"]: service for service in result["services"]}
    web, api = services["web"], services["api"]
    assert result["status"] == "complete"
    assert web["runtime"]["value"] == "nginx"
    assert api["runtime"]["value"] == "node"
    assert web["startCommand"]["value"] == "nginx -g 'daemon off;'"
    assert api["startCommand"]["value"] == "node api.js"
    assert web["buildCommand"]["value"] == "npm run build-web"
    assert api["buildCommand"]["value"] == "npm run build-api"
    assert [port["value"] for port in web["ports"]] == [8080]
    assert [port["value"] for port in api["ports"]] == [4000]
    assert web["outputDirectory"]["value"] == "dist"
    assert api["outputDirectory"]["status"] == "unknown"
    assert validate_analysis({"kind": "analysis", "result": result}, data) == result


def test_model_cannot_copy_another_candidates_same_root_port(bundle):
    data = shared_context_bundle(bundle)
    model = reply(data)
    api = next(service for service in model["result"]["services"] if service["serviceId"] == "api")
    api["ports"][0]["value"] = 8080
    with pytest.raises(AnalyzerError) as raised:
        validate_analysis(model, data)
    assert raised.value.code == "RESULT_OBSERVATION_INVALID"


def test_targeted_unsupported_runtime_is_not_overridden_by_shared_source_node(bundle):
    data = shared_context_bundle(bundle)
    data["deploymentCandidates"] = [
        candidate for candidate in data["deploymentCandidates"] if candidate["candidateId"] == "api"
    ]
    data["facts"] = [fact for fact in data["facts"] if fact.get("candidateId") != "web"]
    for fact in data["facts"]:
        if fact["key"] == "runtime.name" and fact.get("candidateId") == "api":
            fact["value"] = "python"
    data["facts"].append(
        {
            "key": "runtime.name",
            "value": "node",
            "scope": "source",
            "component": ".",
            "evidenceIds": ["e-main"],
        }
    )
    result = static_analysis(rehash(data))
    assert result["status"] == "unsupported"
    assert result["services"][0]["runtime"]["value"] == "python"


def test_candidate_scoped_observation_rejects_unknown_target(bundle):
    bundle["facts"][0]["candidateId"] = "fabricated"
    with pytest.raises(AnalyzerError) as raised:
        static_analysis(rehash(bundle))
    assert raised.value.code == "RESULT_EVIDENCE_INVALID"


def test_candidate_scoped_observation_rejects_another_component(bundle):
    bundle["componentRoots"].append("client")
    bundle["facts"][0].update(candidateId="app", component="client")
    with pytest.raises(AnalyzerError) as raised:
        static_analysis(rehash(bundle))
    assert raised.value.code == "RESULT_EVIDENCE_INVALID"


@pytest.mark.parametrize(
    "key,field",
    [
        ("build.command", "buildCommand"),
        ("start.command", "startCommand"),
        ("output.directory", "outputDirectory"),
    ],
)
def test_redacted_mandatory_values_require_secure_user_configuration(bundle, key, field):
    if key == "output.directory":
        bundle["deploymentCandidates"][0]["role"] = "static"
        bundle["facts"].append(
            {
                "key": key,
                "value": "dist/<REDACTED>",
                "scope": "production",
                "component": "server",
                "evidenceIds": ["e-main"],
            }
        )
    else:
        observation = next(fact for fact in bundle["facts"] if fact["key"] == key)
        observation["value"] = "node server.js --token <REDACTED>"
    result = static_analysis(rehash(bundle))
    assert result["status"] == "needs_input"
    assert result["coverage"]["completeForProfile"] is False
    value = result["services"][0][field]
    assert value["status"] == "unknown" and value["value"] is None
    assert value["evidenceIds"] == ["e-main"]
    assert any(
        question["kind"] == "user_configuration" and question["key"] == f"app.{field}"
        for question in result["questions"]
    )
    assert any("<REDACTED>" in str(fact["value"]) for fact in bundle["facts"] if fact["key"] == key)


@pytest.mark.parametrize("field", ["root", "role"])
def test_redacted_candidate_identity_never_completes(bundle, field):
    bundle["deploymentCandidates"][0][field] = "<REDACTED>"
    result = static_analysis(rehash(bundle))
    assert result["status"] != "complete"
    assert result["services"][0][field]["status"] == "unknown"
    assert any(question["kind"] == "user_configuration" for question in result["questions"])


def test_redacted_port_never_qualifies_as_confirmed_container_port(bundle):
    port = next(
        fact for fact in bundle["facts"] if fact["key"] == "runtime.port" and fact["scope"] == "container"
    )
    port["value"] = "<REDACTED>"
    result = static_analysis(rehash(bundle))
    assert result["status"] == "needs_input"
    assert not any(
        field["status"] == "detected" and field["scope"] == "container"
        for field in result["services"][0]["ports"]
    )


def test_model_cannot_re_elevate_a_masked_observation_into_execution_config(bundle):
    fact = next(fact for fact in bundle["facts"] if fact["key"] == "start.command")
    fact["value"] = ["node", "server.js", "--token", "<REDACTED>"]
    rehash(bundle)
    model = reply(bundle)
    model["result"]["services"][0]["startCommand"].update(status="detected", value=deepcopy(fact["value"]))
    result = validate_analysis(model, bundle)
    assert result["status"] == "needs_input"
    assert result["services"][0]["startCommand"]["status"] == "unknown"
    assert result["services"][0]["startCommand"]["value"] is None


def workdir_bundle(bundle, value="/app/server"):
    data = deepcopy(bundle)
    data["componentRoots"].append(".")
    data["deploymentCandidates"][0]["root"] = "."
    data["facts"].append(
        {
            "key": "docker.workdir",
            "value": value,
            "scope": "container",
            "component": "server",
            "candidateId": "app",
            "evidenceIds": ["e-main"],
        }
    )
    return rehash(data)


def test_container_working_directory_preserved_separately_from_source_root(bundle):
    data = workdir_bundle(bundle)
    result = static_analysis(data)
    service = result["services"][0]
    assert result["status"] == "complete"
    assert service["root"]["value"] == "."
    assert service["workingDirectory"] == {
        "value": "/app/server",
        "status": "detected",
        "scope": "container",
        "evidenceIds": ["e-main"],
        "reason": "Static observation: docker.workdir",
    }


def test_optional_working_directory_omitted_by_old_model_reply_is_restored(bundle):
    data = workdir_bundle(bundle)
    model = reply(data)
    model["result"]["services"][0].pop("workingDirectory")
    validate_reply(model)
    assert validate_analysis(model, data) == static_analysis(data)


def test_wrong_detected_working_directory_is_rejected(bundle):
    data = workdir_bundle(bundle)
    model = reply(data)
    model["result"]["services"][0]["workingDirectory"]["value"] = "/app/client"
    with pytest.raises(AnalyzerError) as raised:
        validate_analysis(model, data)
    assert raised.value.code == "RESULT_OBSERVATION_INVALID"


def test_missing_optional_working_directory_does_not_change_profile_completeness(bundle):
    result = static_analysis(bundle)
    assert result["services"][0]["workingDirectory"]["status"] == "unknown"
    assert result["status"] == "complete"


def test_redacted_optional_working_directory_is_safe_and_requires_configuration(bundle):
    data = workdir_bundle(bundle, "/app/<REDACTED>")
    result = static_analysis(data)
    field = result["services"][0]["workingDirectory"]
    assert field["status"] == "unknown" and field["value"] is None
    assert field["evidenceIds"] == ["e-main"]
    assert result["status"] == "needs_input"
    assert any(
        question["key"] == "app.workingDirectory" and question["kind"] == "user_configuration"
        for question in result["questions"]
    )
    model = {"kind": "analysis", "result": deepcopy(result)}
    model["result"]["services"][0]["workingDirectory"].update(status="detected", value="/app/<REDACTED>")
    assert validate_analysis(model, data)["services"][0]["workingDirectory"]["status"] == "unknown"
