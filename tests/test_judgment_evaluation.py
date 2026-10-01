import copy
import json
from pathlib import Path

import pytest

from iris_analyzer.contracts import AnalyzerError, digest
from iris_analyzer.judgment_evaluation import atomic_claims, evaluate_judgment_cases, score_judgment_run
from iris_analyzer.opencode.runner import response_template
from iris_analyzer.preprocess import compact_model_input, expand_context, prepare_context, release_snapshot
from iris_analyzer.result import static_analysis

CORPUS_PATH = Path(__file__).resolve().parents[1] / "evaluations/ai-judgment-cases.json"
CASES = {case["input"]["caseId"]: case for case in json.loads(CORPUS_PATH.read_text())["cases"]}


def get_fixture(tmp_path, case_id):
    case = CASES[case_id]
    for name, text in case["input"]["sourceFiles"].items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    bundle = prepare_context(tmp_path)
    return case, bundle, static_analysis(bundle)


def test_static_facts_repeated_with_new_reason_and_status_do_not_earn_ai_credit(tmp_path):
    case, bundle, baseline = get_fixture(tmp_path, "normal-noop")
    try:
        reply = response_template(bundle)
        reply["result"] = copy.deepcopy(baseline)
        for service in reply["result"]["services"]:
            for field in service["ports"]:
                field.update(status="suggested", reason="A model explanation", evidenceIds=[])
        score = score_judgment_run(case, baseline, [reply], baseline)
        assert score["claims"]["truePositive"] == score["claims"]["rawNewCount"] == 0
        assert score["claims"]["duplicateStaticCount"] > 0
        assert score["claims"]["precision"] is None
        assert score["claims"]["precisionLowerBound"] is None
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_positive_literal_route_is_new_and_merged_contribution(tmp_path):
    case, bundle, baseline = get_fixture(tmp_path, "literal-expression-new-route")
    try:
        field = copy.deepcopy(case["expectedResult"]["expectedNewClaims"][0])
        reply = response_template(bundle)
        reply["result"]["apiRoutes"] = [field]
        merged = copy.deepcopy(baseline)
        merged["apiRoutes"].append(field)
        verification = {"decisions": [{"fieldPath": ["result", "apiRoutes", 0], "decision": "supported"}]}
        request = {
            "kind": "needs_files",
            "requestedPaths": ["index.js"],
            "reason": "Read the omitted declaration",
        }
        expanded = expand_context(bundle, ["index.js"])
        score = score_judgment_run(
            case, baseline, [request, reply], merged, bundles=[bundle, expanded], verification=verification
        )
        assert score["claims"]["truePositive"] == score["claims"]["admittedTruePositive"] == 1
        assert score["claims"]["precision"] == score["claims"]["recall"] == 1
        assert score["gatePassed"] is True
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_rejected_bad_evidence_is_raw_error_even_when_cleaned_result_has_none(tmp_path):
    case, bundle, baseline = get_fixture(tmp_path, "valid-id-unrelated-evidence")
    try:
        reply = response_template(bundle)
        reply["result"]["dependencies"] = [
            {
                "value": {"name": "postgresql", "engine": "postgresql", "host": "db.internal", "port": 5432},
                "status": "suggested",
                "scope": "production",
                "evidenceIds": [],
                "reason": "incorrect support",
            }
        ]
        verification = {
            "decisions": [
                {
                    "fieldPath": ["result", "dependencies", 0],
                    "decision": "rejected",
                    "reasonCode": "EVIDENCE_PREDICATE_MISMATCH",
                }
            ]
        }
        score = score_judgment_run(case, baseline, [reply], baseline, verification=verification)
        assert score["claims"]["unsupportedProposalCount"] == 1
        assert score["claims"]["escapedUnsupportedCount"] == 0
        assert score["claims"]["truePositive"] == 0
        assert score["claims"]["unreviewedCount"] == 1
        assert score["claims"]["precision"] is None
        assert score["claims"]["precisionLowerBound"] == 0
        merged = copy.deepcopy(baseline)
        merged["dependencies"] += reply["result"]["dependencies"]
        escaped = score_judgment_run(case, baseline, [reply], merged, verification=verification)
        assert escaped["claims"]["escapedUnsupportedCount"] == 1
        assert escaped["gatePassed"] is False
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_exact_reviewed_negative_atom_counts_raw_and_escaped_false_positive(tmp_path):
    case, bundle, baseline = get_fixture(tmp_path, "normal-noop")
    try:
        case = copy.deepcopy(case)
        forbidden = {
            "target": "apiRoutes",
            "status": "suggested",
            "scope": "source",
            "value": {
                "method": "GET",
                "path": "/invented",
                "component": ".",
            },
        }
        case["expectedResult"]["forbiddenAtomicClaims"] = [forbidden]
        reply = response_template(bundle)
        reply["result"]["apiRoutes"] = [forbidden]
        score = score_judgment_run(case, baseline, [reply], baseline)
        assert score["claims"]["falsePositive"] == 1
        assert score["claims"]["escapedFalsePositiveCount"] == 0
        assert score["claims"]["precision"] == 0
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_verifier_added_runtime_issue_is_not_ai_contribution(tmp_path):
    case, bundle, baseline = get_fixture(tmp_path, "node-build-nginx-runtime")
    try:
        verification = {
            "reviewFindings": [
                {
                    "category": "runtime_stage_mismatch",
                    "origin": "verifier",
                    "decision": "supported",
                    "evidenceIds": [e["evidenceId"] for e in bundle["evidence"]],
                }
            ]
        }
        score = score_judgment_run(
            case, baseline, [response_template(bundle)], baseline, verification=verification
        )
        assert score["issues"]["verifierOnlyCount"] == 1
        assert score["issues"]["modelTruePositive"] == 0
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_issue_category_and_complete_support_are_needed_for_ai_credit(tmp_path):
    case, bundle, baseline = get_fixture(tmp_path, "readme-selected-config-conflict")
    try:
        ids = [e["evidenceId"] for e in bundle["evidence"]]
        finding = {
            "category": "documentation_mismatch",
            "reason": "Review selected deployment docs",
            "evidenceIds": ids,
        }
        reply = response_template(bundle)
        reply["reviewFindings"] = [finding]
        verification = {
            "reviewFindings": [{**finding, "origin": "model", "decision": "supported", "blocking": False}]
        }
        score = score_judgment_run(
            case, baseline, [reply], baseline, bundles=[bundle], verification=verification
        )
        assert score["issues"]["modelTruePositive"] == 1
        reply["reviewFindings"][0]["evidenceIds"] = [
            e["evidenceId"] for e in bundle["evidence"] if e["path"] == "README.md"
        ]
        incomplete = score_judgment_run(
            case, baseline, [reply], baseline, bundles=[bundle], verification=verification
        )
        assert incomplete["issues"]["modelTruePositive"] == 0
        assert incomplete["issues"]["unreviewedCount"] == 1
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_initial_file_request_cannot_get_credit_for_later_seen_port(tmp_path):
    case, bundle, baseline = get_fixture(tmp_path, "available-config-needs-files")
    try:
        initial = {
            "kind": "needs_files",
            "requestedPaths": ["release-settings.json"],
            "reason": "Resolve literal settings binding",
        }
        later = response_template(bundle)
        later["result"]["services"] = copy.deepcopy(baseline["services"])
        later["result"]["services"][0]["ports"] = [
            {"value": 3000, "status": "suggested", "scope": "container"}
        ]
        score = score_judgment_run(case, baseline, [initial, later], baseline, bundles=[bundle, bundle])
        assert score["claims"]["rawNewCount"] == 0
        assert score["fileRequests"]["appropriate"] == ["release-settings.json"]
        assert score["fileRequests"]["initialContentWithheld"] is True
        assert score["scoredPhase"] == "initial_reply"
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_unknown_natural_language_cannot_earn_abstention_credit(tmp_path):
    case, bundle, baseline = get_fixture(tmp_path, "db-import-not-connection")
    try:
        reply = response_template(bundle)
        reply["result"]["questions"] = [
            {"key": "database_connection", "kind": "code_review", "reason": "Unreviewed rationale"}
        ]
        score = score_judgment_run(case, baseline, [reply], baseline)
        assert score["abstentions"]["identityMatches"] == 1
        assert score["abstentions"]["rationaleAutomaticallyVerified"] is False
        assert any(x["kind"] == "abstention_rationale" for x in score["humanReview"])
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_full_static_corpus_is_fixture_verification_not_model_performance(tmp_path):
    report = evaluate_judgment_cases(CORPUS_PATH, out=tmp_path / "offline")
    assert report["summary"]["caseCount"] == 11
    assert report["summary"]["baselinePassed"] is True
    assert report["summary"]["runCount"] == 0
    assert report["summary"]["measuredGatesPassed"] is None
    assert report["modelEvaluationPerformed"] is False
    negative = next(c for c in report["cases"] if c["caseId"] == "valid-id-unrelated-evidence")
    assert negative["excludedFromModelMetrics"] is True


