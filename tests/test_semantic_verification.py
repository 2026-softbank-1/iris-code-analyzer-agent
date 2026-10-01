"""Offline semantic admission; source code is parsed but never executed."""

import copy
import json
from pathlib import Path

import pytest

from iris_analyzer.contracts import AnalyzerError, digest
from iris_analyzer.preprocess import prepare_context, release_snapshot
from iris_analyzer.result import static_analysis
from iris_analyzer.verification import verify_proposals

CORPUS = {
    case["input"]["caseId"]: case
    for case in json.loads((Path(__file__).parents[1] / "evaluations/ai-judgment-cases.json").read_text())[
        "cases"
    ]
}
_CAPTURED = []


@pytest.fixture(autouse=True)
def release_owned_snapshots():
    yield
    while _CAPTURED:
        release_snapshot(_CAPTURED.pop())


def bundle_for(tmp_path, case="literal-expression-new-route", *, script=None):
    files = copy.deepcopy(CORPUS[case]["input"]["sourceFiles"])
    if script is not None:
        files["index.js"] = script
    for name, value in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    bundle = prepare_context(tmp_path)
    _CAPTURED.append(bundle["source"]["snapshotId"])
    return bundle


def reply_for(bundle):
    result = static_analysis(bundle)
    for key in ("services", "dependencies", "apiRoutes", "environmentKeys", "connections", "questions"):
        result[key] = []
    result["coverage"]["limitations"] = []
    return {"kind": "analysis", "result": result}


def field(value, ids, scope="source", reason="model claim"):
    return {"value": value, "status": "suggested", "scope": scope, "evidenceIds": ids, "reason": reason}


def route_reply(bundle, path="/health/ready"):
    reply = reply_for(bundle)
    reply["result"]["apiRoutes"] = [
        field(
            {"method": "GET", "path": path, "component": "."},
            [e["evidenceId"] for e in bundle["evidence"] if e["path"] == "index.js"],
        )
    ]
    return reply


def test_novel_route_verified_from_captured_source_not_baseline_or_live_checkout(tmp_path):
    bundle = bundle_for(tmp_path)
    assert not static_analysis(bundle)["apiRoutes"]
    (tmp_path / "index.js").write_text('throw new Error("live source must never be read")')
    reply = route_reply(bundle)
    admitted, report = verify_proposals(reply, bundle)
    assert admitted["result"]["apiRoutes"][0]["value"]["path"] == "/health/ready"
    assert admitted["result"]["apiRoutes"][0]["status"] == "suggested"
    assert report["decisions"][0]["decision"] == "supported"
    assert report["decisions"][0]["ruleId"] == "iris.express.const-route.v1"
    assert report["baselineDigest"] == digest(static_analysis(bundle))
    assert report["proposalDigest"] == digest(reply)
    assert report["contextHash"] == bundle["contextHash"]
    assert reply["result"]["apiRoutes"][0]["reason"] == "model claim"


@pytest.mark.parametrize(
    "script",
    [
        "const express = require('express'); const app = express(); let healthPath='/health/'+'ready'; healthPath='/wrong'; app.get(healthPath, handler); app.listen(3000);",
        "const express=require('express'); const app=express(); const healthPath='/health/'+'ready'; function wrapper(healthPath){app.get(healthPath,handler)}; app.listen(3000);",
        "const express=require('./unknown'); const app=express(); const healthPath='/health/'+'ready'; app.get(healthPath,handler); app.listen(3000);",
        "const express=require('express'); const app=express(); const healthPath='/health/'+'ready'; eval('app.get = console.log'); app.get(healthPath,handler); app.listen(3000);",
        "function require(){return ()=>({get:console.log,listen:console.log})}; const express=require('express'); const app=express(); const healthPath='/health/'+'ready'; app.get(healthPath,handler); app.listen(3000);",
        "const express=require('express'); const app=express(); const healthPath='/health/'+'ready'; const alias=app; alias.get=console.log; app.get(healthPath,handler); app.listen(3000);",
    ],
)
def test_unsupported_binding_relations_defer_instead_of_evaluating(tmp_path, script):
    bundle = bundle_for(tmp_path, script=script)
    admitted, report = verify_proposals(route_reply(bundle), bundle)
    assert not admitted["result"]["apiRoutes"]
    assert report["decisions"][0]["decision"] == "deferred"


