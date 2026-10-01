"""Independent admission tests; passing them is not a model-accuracy measurement.

Holdout source names, ports, expressions, and mutations are not prompt examples.
Every hostile proposal uses an actually issued evidence ID unless the test is
explicitly exercising provenance rejection.
"""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from iris_analyzer.contracts import AnalyzerError, digest
from iris_analyzer.preprocess import prepare_context, release_snapshot
from iris_analyzer.result import static_analysis, validate_analysis

HOLDOUT = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/ai-review-holdout/cases.json").read_text()
)
CASES = {case["id"]: case for case in HOLDOUT["cases"]}
ROUTE_CASES = [case for case in HOLDOUT["cases"] if "route" in case]


@pytest.fixture
def source_bundle(tmp_path):
    snapshots = set()

    def create(case):
        for name, content in case["files"].items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        bundle = prepare_context(tmp_path)
        snapshots.add(bundle["source"]["snapshotId"])
        return bundle

    yield create
    for snapshot_id in snapshots:
        release_snapshot(snapshot_id)


def field(value, evidence_ids, scope="source"):
    return {
        "status": "suggested",
        "value": value,
        "scope": scope,
        "evidenceIds": evidence_ids,
        "reason": "Independent acceptance proposal; verify against original source",
    }


def evidence_for(bundle, path):
    ids = [item["evidenceId"] for item in bundle["evidence"] if item["path"] == path]
    assert ids, f"Fixture did not provide evidence from {path}"
    return ids


def route_reply(bundle, case, *, component=None, scope="source", evidence_ids=None):
    reply = {"kind": "analysis", "result": static_analysis(bundle)}
    path = case.get("citationPath", "gateway.cjs")
    reply["result"]["apiRoutes"].append(
        field(
            {"method": "GET", "path": case["route"], "component": component or case["component"]},
            evidence_ids or evidence_for(bundle, path),
            scope,
        )
    )
    return reply


def verify(reply, bundle):
    from iris_analyzer.verification import verify_proposals

    return verify_proposals(reply, bundle)


def matching_route(result, path, component="."):
    return [
        item
        for item in result["apiRoutes"]
        if isinstance(item["value"], dict)
        and item["value"].get("path") == path
        and item["value"].get("component") == component
    ]


def test_holdout_is_not_a_paid_model_result_or_prompt_example():
    assert HOLDOUT["includedInModelPrompt"] is False
    assert "offline" in HOLDOUT["purpose"]
    assert len(CASES) == len(HOLDOUT["cases"])
    prompt_text = "\n".join(
        path.read_text()
        for path in (Path(__file__).parents[1] / "src/iris_analyzer/opencode/prompts").glob("*.txt")
    )
    assert "/ops/alive" not in prompt_text
    assert "4637" not in prompt_text


@pytest.mark.parametrize("case", ROUTE_CASES, ids=[case["id"] for case in ROUTE_CASES])
def test_literal_route_support_and_binding_mutations_through_public_admission(case, source_bundle):
    bundle = source_bundle(case)
    baseline = static_analysis(bundle)
    assert not matching_route(baseline, case["route"], case["component"])
    reply = route_reply(bundle, case)
    sanitized, report = verify(reply, bundle)
    merged = validate_analysis(reply, bundle)
    expected = case["decision"] == "supported"
    assert bool(matching_route(sanitized["result"], case["route"], case["component"])) is expected
    assert bool(matching_route(merged, case["route"], case["component"])) is expected
    decisions = [item for item in report["decisions"] if "apiRoutes" in item["fieldPath"]]
    assert decisions and (decisions[-1]["decision"] == "supported") is expected
    assert report["sourceSnapshotId"] == bundle["source"]["snapshotId"]
    assert report["contextHash"] == bundle["contextHash"]
    assert report["baselineDigest"] == digest(baseline)
    if expected:
        assert matching_route(merged, case["route"])[0]["status"] == "suggested"


@pytest.mark.parametrize("scope,component", [("container", "."), ("production", "."), ("source", "absent")])
def test_supported_expression_cannot_be_relabelled_as_another_scope_or_component(
    scope, component, source_bundle
):
    case = CASES["composed-diagnostics-route"]
    bundle = source_bundle(case)
    reply = route_reply(bundle, case, scope=scope, component=component)
    sanitized, report = verify(reply, bundle)
    assert not matching_route(sanitized["result"], case["route"], component)
    assert any(item["decision"] != "supported" for item in report["decisions"])
    assert not matching_route(validate_analysis(reply, bundle), case["route"], component)


