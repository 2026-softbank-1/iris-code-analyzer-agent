"""Offline corpus integrity and deterministic baselines, not model compliance.

Gold labels describe desired AI policy. In particular, these tests do not claim
that the current proposal validator rejects semantically unrelated evidence.
"""

import json
from pathlib import Path, PurePosixPath

import pytest

from iris_analyzer.preprocess import expand_context, prepare_context, release_snapshot
from iris_analyzer.result import static_analysis

CORPUS = json.loads(
    (Path(__file__).resolve().parents[1] / "evaluations" / "ai-judgment-cases.json").read_text()
)
CASES = CORPUS["cases"]
CASE_IDS = [case["input"]["caseId"] for case in CASES]


def materialize(root, files):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def test_corpus_is_explicitly_a_future_policy_spec_with_discriminating_gold_labels():
    assert CORPUS["purpose"] == "reviewed_future_policy_spec"
    assert CORPUS["modelEvaluationPerformed"] is False
    assert len(CASE_IDS) == len(set(CASE_IDS)) == 11
    assert {case["expectedResult"]["disposition"] for case in CASES} >= {
        "no_change",
        "needs_files",
        "abstain",
        "reject_proposal",
        "suggest",
        "preserve_blocker",
    }
    by_id = {case["input"]["caseId"]: case for case in CASES}
    normal = by_id["normal-noop"]["expectedResult"]
    assert normal["expectedNewClaims"] == [] and normal["expectedNewIssues"] == []
    negative = by_id["valid-id-unrelated-evidence"]
    assert negative["input"]["baselineCoverage"] == "intended_ai_policy"
    assert negative["expectedResult"]["expectedNewClaims"] == []
    assert negative["expectedResult"]["disposition"] == "reject_proposal"
    positive = by_id["literal-expression-new-route"]["expectedResult"]["expectedNewClaims"]
    assert len(positive) == 1
    assert positive[0]["status"] == "suggested"
    assert positive[0]["value"] == {"method": "GET", "path": "/health/ready", "component": "."}


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_gold_evidence_locations_and_expectations_are_internally_consistent(case):
    assert set(case) == {
        "input",
        "evidenceToCheck",
        "allowedConclusions",
        "forbiddenClaims",
        "expectedResult",
    }
    files = case["input"]["sourceFiles"]
    for name in files:
        path = PurePosixPath(name)
        assert not path.is_absolute() and ".." not in path.parts
    evidence_ids = {e["id"] for e in case["evidenceToCheck"]}
    assert len(evidence_ids) == len(case["evidenceToCheck"])
    for evidence in case["evidenceToCheck"]:
        lines = files[evidence["path"]].splitlines()
        start, end = evidence["startLine"], evidence["endLine"]
        assert 1 <= start <= end <= len(lines)
        assert evidence["exactText"] == "\n".join(lines[start - 1 : end])
        assert evidence["relationship"]
    claims = {claim["id"]: claim for claim in case["allowedConclusions"]}
    assert len(claims) == len(case["allowedConclusions"])
    for claim in claims.values():
        assert claim["supportingEvidenceIds"]
        assert set(claim["supportingEvidenceIds"]) <= evidence_ids
    expected = case["expectedResult"]
    assert expected["scoredAsActualModelResult"] is False
    for addition in expected["expectedNewClaims"]:
        assert addition["claimId"] in claims
        assert addition["status"] == "suggested"
        assert set(addition["evidenceIds"]) <= evidence_ids
    for issue in expected["expectedNewIssues"]:
        assert issue["reason"] and set(issue["evidenceIds"]) <= evidence_ids
    for forbidden in case["forbiddenClaims"]:
        assert forbidden["statement"] and forbidden["reason"]
    assert len({item["id"] for item in case["forbiddenClaims"]}) == len(case["forbiddenClaims"])
    assert set(expected["expectedFileRequests"]) <= set(case["input"]["unselectedAvailablePaths"])
    if expected["disposition"] == "needs_files":
        assert expected["expectedFileRequests"] and not expected["expectedNewClaims"]


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_named_static_baseline_assertions_match_real_preprocessing(case, tmp_path):
    source = case["input"]
    materialize(tmp_path, source["sourceFiles"])
    bundle = prepare_context(tmp_path)
    try:
        result = static_analysis(bundle)
        for assertion in source["baselineAssertions"]:
            matches = [
                fact
                for fact in bundle["facts"]
                if all(fact.get(key) == value for key, value in assertion["match"].items())
            ]
            assert bool(matches) is assertion["present"], assertion
        if "baselineStatus" in source:
            assert result["status"] == source["baselineStatus"]
        selected = {entry["path"] for entry in bundle["selectedFiles"]}
        manifest = {entry["path"]: entry for entry in bundle["manifest"]}
        assert set(source["selectedPaths"]) <= selected
        for path in source["unselectedAvailablePaths"]:
            assert path not in selected and manifest[path]["eligible"]
        # Evidence aliases are future-model labels, never actual context IDs.
        assert not {e["id"] for e in case["evidenceToCheck"]} & {e["evidenceId"] for e in bundle["evidence"]}
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_requested_file_is_read_from_the_same_snapshot_and_not_the_mutated_checkout(tmp_path):
    case = next(c for c in CASES if c["input"]["caseId"] == "available-config-needs-files")
    materialize(tmp_path, case["input"]["sourceFiles"])
    bundle = prepare_context(tmp_path)
    try:
        (tmp_path / "release-settings.json").write_text('{"port":9999}\n')
        expanded = expand_context(bundle, case["expectedResult"]["expectedFileRequests"])
        assert expanded["source"]["snapshotId"] == bundle["source"]["snapshotId"]
        snippets = [e["text"] for e in expanded["evidence"] if e["path"] == "release-settings.json"]
        assert snippets and any('"port":3000' in text for text in snippets)
        assert all("9999" not in text for text in snippets)
    finally:
        release_snapshot(bundle["source"]["snapshotId"])