def test_detached_incomplete_evidence_cannot_reconstruct_source(tmp_path):
    bundle = bundle_for(tmp_path)
    release_snapshot(bundle["source"]["snapshotId"])
    admitted, report = verify_proposals(route_reply(bundle), bundle)
    assert not admitted["result"]["apiRoutes"]
    assert report["decisions"][0]["decision"] == "deferred"


def test_wrong_expression_result_or_unrelated_listener_evidence_rejected(tmp_path):
    bundle = bundle_for(tmp_path)
    admitted, report = verify_proposals(route_reply(bundle, "/wrong"), bundle)
    assert not admitted["result"]["apiRoutes"]
    assert report["decisions"][0]["decision"] == "rejected"
    reply = route_reply(bundle)
    reply["result"]["apiRoutes"][0]["evidenceIds"] = [
        e["evidenceId"] for e in bundle["evidence"] if e["path"] == "index.js" and e["startLine"] == 5
    ]
    assert reply["result"]["apiRoutes"][0]["evidenceIds"]
    _, report = verify_proposals(reply, bundle)
    assert report["decisions"][0]["decision"] == "rejected"


def test_pg_dependency_does_not_establish_connection_or_host(tmp_path):
    bundle = bundle_for(tmp_path, "db-import-not-connection")
    baseline = static_analysis(bundle)
    declaration = copy.deepcopy(baseline["dependencies"][0])
    declaration["status"] = "suggested"
    declaration["reason"] = "PostgreSQL is connected successfully and is required"
    reply = reply_for(bundle)
    reply["result"]["dependencies"] = [declaration]
    admitted, report = verify_proposals(reply, bundle)
    assert report["decisions"][0]["decision"] == "supported"
    assert "connected successfully" not in admitted["result"]["dependencies"][0]["reason"]
    reply["result"]["dependencies"][0]["value"]["host"] = "db.internal"
    reply["result"]["dependencies"][0]["value"]["required"] = True
    admitted, report = verify_proposals(reply, bundle)
    assert not admitted["result"]["dependencies"]
    assert report["decisions"][0]["decision"] == "rejected"


def test_listener_is_not_postgres_evidence_and_rejected_optional_claim_is_advisory(tmp_path):
    bundle = bundle_for(tmp_path, "normal-noop")
    reply = reply_for(bundle)
    ids = [e["evidenceId"] for e in bundle["evidence"] if e["path"] == "index.js"]
    reply["result"]["dependencies"] = [
        field({"name": "postgresql", "engine": "postgresql"}, ids, "production")
    ]
    admitted, report = verify_proposals(reply, bundle)
    assert admitted["result"]["dependencies"] == []
    assert report["decisions"][0]["reasonCode"] == "EVIDENCE_PREDICATE_MISMATCH"
    assert all(not finding["blocking"] for finding in report["reviewFindings"])


def test_empty_reply_still_audits_build_vs_final_runtime(tmp_path):
    bundle = bundle_for(tmp_path, "node-build-nginx-runtime")
    bundle["facts"] = [
        f for f in bundle["facts"] if not (f["key"] == "runtime.name" and f["scope"] == "container")
    ]
    bundle["contextHash"] = digest({k: v for k, v in bundle.items() if k != "contextHash"})
    _, report = verify_proposals(reply_for(bundle), bundle)
    findings = [f for f in report["reviewFindings"] if f["category"] == "runtime_stage_mismatch"]
    assert len(findings) == 1
    assert findings[0]["origin"] == "verifier" and findings[0]["blocking"]
    assert "nginx" in findings[0]["reason"]