def test_real_id_from_unrelated_readme_does_not_support_a_real_but_uncited_route(source_bundle):
    case = deepcopy(CASES["composed-diagnostics-route"])
    case["files"]["README.md"] = "# Operator note\nThis service has a purple wordmark.\n"
    bundle = source_bundle(case)
    reply = route_reply(bundle, case, evidence_ids=evidence_for(bundle, "README.md"))
    sanitized, _ = verify(reply, bundle)
    assert not matching_route(sanitized["result"], case["route"])
    assert not matching_route(validate_analysis(reply, bundle), case["route"])


@pytest.mark.parametrize("extra", [{"readiness": True}, {"authenticated": False}, {"runtimeStatus": 200}])
def test_proved_route_does_not_support_extra_operational_claims(extra, source_bundle):
    case = CASES["composed-diagnostics-route"]
    bundle = source_bundle(case)
    reply = route_reply(bundle, case)
    reply["result"]["apiRoutes"][-1]["value"].update(extra)
    sanitized, _ = verify(reply, bundle)
    assert not matching_route(sanitized["result"], case["route"])
    assert not matching_route(validate_analysis(reply, bundle), case["route"])


def test_valid_listener_id_cannot_admit_postgresql_and_does_not_poison_baseline(source_bundle):
    case = CASES["documentation-advisory"]
    bundle = source_bundle(case)
    baseline = static_analysis(bundle)
    reply = {"kind": "analysis", "result": deepcopy(baseline)}
    reply["result"]["dependencies"].append(
        field(
            {"name": "postgresql", "engine": "postgresql", "host": "db.invalid", "port": 5432},
            evidence_for(bundle, "gateway.cjs"),
            "production",
        )
    )
    merged = validate_analysis(reply, bundle)
    assert merged["dependencies"] == baseline["dependencies"]
    assert merged["status"] == baseline["status"]
    assert merged["services"] == baseline["services"]


@pytest.mark.parametrize("addition", [{"host": "database.internal"}, {"required": True}, {"port": 5432}])
def test_installed_postgres_client_does_not_prove_endpoint_requiredness_or_port(addition, source_bundle):
    bundle = source_bundle(CASES["db-library-without-client"])
    baseline = static_analysis(bundle)
    dependency = next(
        item for item in baseline["dependencies"] if item["value"].get("engine") == "postgresql"
    )
    reply = {"kind": "analysis", "result": deepcopy(baseline)}
    proposal = deepcopy(dependency)
    proposal.update(status="suggested", reason="Endpoint guess from unused PostgreSQL import")
    proposal["value"].update(addition)
    reply["result"]["dependencies"].append(proposal)
    merged = validate_analysis(reply, bundle)
    assert merged["dependencies"] == baseline["dependencies"]


@pytest.mark.parametrize("identity", ["sourceSnapshotId", "contextHash"])
def test_context_replay_is_rejected_at_public_admission(identity, source_bundle):
    case = CASES["composed-diagnostics-route"]
    bundle = source_bundle(case)
    reply = route_reply(bundle, case)
    reply["result"][identity] = "0" * 64
    with pytest.raises(AnalyzerError) as caught:
        validate_analysis(reply, bundle)
    assert caught.value.code == "RESULT_CONTEXT_MISMATCH"


def test_changed_checkout_cannot_change_a_claim_about_the_captured_snapshot(source_bundle, tmp_path):
    case = CASES["composed-diagnostics-route"]
    bundle = source_bundle(case)
    reply = route_reply(bundle, case)
    (tmp_path / "gateway.cjs").write_text("throw new Error('mutable checkout must not be read');\n")
    merged = validate_analysis(reply, bundle)
    assert matching_route(merged, "/ops/alive")


def test_unknown_evidence_id_is_hard_rejected_before_semantic_admission(source_bundle):
    case = CASES["composed-diagnostics-route"]
    bundle = source_bundle(case)
    reply = route_reply(bundle, case, evidence_ids=["unissued-proof"])
    with pytest.raises(AnalyzerError) as caught:
        validate_analysis(reply, bundle)
    assert caught.value.code == "RESULT_EVIDENCE_INVALID"


