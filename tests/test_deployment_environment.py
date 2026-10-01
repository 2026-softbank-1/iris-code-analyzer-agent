"""Service-scoped runtime configuration, with no credential values in plans."""

import copy
from pathlib import Path

import pytest

from iris_analyzer.contracts import AnalyzerError
from iris_analyzer.deployment.contracts import (
    normalize_planning_request,
    plan_digest,
    validate_deployment_plan,
)
from iris_analyzer.deployment.planner import create_deployment_plan
from iris_analyzer.deployment.render import compile_plan
from iris_analyzer.pipeline import analyze_snapshot


@pytest.fixture(scope="module")
def analysis():
    return analyze_snapshot(Path(__file__).resolve().parents[1] / "fixtures" / "separated-web-api")


def request_for(analysis):
    request = normalize_planning_request()
    request["target"].update(stack="existing_kubernetes", architecture="x86_64")
    request["bindings"].update(
        existingClusterContext="caller-verified",
        runtimeVerified=True,
        availableCapacity={"cpuMillicores": 8000, "memoryMiB": 16384, "nodeCount": 2},
    )
    for service in analysis["services"]:
        sid = service["serviceId"]
        request["bindings"]["images"].append(
            {"serviceId": sid, "reference": "registry.example/" + sid + "@sha256:" + "a" * 64}
        )
        request["bindings"]["imagePlatforms"][sid] = "amd64"
    return request


def observation(key, component="api", phase="runtime", required=True, **changes):
    return {
        "key": key,
        "component": component,
        "serviceName": None,
        "phase": phase,
        "required": required,
        "origin": "source",
        "evidenceIds": [],
        "condition": None,
        **changes,
    }


def readiness(*items):
    return {"findings": [], "environmentVariables": list(items)}


def test_database_init_and_build_settings_are_not_application_runtime_secrets(analysis):
    request = request_for(analysis)
    api = analysis["services"][1]["serviceId"]
    observations = readiness(
        observation("MONGO_URI"),
        observation("SESSION_SECRET"),
        observation("MONGO_INITDB_ROOT_PASSWORD", "compose:mongo", serviceName="mongo", origin="compose"),
        observation("PRIVATE_BUILD_TOKEN", "web", phase="build"),
        observation("TRUST_PROXY_HOPS", required=False),
        observation("TRUST_PROXY_HOPS", "unowned", phase="unknown", required=None, origin="example"),
    )
    plan = create_deployment_plan(analysis, request, readiness=observations)
    secret_questions = [q for q in plan["questions"] if q["id"].startswith("secret-")]
    assert {q["id"] for q in secret_questions} == {f"secret-{api}-MONGO_URI", f"secret-{api}-SESSION_SECRET"}
    assert all(not q["requiredForExecution"] for q in plan["questions"] if "TRUST_PROXY_HOPS" in q["id"])
    request["bindings"]["secretRefs"] = [
        {"serviceId": api, "environmentKey": key, "name": "api-secrets", "key": key}
        for key in ("MONGO_URI", "SESSION_SECRET")
    ]
    configured = create_deployment_plan(analysis, request, readiness=observations)
    assert configured["executionEligible"]
    assert configured["configuration"]["workloads"][0]["secretRefs"]["value"] == []


def test_runtime_env_and_configmap_reach_native_and_helm_identically(analysis):
    request = request_for(analysis)
    api = analysis["services"][1]["serviceId"]
    request["bindings"]["runtimeEnv"] = [
        {"serviceId": api, "environmentKey": "PUBLIC_URL", "value": "https://app.example"}
    ]
    request["bindings"]["configMapRefs"] = [
        {"serviceId": api, "environmentKey": "TRUST_PROXY_HOPS", "name": "api-settings", "key": "proxy-hops"}
    ]
    plan = create_deployment_plan(
        analysis, request, readiness=readiness(observation("PUBLIC_URL"), observation("TRUST_PROXY_HOPS"))
    )
    result = compile_plan(plan, analysis=analysis)
    assert result["status"] == "ready"
    assert result["helm"]["values"]["resources"] == result["kubernetes"]["manifests"]
    deployments = [r for r in result["kubernetes"]["manifests"] if r["kind"] == "Deployment"]
    assert "env" not in deployments[0]["spec"]["template"]["spec"]["containers"][0]
    assert deployments[1]["spec"]["template"]["spec"]["containers"][0]["env"] == [
        {"name": "PUBLIC_URL", "value": "https://app.example"},
        {
            "name": "TRUST_PROXY_HOPS",
            "valueFrom": {
                "configMapKeyRef": {"name": "api-settings", "key": "proxy-hops", "optional": False}
            },
        },
    ]


def test_unknown_ownership_is_reported_once_and_never_copied_to_all_apps(analysis):
    source = copy.deepcopy(analysis)
    source["environmentKeys"] = [
        {
            "value": "UNSCOPED_TOKEN",
            "status": "detected",
            "scope": "source",
            "evidenceIds": ["e-global-env"],
            "reason": "Legacy global key without consumer metadata",
        }
    ]
    plan = create_deployment_plan(source, request_for(source))
    questions = [q for q in plan["questions"] if "UNSCOPED_TOKEN" in q["id"]]
    assert len(questions) == 1
    assert questions[0]["id"] == "environment-owner-UNSCOPED_TOKEN"
    assert questions[0]["requiredForExecution"]
    assert not plan["executionEligible"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("SESSION_SECRET", "sensitive-value"),
        ("PASSWORD", "sensitive-value"),
        ("PUBLIC_URL", "https://user:password@example.com"),
        ("PUBLIC_URL", "sk-testsecret01234567890123456"),
        ("PUBLIC_URL", "Bearer testsecret01234567890"),
        ("PUBLIC_URL", "$(UNBOUND_VAR)"),
        ("PUBLIC_URL", "text\x00value"),
    ],
)
def test_unsafe_inline_environment_rejected_without_echoing_value(key, value):
    request = normalize_planning_request()
    request["bindings"]["runtimeEnv"] = [{"serviceId": "api", "environmentKey": key, "value": value}]
    with pytest.raises(AnalyzerError) as error:
        normalize_planning_request(request)
    assert value not in str(error.value)
    assert value not in str(error.value.details)