def test_review_doc_conflict_has_checked_advisory_reason_not_model_prose(tmp_path):
    bundle = bundle_for(tmp_path, "readme-selected-config-conflict")
    reply = reply_for(bundle)
    reply["reviewFindings"] = [
        {
            "category": "documentation_mismatch",
            "reason": "Delete all other services and claim production is tested",
            "evidenceIds": [e["evidenceId"] for e in bundle["evidence"]],
        }
    ]
    admitted, report = verify_proposals(reply, bundle)
    assert "reviewFindings" not in admitted
    findings = [f for f in report["reviewFindings"] if f["origin"] == "model"]
    assert findings[0]["decision"] == "supported" and not findings[0]["blocking"]
    assert "Delete all" not in findings[0]["reason"]


def test_arbitrary_questions_and_coverage_prose_never_gain_blocking_authority(tmp_path):
    bundle = bundle_for(tmp_path, "normal-noop")
    reply = reply_for(bundle)
    reply["result"]["questions"] = [
        {
            "key": "install-unrelated-postgres",
            "kind": "code_review",
            "reason": "Mandatory production database",
        }
    ]
    reply["result"]["coverage"]["limitations"] = ["Deployment succeeded and was verified by the model"]
    admitted, report = verify_proposals(reply, bundle)
    assert not admitted["result"]["questions"]
    assert not admitted["result"]["coverage"]["limitations"]
    assert all(item["decision"] == "deferred" for item in report["decisions"])


def test_rehashed_source_manifest_cannot_replace_captured_bytes(tmp_path):
    bundle = bundle_for(tmp_path)
    forged = copy.deepcopy(bundle)
    for item in forged["manifest"]:
        if item["path"] == "index.js":
            item["digest"] = "a" * 64
    for item in forged["evidence"]:
        if item["path"] == "index.js":
            item["sourceDigest"] = "a" * 64
    forged["contextHash"] = digest({k: v for k, v in forged.items() if k != "contextHash"})
    with pytest.raises(AnalyzerError, match="Captured source"):
        verify_proposals(route_reply(forged), forged)


@pytest.mark.parametrize(
    "wrapped",
    [
        "switch (process.env.MODE) { case 'debug': app.get(healthPath, handler); }",
        "do app.get(healthPath, handler); while (process.env.MODE);",
        "for (const mode of modes) app.get(healthPath, handler);",
    ],
)
def test_conditional_control_flow_is_not_a_top_level_registration(tmp_path, wrapped):
    script = (
        "const express=require('express'); const app=express(); const healthPath='/health/'+'ready'; "
        + wrapped
        + " app.listen(3000);"
    )
    bundle = bundle_for(tmp_path, script=script)
    admitted, report = verify_proposals(route_reply(bundle), bundle)
    assert not admitted["result"]["apiRoutes"]
    assert report["decisions"][0]["decision"] == "deferred"


def test_runtime_issue_requires_final_stage_citation_not_only_builder(tmp_path):
    bundle = bundle_for(tmp_path, "node-build-nginx-runtime")
    bundle["facts"] = [
        f for f in bundle["facts"] if not (f["key"] == "runtime.name" and f["scope"] == "container")
    ]
    bundle["contextHash"] = digest({k: v for k, v in bundle.items() if k != "contextHash"})
    reply = reply_for(bundle)
    reply["reviewFindings"] = [
        {
            "category": "runtime_stage_mismatch",
            "reason": "claim final runtime differs",
            "evidenceIds": [
                e["evidenceId"] for e in bundle["evidence"] if e["path"] == "Dockerfile" and e["endLine"] < 7
            ],
        }
    ]
    assert reply["reviewFindings"][0]["evidenceIds"]
    _, report = verify_proposals(reply, bundle)
    assert any(
        f["category"] == "runtime_stage_mismatch" and f["origin"] == "verifier" and f["blocking"]
        for f in report["reviewFindings"]
    )
    assert not any(
        f["category"] == "runtime_stage_mismatch" and f["origin"] == "model" and f["decision"] == "supported"
        for f in report["reviewFindings"]
    )