class EmptyRunner:
    calls = []

    def invoke_model(self, bundle):
        return response_template(bundle)


def test_fake_pipeline_run_retains_raw_reply_baseline_and_sidecars(tmp_path):
    report = evaluate_judgment_cases(
        CORPUS_PATH, out=tmp_path / "run", runner=EmptyRunner(), case_ids=["normal-noop"]
    )
    case_dir = tmp_path / "run/normal-noop"
    raw = json.loads((case_dir / "run-01/captured-raw-replies.json").read_text())
    assert len(raw) == 1 and raw[0]["kind"] == "analysis"
    assert (case_dir / "baseline-bundle.json").is_file()
    assert (case_dir / "run-01/run-report.json").is_file()
    assert report["summary"]["successfulRuns"] == 1
    assert report["summary"]["rawNewClaimTruePositive"] == 0


def test_shared_budget_failure_stops_further_calls_and_retains_failure(tmp_path):
    class ExhaustedRunner(EmptyRunner):
        count = 0

        def invoke_model(self, bundle):
            self.count += 1
            raise AnalyzerError("MODEL_BUDGET_EXCEEDED", "budget exhausted")

    runner = ExhaustedRunner()
    report = evaluate_judgment_cases(
        CORPUS_PATH,
        out=tmp_path / "run",
        runner=runner,
        repetitions=2,
        case_ids=["normal-noop", "literal-expression-new-route"],
    )
    assert runner.count == 1
    assert report["summary"]["failedRuns"] == 1
    assert report["summary"]["skippedRuns"] == 3
    assert report["summary"]["successfulRuns"] == 0


