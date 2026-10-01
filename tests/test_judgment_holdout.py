"""Reviewed holdout gold, source ranges and baseline; no paid model calls."""

import json
from copy import deepcopy
from pathlib import Path, PurePosixPath

import pytest

from iris_analyzer.judgment_evaluation import evaluate_judgment_cases, score_judgment_run
from iris_analyzer.opencode.runner import response_template
from iris_analyzer.preprocess import compact_model_input, expand_context, prepare_context, release_snapshot
from iris_analyzer.result import static_analysis, validate_analysis
from iris_analyzer.verification import verify_proposals

ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / "evaluations/ai-judgment-holdout.json"
CORPUS = json.loads(CORPUS_PATH.read_text())
CASES = CORPUS["cases"]


def materialize(root, files):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def covered(bundle, evidence):
    available = {
        line
        for item in bundle["evidence"]
        if item["path"] == evidence["path"]
        for line in range(item["startLine"], item["endLine"] + 1)
    }
    return set(range(evidence["startLine"], evidence["endLine"] + 1)) <= available


def test_holdout_is_reviewed_gold_and_not_an_embedded_prompt_or_model_result():
    assert CORPUS["purpose"] == "reviewed_holdout_policy_spec"
    assert CORPUS["modelEvaluationPerformed"] is False
    assert len(CASES) >= 4
    assert len({case["input"]["caseId"] for case in CASES}) == len(CASES)
    prompt = (ROOT / "src/iris_analyzer/opencode/prompts/deployment.txt").read_text()
    assert "/ops/alive" not in prompt and "4637" not in prompt
    assert {case["expectedResult"]["disposition"] for case in CASES} == {"suggest", "abstain", "no_change"}


@pytest.mark.parametrize("case", CASES, ids=[case["input"]["caseId"] for case in CASES])
def test_holdout_gold_matches_exact_source_evidence_and_real_baseline(case, tmp_path):
    assert set(case) == {
        "input",
        "evidenceToCheck",
        "allowedConclusions",
        "forbiddenClaims",
        "expectedResult",
    }
    files = case["input"]["sourceFiles"]
    for name in files:
        assert not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts
    materialize(tmp_path, files)
    bundle = prepare_context(tmp_path)
    try:
        baseline = static_analysis(bundle)
        assert baseline["status"] == case["input"]["baselineStatus"]
        for assertion in case["input"]["baselineAssertions"]:
            assert (
                any(
                    all(fact.get(key) == value for key, value in assertion["match"].items())
                    for fact in bundle["facts"]
                )
                is assertion["present"]
            )
        assert set(case["input"]["selectedPaths"]) <= {x["path"] for x in bundle["selectedFiles"]}
        expandable = {item["path"] for item in compact_model_input(bundle)["expandableSelectedPaths"]}
        requested = set(case["expectedResult"]["expectedFileRequests"])
        assert requested <= set(case["input"]["expandableSelectedPaths"]) | set(
            case["input"]["unselectedAvailablePaths"]
        )
        assert set(case["input"]["expandableSelectedPaths"]) <= expandable
        aliases = {item["id"] for item in case["evidenceToCheck"]}
        expanded = expand_context(bundle, sorted(requested)) if requested else bundle
        for item in case["evidenceToCheck"]:
            lines = files[item["path"]].splitlines()
            assert 1 <= item["startLine"] <= item["endLine"] <= len(lines)
            assert item["exactText"] == "\n".join(lines[item["startLine"] - 1 : item["endLine"]])
            assert item["relationship"]
            if item["availability"] == "initial":
                assert covered(bundle, item)
            elif item["availability"] == "after_expansion":
                assert not covered(bundle, item)
                assert covered(expanded, item)
            else:
                pytest.fail("Unknown evidence availability")
        for claim in case["allowedConclusions"]:
            assert claim["supportingEvidenceIds"] and set(claim["supportingEvidenceIds"]) <= aliases
        for claim in case["expectedResult"]["expectedNewClaims"]:
            assert set(claim["evidenceIds"]) <= aliases
        assert case["expectedResult"]["scoredAsActualModelResult"] is False
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_holdout_static_evaluation_reports_no_model_measurement(tmp_path):
    report = evaluate_judgment_cases(CORPUS_PATH, out=tmp_path / "evaluation")
    assert report["mode"] == "static_baseline_only"
    assert report["modelEvaluationPerformed"] is False
    assert report["summary"]["baselinePassed"] is True
    assert report["summary"]["caseCount"] == len(CASES)
    assert report["summary"]["runCount"] == 0
    assert report["summary"]["measuredGatesPassed"] is None


def test_reviewed_positive_can_be_verified_after_requesting_selected_partial_source(tmp_path):
    case = next(case for case in CASES if case["expectedResult"]["disposition"] == "suggest")
    materialize(tmp_path, case["input"]["sourceFiles"])
    initial = prepare_context(tmp_path)
    try:
        baseline = static_analysis(initial)
        request = {
            "kind": "needs_files",
            "requestedPaths": ["gateway.cjs"],
            "reason": "Read the missing local constant and its factory binding",
        }
        expanded = expand_context(initial, request["requestedPaths"])
        proposed = deepcopy(case["expectedResult"]["expectedNewClaims"][0])
        reply = response_template(expanded)
        reply["result"]["apiRoutes"] = [
            {
                "value": proposed["value"],
                "status": "suggested",
                "scope": proposed["scope"],
                "evidenceIds": [e["evidenceId"] for e in expanded["evidence"] if e["path"] == "gateway.cjs"],
                "reason": "Immutable literal concatenation is used by the Express GET registration",
            }
        ]
        _, verification = verify_proposals(reply, expanded)
        merged = validate_analysis(reply, expanded)
        score = score_judgment_run(
            case, baseline, [request, reply], merged, bundles=[initial, expanded], verification=verification
        )
        assert score["claims"]["truePositive"] == score["claims"]["admittedTruePositive"] == 1
        assert score["claims"]["falsePositive"] == 0
        assert score["fileRequests"]["appropriate"] == ["gateway.cjs"]
        assert score["gatePassed"] is True
    finally:
        release_snapshot(initial["source"]["snapshotId"])