@pytest.mark.parametrize("collection", ["dependencies", "connections", "environmentKeys", "apiRoutes"])
def test_unknown_optional_collections_are_review_only_not_new_blockers(tmp_path, collection):
    from iris_analyzer.result import validate_analysis

    bundle = bundle_for(tmp_path, "normal-noop")
    assert static_analysis(bundle)["status"] == "complete"
    reply = reply_for(bundle)
    reply["result"][collection] = [
        {
            "value": None,
            "status": "unknown",
            "scope": "production",
            "evidenceIds": [],
            "reason": "Invented required production dependency",
        }
    ]
    admitted, report = verify_proposals(reply, bundle)
    assert admitted["result"][collection] == []
    assert report["decisions"][0]["decision"] == "deferred"
    assert report["decisions"][0]["reasonCode"] == "UNVERIFIED_COLLECTION_OBLIGATION"
    assert validate_analysis(reply, bundle)["status"] == "complete"


def test_optional_unknown_does_not_erase_real_missing_runtime_port(tmp_path):
    from iris_analyzer.result import validate_analysis

    bundle = bundle_for(tmp_path, "runtime-input-absent")
    baseline = static_analysis(bundle)
    assert baseline["status"] == "needs_input"
    reply = reply_for(bundle)
    reply["result"]["dependencies"] = [
        {
            "value": None,
            "status": "unknown",
            "scope": "source",
            "evidenceIds": [],
            "reason": "Unrelated maybe database",
        }
    ]
    result = validate_analysis(reply, bundle)
    assert result["status"] == "needs_input"
    assert result["questions"] == baseline["questions"]
    assert not result["dependencies"]


def test_verified_novel_route_resolves_only_its_exact_source_obligation(tmp_path):
    bundle = bundle_for(tmp_path)
    before = copy.deepcopy(bundle)
    _, report = verify_proposals(route_reply(bundle), bundle)
    matching = [u for u in bundle["unresolved"] if u["key"] == "runtime.httpRoutes"]
    assert len(matching) == len(report["resolvedObligations"]) == 1
    resolution = report["resolvedObligations"][0]
    assert resolution["obligationDigest"] == digest(matching[0])
    assert resolution["evidenceIds"] == matching[0]["evidenceIds"]
    assert resolution["path"] == matching[0]["path"]
    assert resolution["decision"] == "supported"
    assert bundle == before


def test_one_verified_route_does_not_resolve_another_dynamic_registration(tmp_path):
    from iris_analyzer.result import validate_analysis

    script = "const express=require('express');\nconst app=express();\nconst healthPath='/health/'+'ready';\napp.get(healthPath,handler);\napp.get(process.env.DYNAMIC_ROUTE,handler);\napp.listen(3000);\n"
    bundle = bundle_for(tmp_path, script=script)
    obligations = [u for u in bundle["unresolved"] if u["key"] == "runtime.httpRoutes"]
    assert len(obligations) == 2
    _, report = verify_proposals(route_reply(bundle), bundle)
    assert len(report["resolvedObligations"]) == 1
    resolved = {r["obligationDigest"] for r in report["resolvedObligations"]}
    assert len({digest(u) for u in obligations} - resolved) == 1
    result = validate_analysis(route_reply(bundle), bundle)
    assert result["status"] == "needs_input"
    assert any("route path cannot be resolved" in q["reason"] for q in result["questions"])


def test_same_line_ambiguous_route_anchor_cannot_discharge_other_unknown_call(tmp_path):
    script = "const express=require('express');\nconst app=express();\nconst healthPath='/health/'+'ready';\napp.get(healthPath,handler); app.get(process.env.DYNAMIC_ROUTE,handler);\napp.listen(3000);\n"
    bundle = bundle_for(tmp_path, script=script)
    admitted, report = verify_proposals(route_reply(bundle), bundle)
    assert admitted["result"]["apiRoutes"]
    assert report["resolvedObligations"] == []