def test_stale_documentation_advisory_preserves_valid_runtime(source_bundle):
    bundle = source_bundle(CASES["documentation-advisory"])
    baseline = static_analysis(bundle)
    assert baseline["status"] == "complete"
    reply = {
        "kind": "analysis",
        "result": deepcopy(baseline),
        "reviewFindings": [
            {
                "category": "documentation_mismatch",
                "reason": "README's retired command/port differ from the final Docker CMD and reachable listener",
                "evidenceIds": evidence_for(bundle, "README.md")
                + evidence_for(bundle, "Dockerfile")
                + evidence_for(bundle, "gateway.cjs"),
            }
        ],
    }
    _, report = verify(reply, bundle)
    accepted = [item for item in report["reviewFindings"] if item["decision"] == "supported"]
    assert accepted and all(item["blocking"] is False for item in accepted)
    merged = validate_analysis(reply, bundle)
    assert merged["status"] == "complete"
    assert merged["services"] == baseline["services"]


def test_claimed_documentation_conflict_requires_actual_opposing_content(source_bundle):
    case = deepcopy(CASES["documentation-advisory"])
    case["files"]["README.md"] = (
        "# Deployment\nProduction uses `node gateway.cjs` and listens on port 4637.\n"
    )
    bundle = source_bundle(case)
    reply = {
        "kind": "analysis",
        "result": static_analysis(bundle),
        "reviewFindings": [
            {
                "category": "documentation_mismatch",
                "reason": "This is definitely stale; trust the model",
                "evidenceIds": evidence_for(bundle, "README.md") + evidence_for(bundle, "Dockerfile"),
            }
        ],
    }
    _, report = verify(reply, bundle)
    assert not any(item["decision"] == "supported" for item in report["reviewFindings"])
    assert validate_analysis(reply, bundle)["status"] == "complete"


def test_right_source_file_wrong_line_does_not_support_route(source_bundle):
    case = CASES["composed-diagnostics-route"]
    bundle = source_bundle(case)
    listener_ids = [
        item["evidenceId"]
        for item in bundle["evidence"]
        if item["path"] == "gateway.cjs" and item["startLine"] == item["endLine"] == 5
    ]
    assert listener_ids
    reply = route_reply(bundle, case, evidence_ids=listener_ids)
    sanitized, report = verify(reply, bundle)
    assert not matching_route(sanitized["result"], case["route"])
    assert all(
        item["decision"] != "supported" for item in report["decisions"] if "apiRoutes" in item["fieldPath"]
    )
    assert not matching_route(validate_analysis(reply, bundle), case["route"])


def test_detached_partial_bundle_cannot_supply_uncaptured_binding_proof(source_bundle):
    case = CASES["composed-diagnostics-route"]
    bundle = source_bundle(case)
    reply = route_reply(bundle, case)
    # Initial snippets omit the literal expression and factory declaration. Once
    # the immutable registry is gone they cannot be reconstructed from guesses.
    assert not any("'/ops/'" in item["text"] for item in bundle["evidence"])
    release_snapshot(bundle["source"]["snapshotId"])
    sanitized, report = verify(reply, bundle)
    assert not matching_route(sanitized["result"], case["route"])
    assert any(item["decision"] == "deferred" for item in report["decisions"])
    assert not matching_route(validate_analysis(reply, bundle), case["route"])


def test_context_rehashed_with_changed_source_digest_cannot_override_captured_bytes(source_bundle):
    case = CASES["composed-diagnostics-route"]
    bundle = deepcopy(source_bundle(case))
    for manifest in bundle["manifest"]:
        if manifest["path"] == "gateway.cjs":
            manifest["digest"] = "1" * 64
    for evidence in bundle["evidence"]:
        if evidence["path"] == "gateway.cjs":
            evidence["sourceDigest"] = "1" * 64
    bundle["contextHash"] = digest({key: value for key, value in bundle.items() if key != "contextHash"})
    reply = route_reply(bundle, case)
    with pytest.raises(AnalyzerError) as caught:
        validate_analysis(reply, bundle)
    assert caught.value.code == "VERIFICATION_SOURCE_MISMATCH"


def no_op_gold(case_id):
    return {
        "input": {"caseId": case_id, "selectedContext": {}},
        "allowedConclusions": [],
        "expectedResult": {
            "disposition": "no_change",
            "expectedNewClaims": [],
            "expectedNewIssues": [],
            "expectedAbstentions": [],
            "expectedFileRequests": [],
        },
    }


def test_rephrased_static_observation_is_not_credited_as_model_novelty(source_bundle):
    from iris_analyzer.judgment_evaluation import score_judgment_run

    bundle = source_bundle(CASES["documentation-advisory"])
    baseline = static_analysis(bundle)
    reply = {"kind": "analysis", "result": deepcopy(baseline)}
    for service in reply["result"]["services"]:
        for port in service["ports"]:
            port["status"] = "suggested"
            port["reason"] = "I independently discovered this number with different wording"
    _, verification = verify(reply, bundle)
    merged = validate_analysis(reply, bundle)
    score = score_judgment_run(
        no_op_gold("rephrased-known-port"),
        baseline,
        [reply],
        merged,
        bundles=[bundle],
        verification=verification,
    )
    assert score["claims"]["rawNewCount"] == 0
    assert score["claims"]["mergedNewCount"] == 0
    assert score["claims"]["truePositive"] == 0
    assert score["claims"]["precision"] is None