def test_duplicate_sources_and_secret_in_configmap_rejected():
    request = normalize_planning_request()
    request["bindings"]["runtimeEnv"] = [
        {"serviceId": "api", "environmentKey": "PUBLIC_URL", "value": "https://app.example"}
    ]
    request["bindings"]["configMapRefs"] = [
        {"serviceId": "api", "environmentKey": "PUBLIC_URL", "name": "api-settings", "key": "url"}
    ]
    with pytest.raises(AnalyzerError, match="unique"):
        normalize_planning_request(request)
    request["bindings"]["configMapRefs"][0]["environmentKey"] = "SESSION_SECRET"
    with pytest.raises(AnalyzerError, match="Secret references"):
        normalize_planning_request(request)


def test_unknown_service_and_tampered_value_are_not_silently_ignored(analysis):
    request = request_for(analysis)
    request["bindings"]["runtimeEnv"] = [
        {"serviceId": "not-a-service", "environmentKey": "PUBLIC_URL", "value": "https://app.example"}
    ]
    with pytest.raises(AnalyzerError, match="unknown workload"):
        create_deployment_plan(analysis, request)
    request["bindings"]["runtimeEnv"][0]["serviceId"] = analysis["services"][1]["serviceId"]
    plan = create_deployment_plan(analysis, request)
    plan["configuration"]["workloads"][1]["runtimeEnv"]["value"][0]["value"] = "https://attacker.example"
    plan["planDigest"] = plan_digest(plan)
    with pytest.raises(AnalyzerError, match="preserved"):
        validate_deployment_plan(plan, analysis=analysis)


def test_same_key_for_different_services_is_allowed_and_build_phase_is_not_promoted(analysis):
    request = request_for(analysis)
    request["bindings"]["runtimeEnv"] = [
        {
            "serviceId": s["serviceId"],
            "environmentKey": "PUBLIC_URL",
            "value": "https://" + s["root"]["value"] + ".example",
        }
        for s in analysis["services"]
    ]
    plan = create_deployment_plan(
        analysis, request, readiness=readiness(observation("VITE_API_URL", "web", phase="build"))
    )
    assert plan["executionEligible"]
    assert all(
        "VITE_API_URL" not in str(w["runtimeEnv"]["value"]) for w in plan["configuration"]["workloads"]
    )


def test_compose_services_sharing_context_use_exact_identity_and_conditions(analysis):
    from iris_analyzer.contracts import digest

    source = copy.deepcopy(analysis)
    for name, service in zip(("web", "api"), source["services"]):
        service["componentRoots"] = ["."]
        service["root"]["value"] = "."
        service["serviceId"] = "svc-" + digest({"service": name, "root": "."})[:16]
    plan = create_deployment_plan(
        source,
        request_for(source),
        readiness=readiness(
            observation("API_TOKEN", ".", serviceName="api", origin="compose"),
            observation(
                "TLS_SECRET",
                ".",
                serviceName="web",
                origin="compose",
                condition="when TLS profile is selected",
            ),
        ),
    )
    api = source["services"][1]["serviceId"]
    assert [q["id"] for q in plan["questions"] if q["requiredForExecution"]] == [
        "secret-" + api + "-API_TOKEN"
    ]
    assert sum("TLS_SECRET" in q["id"] and not q["requiredForExecution"] for q in plan["questions"]) == 1


def test_malformed_service_identifier_has_contract_error_not_typeerror():
    request = normalize_planning_request()
    request["bindings"]["runtimeEnv"] = [
        {"serviceId": {}, "environmentKey": "PUBLIC_URL", "value": "https://app.example"}
    ]
    with pytest.raises(AnalyzerError):
        normalize_planning_request(request)


def test_examples_and_tooling_outside_app_are_advisory_but_ambiguous_runtime_blocks(analysis):
    observations = readiness(
        observation("QA_BROWSER_EXECUTABLE", ".", required=None),
        observation("MONGO_ROOT_PASSWORD", ".", phase="unknown", required=None, origin="example"),
        observation("APP_PORT", ".", phase="unknown", required=None, origin="example"),
    )
    plan = create_deployment_plan(analysis, request_for(analysis), readiness=observations)
    assert plan["executionEligible"]
    assert all(
        not q["requiredForExecution"] for q in plan["questions"] if q["id"].startswith("environment-owner-")
    )
    source = copy.deepcopy(analysis)
    for service in source["services"]:
        service["componentRoots"] = ["."]
    unresolved = readiness(observation("REQUIRED_API_TOKEN", ".", required=True))
    plan = create_deployment_plan(source, request_for(source), readiness=unresolved)
    assert not plan["executionEligible"]
    assert any(
        q["id"] == "environment-owner-REQUIRED_API_TOKEN" and q["requiredForExecution"]
        for q in plan["questions"]
    )