@pytest.mark.parametrize("repetitions", [0, -1, 11, True])
def test_unbounded_repetition_rejected_before_model_call(tmp_path, repetitions):
    with pytest.raises(ValueError):
        evaluate_judgment_cases(CORPUS_PATH, out=tmp_path / "run", repetitions=repetitions)


def test_service_scope_and_owner_are_part_of_atomic_identity():
    field = {"status": "detected", "scope": "container", "value": 3000}
    a = {"services": [{"root": {"value": "app"}, "ports": [field]}]}
    b = copy.deepcopy(a)
    b["services"][0]["ports"][0]["scope"] = "host_mapping"
    c = copy.deepcopy(a)
    c["services"][0]["root"]["value"] = "other"
    assert set(atomic_claims(a)) != set(atomic_claims(b)) != set(atomic_claims(c))
    assert len(digest(atomic_claims(a))) == 64


def test_correct_value_with_rejected_support_is_not_ai_discovery_credit(tmp_path):
    case, bundle, baseline = get_fixture(tmp_path, "literal-expression-new-route")
    try:
        reply = response_template(bundle)
        field = copy.deepcopy(case["expectedResult"]["expectedNewClaims"][0])
        reply["result"]["apiRoutes"] = [field]
        verification = {"decisions": [{"fieldPath": ["result", "apiRoutes", 0], "decision": "rejected"}]}
        score = score_judgment_run(case, baseline, [reply], baseline, verification=verification)
        assert score["claims"]["rawGoldMatchCount"] == 1
        assert score["claims"]["valueMatchedButUnsupported"] == 1
        assert score["claims"]["truePositive"] == 0
        assert score["gatePassed"] is False
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_cli_uses_hive_defaults_and_explicit_cumulative_budget(tmp_path, monkeypatch, capsys):
    import importlib.util
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location(
        "evaluate_ai_judgment", CORPUS_PATH.parents[1] / "scripts/evaluate_ai_judgment.py"
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    selected = {}

    def config_from_env(path, **options):
        selected.update(options)
        return SimpleNamespace(
            server_url="http://127.0.0.1:1", provider="hive-ai", model="zai-org/glm-5.3-flash"
        )

    class NoNetworkRunner:
        def __init__(self, config, *, prompt_file=None):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def no_network_evaluation(*args, **kwargs):
        assert kwargs["runner"].max_cost_usd == 20
        return {"mode": "test_only", "summary": {"baselinePassed": True, "failedRuns": 0, "skippedRuns": 0}}

    monkeypatch.setattr(cli.ModelConfig, "from_env", config_from_env)
    monkeypatch.setattr(cli, "EvaluationOpenCodeRunner", NoNetworkRunner)
    monkeypatch.setattr(cli, "evaluate_judgment_cases", no_network_evaluation)
    assert cli.main(["--live", "--provider", "hive-ai", "--max-cost-usd", "20", "--out", str(tmp_path)]) == 0
    assert selected["provider"] == "hive-ai"
    assert selected["model"] is None
    assert "output_mode" not in selected
    assert "test_only" in capsys.readouterr().out


def test_existing_port_blocker_rephrased_with_gold_key_has_zero_novelty_credit(tmp_path):
    case, bundle, baseline = get_fixture(tmp_path, "same-scope-listener-expose")
    try:
        reply = response_template(bundle)
        reply["result"]["questions"] = [
            {
                "key": "resolved_container_port",
                "kind": "code_review",
                "reason": "Review port conflict",
            }
        ]
        score = score_judgment_run(case, baseline, [reply], baseline)
        assert score["abstentions"]["identityMatches"] == 1
        assert score["abstentions"]["newIdentityMatches"] == 0
        assert score["abstentions"]["preservedExpectedBlocker"] is True
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_positive_pipeline_sidecar_controls_discovery_credit(tmp_path):
    class RouteRunner(EmptyRunner):
        def invoke_model(self, bundle):
            if bundle["revision"] == 1:
                return {
                    "kind": "needs_files",
                    "requestedPaths": ["index.js"],
                    "reason": "Read the omitted constant declaration before deriving the route.",
                }
            reply = response_template(bundle)
            reply["result"]["apiRoutes"] = [
                {
                    "value": {"method": "GET", "path": "/health/ready", "component": "."},
                    "scope": "source",
                    "status": "suggested",
                    "reason": "Local literal concatenation feeds the Express route registration.",
                    "evidenceIds": [e["evidenceId"] for e in bundle["evidence"] if e["path"] == "index.js"],
                }
            ]
            return reply

    output = tmp_path / "verified"
    report = evaluate_judgment_cases(
        CORPUS_PATH, out=output, runner=RouteRunner(), case_ids=["literal-expression-new-route"]
    )
    assert report["summary"]["rawNewClaimTruePositive"] == 1
    assert report["summary"]["escapedUnsupportedCount"] == 0
    assert (output / "literal-expression-new-route/run-01/verification-report.json").is_file()


def test_partial_selected_file_request_is_appropriate_after_snapshot_release(tmp_path):
    case, bundle, baseline = get_fixture(tmp_path, "literal-expression-new-route")
    expanded = expand_context(bundle, ["index.js"])
    assert "index.js" in {entry["path"] for entry in bundle["selectedFiles"]}
    assert "index.js" in {entry["path"] for entry in compact_model_input(bundle)["expandableSelectedPaths"]}
    assert "index.js" not in {
        entry["path"] for entry in compact_model_input(expanded)["expandableSelectedPaths"]
    }
    release_snapshot(bundle["source"]["snapshotId"])
    request = {"kind": "needs_files", "requestedPaths": ["index.js"], "reason": "Read omitted constant"}
    partial = score_judgment_run(case, baseline, [request], baseline, bundles=[bundle])
    assert partial["fileRequests"]["appropriate"] == ["index.js"]
    assert partial["fileRequests"]["initialContentWithheld"] is True
    assert partial["fileRequests"]["partiallySelectedPaths"] == ["index.js"]
    complete = score_judgment_run(case, baseline, [request], baseline, bundles=[expanded])
    assert complete["fileRequests"]["appropriate"] == []
    assert complete["fileRequests"]["initialContentWithheld"] is False


def test_v2_wire_counts_only_explicit_change_not_adapter_copied_service_fields(tmp_path):
    from iris_analyzer.opencode.review_protocol import canonicalize_review

    case, bundle, baseline = get_fixture(tmp_path, "normal-noop")
    try:
        service = baseline["services"][0]
        field = copy.deepcopy(service["startCommand"])
        field["status"] = "suggested"
        wire = {
            "kind": "review",
            "changes": [
                {
                    "target": "services.startCommand",
                    "serviceId": service["serviceId"],
                    "field": field,
                }
            ],
            "reviewFindings": [],
            "questions": [],
        }
        canonical = canonicalize_review(wire, bundle)
        score = score_judgment_run(case, baseline, [canonical], baseline, wire_replies=[wire])
        assert score["claims"]["rawNewCount"] == 0
        assert score["claims"]["duplicateStaticCount"] == 1
        assert score["claims"]["adapterCopiedBaselineCount"] > 0
        assert score["rawContributionBasis"] == "model_wire_deltas"
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_v2_expanded_static_port_is_not_model_authored_discovery(tmp_path):
    from iris_analyzer.opencode.review_protocol import canonicalize_review
    from iris_analyzer.result import validate_analysis_with_report

    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"start": "node gateway.cjs"},
                "dependencies": {"express": "5"},
            }
        )
    )
    (tmp_path / "gateway.cjs").write_text(
        "const web=require('express'); const app=web(); app.listen(4637);\n"
    )
    (tmp_path / "secondary.js").write_text(
        "const web=require('express'); const second=web(); second.listen(5842);\n"
    )
    bundle = prepare_context(tmp_path)
    try:
        baseline = static_analysis(bundle)
        assert not any(fact["value"] == 5842 for fact in atomic_claims(baseline).values())
        expanded = expand_context(bundle, ["secondary.js"])
        service = static_analysis(expanded)["services"][0]
        wire = {
            "kind": "review",
            "changes": [
                {
                    "target": "services.runtime",
                    "serviceId": service["serviceId"],
                    "field": {
                        "value": "node",
                        "status": "suggested",
                        "scope": "source",
                        "reason": "Source runtime",
                        "evidenceIds": [
                            e["evidenceId"] for e in expanded["evidence"] if e["path"] == "package.json"
                        ],
                    },
                }
            ],
            "reviewFindings": [],
            "questions": [],
        }
        canonical = canonicalize_review(wire, expanded)
        assert any(fact["value"] == 5842 for fact in atomic_claims(canonical["result"]).values())
        merged, verification = validate_analysis_with_report(canonical, expanded)
        request = {"kind": "needs_files", "requestedPaths": ["secondary.js"], "reason": "Inspect second file"}
        score = score_judgment_run(
            CASES["normal-noop"],
            baseline,
            [request, canonical],
            merged,
            bundles=[bundle, expanded],
            verification=verification,
            wire_replies=[request, wire],
        )
        assert score["claims"]["rawNewCount"] == score["claims"]["truePositive"] == 0
        assert score["claims"]["duplicateStaticCount"] == 1
        assert score["claims"]["mergedNotModelAuthoredCount"] >= 1
        assert score["claims"]["adapterOwnedClaimCount"] > score["claims"]["adapterCopiedBaselineCount"]
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_v2_wire_capture_persists_original_model_delta_for_offline_rescoring(tmp_path):
    from iris_analyzer.opencode.review_protocol import canonicalize_review

    class WireRunner(EmptyRunner):
        last_wire_reply = None

        def invoke_model(self, bundle):
            service = static_analysis(bundle)["services"][0]
            field = copy.deepcopy(service["runtime"])
            field["status"] = "suggested"
            self.last_wire_reply = {
                "kind": "review",
                "changes": [
                    {
                        "target": "services.runtime",
                        "serviceId": service["serviceId"],
                        "field": field,
                    }
                ],
                "reviewFindings": [],
                "questions": [],
            }
            return canonicalize_review(self.last_wire_reply, bundle)

    output = tmp_path / "wire"
    report = evaluate_judgment_cases(CORPUS_PATH, out=output, runner=WireRunner(), case_ids=["normal-noop"])
    raw = json.loads((output / "normal-noop/run-01/captured-wire-replies.json").read_text())
    assert raw[0]["kind"] == "review" and len(raw[0]["changes"]) == 1
    score = report["cases"][0]["runs"][0]["evaluation"]
    assert score["claims"]["duplicateStaticCount"] == 1
    assert score["claims"]["rawNewCount"] == 0