def test_verifier_only_runtime_audit_does_not_become_ai_contribution(source_bundle):
    from iris_analyzer.judgment_evaluation import score_judgment_run

    case = {
        "files": {
            "package.json": json.dumps(
                {"scripts": {"build": "vite build"}, "devDependencies": {"vite": "6.0.0"}}
            ),
            "main.js": "document.body.textContent='generated';\n",
            "Dockerfile": "FROM node:22 AS compile\nWORKDIR /build\nCOPY . .\nRUN npm run build\nFROM nginx:1.27\nCOPY --from=compile /build/dist /usr/share/nginx/html\nEXPOSE 80\n",
        }
    }
    bundle = source_bundle(case)
    # Fault injection recreates an incomplete extractor without retaining the
    # now-fixed missing nginx runtime declaration in production preprocessing.
    bundle["facts"] = [
        f for f in bundle["facts"] if not (f["key"] == "runtime.name" and f["scope"] == "container")
    ]
    bundle["contextHash"] = digest({k: v for k, v in bundle.items() if k != "contextHash"})
    baseline = static_analysis(bundle)
    reply = {"kind": "analysis", "result": deepcopy(baseline), "reviewFindings": []}
    _, verification = verify(reply, bundle)
    merged = validate_analysis(reply, bundle)
    score = score_judgment_run(
        no_op_gold("final-image-independent-audit"),
        baseline,
        [reply],
        merged,
        bundles=[bundle],
        verification=verification,
    )
    assert score["issues"]["modelTruePositive"] == 0
    assert score["issues"]["verifierOnlyCount"] >= 1
    assert any(item["origin"] == "verifier" for item in verification["reviewFindings"])


def test_ungrounded_legacy_question_does_not_make_valid_baseline_blocking(source_bundle):
    bundle = source_bundle(CASES["documentation-advisory"])
    baseline = static_analysis(bundle)
    assert baseline["status"] == "complete"
    reply = {"kind": "analysis", "result": deepcopy(baseline)}
    reply["result"]["questions"].append(
        {
            "key": "unproven_database_requirement",
            "reason": "The model thinks a database is probably required, without code evidence",
            "kind": "code_review",
        }
    )
    merged = validate_analysis(reply, bundle)
    assert merged["status"] == baseline["status"]
    assert not any(item["key"] == "unproven_database_requirement" for item in merged["questions"])


@pytest.mark.parametrize(
    "readme",
    [
        "# Developer guide\nDevelopment uses `node dev.cjs` and listens on port 5842.\n",
        "# Project overview\n"
        + "General project documentation.\n" * 15
        + "Production uses `node retired.cjs` and listens on port 7123.\n",
    ],
)
def test_documentation_comparison_does_not_invent_same_scope_or_uncited_conflict(readme, source_bundle):
    case = deepcopy(CASES["documentation-advisory"])
    case["files"]["README.md"] = readme
    bundle = source_bundle(case)
    reply = {
        "kind": "analysis",
        "result": static_analysis(bundle),
        "reviewFindings": [
            {
                "category": "documentation_mismatch",
                "reason": "The documentation is supposedly stale",
                "evidenceIds": evidence_for(bundle, "README.md") + evidence_for(bundle, "Dockerfile"),
            }
        ],
    }
    _, report = verify(reply, bundle)
    assert not any(
        item["decision"] == "supported" and item["category"] == "documentation_mismatch"
        for item in report["reviewFindings"]
    )
    assert validate_analysis(reply, bundle)["status"] == "complete"


def test_receiver_declaration_alone_does_not_prove_listener_port(source_bundle):
    case = CASES["composed-diagnostics-route"]
    bundle = source_bundle(case)
    reply = {"kind": "analysis", "result": static_analysis(bundle)}
    receiver_ids = [
        item["evidenceId"]
        for item in bundle["evidence"]
        if item["path"] == "gateway.cjs" and item["startLine"] == item["endLine"] == 2
    ]
    assert receiver_ids
    port = next(item for item in reply["result"]["services"][0]["ports"] if item["value"] == 4637)
    port.update(status="suggested", evidenceIds=receiver_ids)
    _, report = verify(reply, bundle)
    decisions = [item for item in report["decisions"] if "ports" in item["fieldPath"]]
    assert decisions and all(item["decision"] != "supported" for item in decisions)


def test_shadowed_listener_cannot_be_reverified_by_repeating_the_same_static_extractor(source_bundle):
    case = deepcopy(CASES["composed-diagnostics-route"])
    case["files"]["gateway.cjs"] = (
        "const createWeb = require('express');\n"
        "const gateway = createWeb();\n"
        "function boot(gateway) { gateway.listen(4637); }\n"
        "boot({ listen: () => null });\n"
    )
    bundle = source_bundle(case)
    baseline = static_analysis(bundle)
    reply = {"kind": "analysis", "result": deepcopy(baseline)}
    observed = next(item for item in reply["result"]["services"][0]["ports"] if item["value"] == 4637)
    observed["status"] = "suggested"
    _, report = verify(reply, bundle)
    decisions = [item for item in report["decisions"] if "ports" in item["fieldPath"]]
    assert decisions and all(item["decision"] != "supported" for item in decisions)
    assert any(item["origin"] == "verifier" and item["blocking"] for item in report["reviewFindings"])
    merged = validate_analysis(reply, bundle)
    assert merged["status"] == "needs_input"
    assert merged["services"][0]["ports"] == baseline["services"][0]["ports"]


@pytest.mark.filterwarnings("ignore:invalid escape sequence:SyntaxWarning")
@pytest.mark.parametrize(
    "expression,proposed_path",
    [
        (r"'/op\s/' + 'alive'", r"/op\s/alive"),
        (r"`/op\s/` + 'alive'", r"/op\s/alive"),
        (r"'/ops/\x61/' + 'alive'", "/ops/a/alive"),
        (r"'/ops/\u0061/' + 'alive'", "/ops/a/alive"),
        (r"'/ops/\141/' + 'alive'", "/ops/a/alive"),
        ("`/ops/\r\nalive/` + 'ready'", "/ops/\r\nalive/ready"),
    ],
)
def test_ambiguous_or_unsupported_javascript_escapes_are_not_python_literal_proof(
    expression, proposed_path, source_bundle
):
    case = deepcopy(CASES["composed-diagnostics-route"])
    case["route"] = proposed_path
    case["files"]["gateway.cjs"] = (
        "const createWeb = require('express');\n"
        "const gateway = createWeb();\n"
        f"const diagnostic = {expression};\n"
        "gateway.get(diagnostic, (_req, res) => res.send('alive'));\n"
        "gateway.listen(4637);\n"
    )
    bundle = source_bundle(case)
    reply = route_reply(bundle, case)
    sanitized, report = verify(reply, bundle)
    decisions = [item for item in report["decisions"] if "apiRoutes" in item["fieldPath"]]
    assert decisions and all(item["decision"] == "deferred" for item in decisions)
    assert not matching_route(sanitized["result"], proposed_path)
    assert not matching_route(validate_analysis(reply, bundle), proposed_path)


@pytest.mark.filterwarnings("ignore:invalid escape sequence:SyntaxWarning")
@pytest.mark.parametrize(
    "expression,actual_path",
    [
        (r"'/ops/quo\'te/' + 'alive'", "/ops/quo'te/alive"),
        (r"'/ops/quo\"te/' + 'alive'", '/ops/quo"te/alive'),
        (r"'/ops/back\\slash/' + 'alive'", r"/ops/back\slash/alive"),
        (r"'/ops\/' + 'alive'", "/ops/alive"),
        (r"'/ops/line\n/' + 'alive'", "/ops/line\n/alive"),
        (r"`/ops/tab\t/` + 'alive'", "/ops/tab\t/alive"),
    ],
)
def test_supported_javascript_escapes_decode_to_actual_declared_value(expression, actual_path, source_bundle):
    case = deepcopy(CASES["composed-diagnostics-route"])
    case["route"] = actual_path
    case["files"]["gateway.cjs"] = (
        "const createWeb = require('express');\n"
        "const gateway = createWeb();\n"
        f"const diagnostic = {expression};\n"
        "gateway.get(diagnostic, (_req, res) => res.send('alive'));\n"
        "gateway.listen(4637);\n"
    )
    bundle = source_bundle(case)
    reply = route_reply(bundle, case)
    sanitized, report = verify(reply, bundle)
    decisions = [item for item in report["decisions"] if "apiRoutes" in item["fieldPath"]]
    assert decisions and all(item["decision"] == "supported" for item in decisions)
    assert matching_route(sanitized["result"], actual_path)
    assert matching_route(validate_analysis(reply, bundle), actual_path)
