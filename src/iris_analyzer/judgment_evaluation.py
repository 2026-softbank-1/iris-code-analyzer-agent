"""Source-reviewed AI contribution evaluation, separate from static fixture checks.

Exact atoms are scored automatically. Unreviewed natural language and additional
claims remain visible rather than receiving invented semantic correctness scores.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from .contracts import AnalyzerError, Limits, digest
from .pipeline import ModelRunner, analyze_with_report, write_json
from .preprocess import compact_model_input, prepare_context, release_snapshot
from .result import static_analysis

_COLLECTIONS = ("dependencies", "apiRoutes", "environmentKeys", "connections")
_SERVICE_FIELDS = (
    "role",
    "runtime",
    "buildCommand",
    "startCommand",
    "workingDirectory",
    "outputDirectory",
    "ports",
    "healthchecks",
)
_ISSUE_CATEGORIES = {
    "documentation_disagreement": "documentation_mismatch",
    "final_runtime_not_represented": "runtime_stage_mismatch",
    "unsupported_evidence_relationship": "missing_evidence",
}
_QUESTION_KEYS = {
    "resolved_container_port": "runtime.port",
    "container_port": "runtime.port",
    "database_connection": "deployment.connection",
}


def _atom(target: str, field: dict, component: str = ".") -> dict | None:
    value = copy.deepcopy(field.get("value"))
    if field.get("status") == "unknown" or value is None:
        return None
    if isinstance(value, dict):
        component = value.pop("component", component)
        if target == "apiRoutes" and isinstance(value.get("method"), str):
            value["method"] = value["method"].upper()
    return {"target": target, "component": component, "scope": field.get("scope", "source"), "value": value}


def atomic_claims(result: dict) -> dict[str, dict]:
    """Ignore confidence/provenance phrasing; preserve value, scope and owner."""
    atoms: dict[str, dict] = {}
    for target in _COLLECTIONS:
        for field in result.get(target, []):
            atom = _atom(target, field)
            if atom is not None:
                atoms[digest(atom)] = atom
    for service in result.get("services", []):
        component = service.get("root", {}).get("value", ".")
        for name in _SERVICE_FIELDS:
            fields = service.get(name, [])
            fields = fields if isinstance(fields, list) else [fields]
            for field in fields:
                if isinstance(field, dict):
                    atom = _atom("services." + name, field, component)
                    if atom is not None:
                        if service.get("serviceId"):
                            atom["serviceId"] = service["serviceId"]
                        atoms[digest(atom)] = atom
    return atoms


def _model_authored_reply(canonical: dict, wire: dict | None) -> dict:
    """Strip trusted adapter scaffolding from a v2 model-authored delta reply."""
    if wire is None or wire.get("kind") != "review":
        return canonical if wire is None else wire
    result: dict = {key: [] for key in _COLLECTIONS}
    result["questions"] = copy.deepcopy(wire.get("questions", []))
    canonical_services = {
        service["serviceId"]: service for service in canonical.get("result", {}).get("services", [])
    }
    services: dict[str, dict] = {}
    for change in wire.get("changes", []):
        target, field = change["target"], copy.deepcopy(change["field"])
        if target in _COLLECTIONS:
            result[target].append(field)
            continue
        service_id = change["serviceId"]
        if service_id not in canonical_services:
            raise ValueError("wire delta has no matching canonical service identity")
        if service_id not in services:
            services[service_id] = {
                "serviceId": service_id,
                "root": copy.deepcopy(canonical_services[service_id]["root"]),
            }
        name = target.removeprefix("services.")
        if name in {"ports", "healthchecks"}:
            services[service_id].setdefault(name, []).append(field)
        else:
            services[service_id][name] = field
    result["services"] = list(services.values())
    return {
        "kind": "analysis",
        "result": result,
        "reviewFindings": copy.deepcopy(wire.get("reviewFindings", [])),
    }


def _lookup(reply: dict, path: list) -> Any:
    value: Any = reply
    for segment in path:
        try:
            value = value[segment]
        except (KeyError, IndexError, TypeError):
            return None
    return value


def _decision_atom(reply: dict, decision: dict) -> dict | None:
    path = decision.get("fieldPath", [])
    field = _lookup(reply, path)
    if not isinstance(field, dict) or len(path) < 2:
        return None
    if path[0] == "result" and path[1] in _COLLECTIONS:
        return _atom(path[1], field)
    if len(path) >= 4 and path[:2] == ["result", "services"]:
        service = _lookup(reply, path[:3]) or {}
        atom = _atom("services." + path[3], field, service.get("root", {}).get("value", "."))
        if atom is not None and service.get("serviceId"):
            atom["serviceId"] = service["serviceId"]
        return atom
    return None


def _evidence_covers(case: dict, bundle: dict, cited_ids: list[str], aliases: list[str]) -> bool:
    cited = [entry for entry in bundle.get("evidence", []) if entry["evidenceId"] in cited_ids]
    gold = {entry["id"]: entry for entry in case["evidenceToCheck"]}
    if not aliases or not cited:
        return False
    for alias in aliases:
        wanted = gold[alias]
        covered = set()
        for entry in cited:
            if entry["path"] == wanted["path"]:
                covered.update(range(entry["startLine"], entry["endLine"] + 1))
        # Gold spans describe a relationship and can be wider than extractor
        # snippets (e.g. an import has no evidence ID). Bind every gold location
        # by overlap; a separate supported semantic verdict is also required.
        if not set(range(wanted["startLine"], wanted["endLine"] + 1)) & covered:
            return False
    return True


def _question_identity(
    question: dict, service_components: dict[str, str] | None = None, component: str = "."
) -> tuple[str, str]:
    key = question.get("key", "")
    for service_id, root in (service_components or {}).items():
        if key == service_id + ".ports":
            key, component = "runtime.port", root
            break
    key = _QUESTION_KEYS.get(key, key)
    if key in {"runtime.port", "deployment.connection"}:
        key += "@" + component
    return key, question.get("kind", "code_review")


def score_judgment_run(
    case: dict,
    baseline: dict,
    raw_replies: list[dict],
    merged: dict,
    *,
    bundles: list[dict] | None = None,
    verification: dict | None = None,
    wire_replies: list[dict | None] | None = None,
) -> dict:
    """Score the initial reply for withheld-file cases and final analysis elsewhere.

    A verifier's unsupported/deferred verdict establishes a support failure, not
    necessarily that a claim is false in the world. Such failures have their own
    counts; unknown factual correctness requires reviewed adjudication.
    """
    expected = case["expectedResult"]
    bundles = bundles or [{} for _ in raw_replies]
    verification = verification or {}
    initial_only = case["input"]["selectedContext"].get("evaluationPhase") == "initial_reply"
    if wire_replies is not None and len(wire_replies) != len(raw_replies):
        raise ValueError("wire replies must align with canonical replies")
    canonical_replies = raw_replies[:1] if initial_only else raw_replies
    wires = (wire_replies or [None for _ in raw_replies])[: len(canonical_replies)]
    scored_replies = [_model_authored_reply(reply, wire) for reply, wire in zip(canonical_replies, wires)]
    baseline_atoms = atomic_claims(baseline)
    merged_atoms = atomic_claims(merged)
    raw_atoms: dict[str, dict] = {}
    for reply in scored_replies:
        if reply.get("kind") == "analysis":
            raw_atoms.update(atomic_claims(reply.get("result", {})))
    expected_atoms = {}
    for field in expected["expectedNewClaims"]:
        atom = _atom(field["target"], field)
        if atom is not None:
            expected_atoms[digest(atom)] = atom
    # A fixture becoming statically covered is visible and cannot earn model credit.
    expected_novel = set(expected_atoms) - set(baseline_atoms)
    novel = set(raw_atoms) - set(baseline_atoms)
    merged_novel = set(merged_atoms) - set(baseline_atoms)
    unsupported: set[str] = set()
    deferred: set[str] = set()
    supported: set[str] = set()
    final_reply = next((r for r in reversed(canonical_replies) if r.get("kind") == "analysis"), {})
    for decision in verification.get("decisions", []):
        atom = _decision_atom(final_reply, decision)
        if atom is None:
            continue
        if decision.get("decision") == "rejected":
            unsupported.add(digest(atom))
        elif decision.get("decision") == "deferred":
            deferred.add(digest(atom))
        elif decision.get("decision") == "supported":
            supported.add(digest(atom))
    raw_gold_matches = novel & expected_novel
    true_positives = raw_gold_matches & supported & merged_novel
    unexpected = novel - expected_novel
    # Optional reviewed exact negative atoms, never a free-text keyword guess.
    forbidden = {
        digest(atom)
        for field in expected.get("forbiddenAtomicClaims", [])
        if (atom := _atom(field["target"], field)) is not None
    }
    false_positives = unexpected & forbidden
    unreviewed = unexpected - false_positives
    human_review = [
        {"kind": "unreviewed_novel_claim", "claim": raw_atoms[key], "unsupported": key in unsupported}
        for key in sorted(unreviewed)
    ]
    for key in sorted(raw_gold_matches - true_positives):
        human_review.append(
            {
                "kind": "gold_value_without_verified_admission",
                "claim": raw_atoms[key],
                "unsupported": key in unsupported,
                "deferred": key in deferred,
            }
        )
    model_issues = []
    for index, reply in enumerate(scored_replies):
        for finding in reply.get("reviewFindings", []):
            model_issues.append((finding, bundles[index] if index < len(bundles) else {}))
    verified_issues = verification.get("reviewFindings", [])
    issue_matches: set[str] = set()
    unreviewed_issues = 0
    for finding, bundle in model_issues:
        match = next(
            (
                gold
                for gold in expected["expectedNewIssues"]
                if finding.get("category") == _ISSUE_CATEGORIES.get(gold["id"], gold["id"])
                and _evidence_covers(case, bundle, finding.get("evidenceIds", []), gold["evidenceIds"])
            ),
            None,
        )
        confirmed = any(
            item.get("origin") == "model"
            and item.get("decision") == "supported"
            and item.get("category") == finding.get("category")
            and set(item.get("evidenceIds", [])) == set(finding.get("evidenceIds", []))
            and (match is None or item.get("blocking") == match["blocking"])
            for item in verified_issues
        )
        if match is not None and confirmed:
            issue_matches.add(match["id"])
            human_review.append(
                {
                    "kind": "model_issue_rationale",
                    "finding": finding,
                    "note": "The structured category and source relationship are verified; extra claims in the model's prose are not automatically scored.",
                }
            )
        else:
            unreviewed_issues += 1
            human_review.append({"kind": "unreviewed_model_issue", "finding": finding})
    service_components = {
        service["serviceId"]: service.get("root", {}).get("value", ".")
        for service in baseline.get("services", [])
        if service.get("serviceId")
    }
    selected_component = case["input"]["selectedContext"].get("component", ".")

    def question_identity(item: dict) -> tuple[str, str]:
        return _question_identity(item, service_components, selected_component)

    expected_questions = {question_identity(item) for item in expected["expectedAbstentions"]}
    baseline_questions = {question_identity(item) for item in baseline.get("questions", [])}
    raw_questions = {
        question_identity(item)
        for reply in scored_replies
        if reply.get("kind") == "analysis"
        for item in reply.get("result", {}).get("questions", [])
    }
    merged_questions = {question_identity(item) for item in merged.get("questions", [])}
    question_candidates = raw_questions & expected_questions
    baseline_rationales = {
        (question_identity(item), item.get("reason", "")) for item in baseline.get("questions", [])
    }
    seen_rationales = set()
    for reply in scored_replies:
        for item in reply.get("result", {}).get("questions", []):
            identity = question_identity(item)
            signature = (identity, item.get("reason", ""))
            if signature in baseline_rationales or signature in seen_rationales:
                continue
            seen_rationales.add(signature)
            human_review.append(
                {
                    "kind": "abstention_rationale",
                    "key": identity[0],
                    "questionKind": identity[1],
                    "goldIdentityMatch": identity in expected_questions,
                    "question": item,
                    "note": "Key/kind match alone does not verify rationale or requested secret handling.",
                }
            )
    requested = {
        path
        for reply in scored_replies
        if reply.get("kind") == "needs_files"
        for path in reply.get("requestedPaths", [])
    }
    expected_paths = set(expected["expectedFileRequests"])
    initial_bundle = bundles[0] if bundles else {}
    manifest = {entry["path"]: entry for entry in initial_bundle.get("manifest", [])}
    selected = {entry["path"] for entry in initial_bundle.get("selectedFiles", [])}
    # A selected path can contain only snippets. Use the same digest-backed
    # completeness policy exposed to the model, including after snapshot release.
    expandable = (
        {entry["path"] for entry in compact_model_input(initial_bundle)["expandableSelectedPaths"]}
        if initial_bundle.get("manifest") is not None and initial_bundle.get("selectedFiles") is not None
        else set()
    )
    withheld_paths = {
        path
        for path, entry in manifest.items()
        if entry.get("eligible") is True and (path not in selected or path in expandable)
    }
    appropriate_paths = {path for path in requested & expected_paths if path in withheld_paths}
    for reply in scored_replies:
        if reply.get("kind") == "needs_files":
            human_review.append({"kind": "file_request_rationale", "reason": reply.get("reason", "")})
    denominator = len(novel)
    claims = {
        "rawNewCount": len(novel),
        "mergedNewCount": len(merged_novel),
        "duplicateStaticCount": len(set(raw_atoms) & set(baseline_atoms)),
        "adapterCopiedBaselineCount": len(
            {
                key
                for reply, wire in zip(canonical_replies, wires)
                if wire is not None and wire.get("kind") == "review"
                for key in atomic_claims(reply.get("result", {}))
                if key in baseline_atoms and key not in raw_atoms
            }
        ),
        "adapterOwnedClaimCount": len(
            {
                key
                for reply, wire in zip(canonical_replies, wires)
                if wire is not None and wire.get("kind") == "review"
                for key in atomic_claims(reply.get("result", {}))
                if key not in raw_atoms
            }
        ),
        "mergedNotModelAuthoredCount": len(merged_novel - set(raw_atoms)),
        "rawGoldMatchCount": len(raw_gold_matches),
        "valueMatchedButUnsupported": len(raw_gold_matches & unsupported),
        "valueMatchedButUnverified": len(raw_gold_matches - supported - unsupported),
        "truePositive": len(true_positives),
        "falsePositive": len(false_positives),
        "falseNegative": len(expected_novel - true_positives),
        "unreviewedCount": len(unreviewed),
        "unsupportedProposalCount": len(unsupported & novel),
        "deferredProposalCount": len(deferred & novel),
        "escapedUnsupportedCount": len(unsupported & merged_novel),
        "escapedFalsePositiveCount": len(false_positives & merged_novel),
        "admittedTruePositive": len(true_positives & merged_novel),
        "precision": len(true_positives) / denominator
        if denominator and not unreviewed and not (raw_gold_matches - true_positives)
        else None,
        "precisionLowerBound": len(true_positives) / denominator if denominator else None,
        "recall": len(true_positives) / len(expected_novel) if expected_novel else None,
        "rawNewClaims": [raw_atoms[key] for key in sorted(novel)],
        "mergedNewClaims": [merged_atoms[key] for key in sorted(merged_novel)],
        "expectedAlreadyStaticCount": len(set(expected_atoms) & set(baseline_atoms)),
    }
    return {
        "claims": claims,
        "issues": {
            "modelTruePositive": len(issue_matches),
            "matchedGoldIds": sorted(issue_matches),
            "expected": len(expected["expectedNewIssues"]),
            "rawCount": len(model_issues),
            "verifierOnlyCount": sum(item.get("origin") == "verifier" for item in verified_issues),
            "unreviewedCount": unreviewed_issues,
        },
        "abstentions": {
            "expected": len(expected_questions),
            "identityMatches": len(question_candidates),
            "newIdentityMatches": len(question_candidates - baseline_questions),
            "baselineQuestionPreserved": baseline_questions <= merged_questions,
            "preservedExpectedBlocker": expected["disposition"] == "preserve_blocker"
            and bool(baseline_questions)
            and baseline_questions <= merged_questions
            and merged.get("status") == "needs_input",
            "rationaleAutomaticallyVerified": False,
        },
        "fileRequests": {
            "requested": sorted(requested),
            "expected": sorted(expected_paths),
            "appropriate": sorted(appropriate_paths),
            "missing": sorted(expected_paths - requested),
            "unexpected": sorted(requested - expected_paths),
            "initialContentWithheld": expected_paths <= withheld_paths,
            "partiallySelectedPaths": sorted(expected_paths & expandable),
        },
        "baselinePreserved": set(baseline_atoms) <= set(merged_atoms),
        "scoredPhase": "initial_reply" if initial_only else "all_replies",
        "rawContributionBasis": "model_wire_deltas"
        if any(wire is not None and wire.get("kind") == "review" for wire in wires)
        else "legacy_model_reply",
        "humanReview": human_review,
        "gatePassed": set(baseline_atoms) <= set(merged_atoms)
        and not (unsupported & merged_novel)
        and not (false_positives & merged_novel)
        and expected_novel <= true_positives
        and expected_paths <= appropriate_paths,
        "gateScope": "Baseline preservation, admitted-support checks, expected atomic discoveries and file paths only; not full semantic correctness.",
    }


class _RecordingRunner:
    def __init__(self, runner: ModelRunner):
        self.runner = runner
        self.replies: list[dict] = []
        self.bundles: list[dict] = []
        self.wire_replies: list[dict | None] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.runner, name)

    def invoke_model(self, bundle: dict) -> dict:
        self.bundles.append(copy.deepcopy(bundle))
        reply = self.runner.invoke_model(bundle)
        self.replies.append(copy.deepcopy(reply))
        self.wire_replies.append(copy.deepcopy(getattr(self.runner, "last_wire_reply", None)))
        return reply


def _materialize(root: Path, source_files: dict) -> None:
    for name, content in source_files.items():
        relative = PurePosixPath(name)
        if (
            not name
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in name
            or relative.as_posix() != name
            or any(ord(char) < 32 for char in name)
            or not isinstance(content, str)
        ):
            raise ValueError("evaluation fixture requires safe relative UTF-8 source files")
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def evaluate_judgment_cases(
    corpus_path: str | Path,
    *,
    out: str | Path,
    runner: ModelRunner | None = None,
    repetitions: int = 1,
    limits: Limits | None = None,
    case_ids: list[str] | None = None,
) -> dict:
    """Run a bounded corpus; a caller-provided runner decides whether APIs are called."""
    if type(repetitions) is not int or not 1 <= repetitions <= 10:
        raise ValueError("repetitions must be between 1 and 10")
    corpus = json.loads(Path(corpus_path).read_text(encoding="utf-8"))
    cases = corpus["cases"]
    known = [case["input"]["caseId"] for case in cases]
    if len(known) != len(set(known)) or any(not re.fullmatch(r"[a-z0-9-]+", name) for name in known):
        raise ValueError("case IDs must be unique safe names")
    if case_ids is not None and (not case_ids or not set(case_ids) <= set(known)):
        raise ValueError("case_ids must select known cases")
    destination = Path(out).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("evaluation output must be a new or empty directory")
    destination.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "schemaVersion": "iris.ai-judgment-evaluation.v1",
        "corpusDigest": digest(corpus),
        "mode": "live_model" if runner else "static_baseline_only",
        "repetitions": repetitions,
        "modelEvaluationPerformed": runner is not None,
        "cases": [],
        "limits": [
            "Static fixture checks are not model performance measurements.",
            "Unsupported proposal means its support is invalid, not necessarily a false world fact.",
            "Unreviewed claims and natural language require human adjudication; null precision means N/A.",
            "Constructed proposalUnderReview cases are validator tests, not spontaneous model errors.",
        ],
    }
    budget_exhausted = False
    for case in cases:
        source = case["input"]
        case_id = source["caseId"]
        if case_ids is not None and case_id not in case_ids:
            continue
        case_dir = destination / case_id
        fixture = case_dir / "fixture"
        _materialize(fixture, source["sourceFiles"])
        bundle = prepare_context(fixture, limits=limits)
        try:
            baseline = static_analysis(bundle)
            assertions = []
            for assertion in source["baselineAssertions"]:
                present = any(
                    all(fact.get(k) == v for k, v in assertion["match"].items()) for fact in bundle["facts"]
                )
                assertions.append({**assertion, "passed": present is assertion["present"]})
            write_json(case_dir / "baseline.json", baseline)
            write_json(case_dir / "baseline-bundle.json", bundle)
            record: dict = {
                "caseId": case_id,
                "category": source["category"],
                "baselineAssertions": assertions,
                "baselineStatus": baseline["status"],
                "runs": [],
                "baselineStatusMatches": "baselineStatus" not in source
                or baseline["status"] == source["baselineStatus"],
            }
            if "proposalUnderReview" in source:
                record.update(
                    modelEvaluationPerformed=False,
                    excludedFromModelMetrics=True,
                    exclusionReason="Constructed validator counterexample; candidate not injected into model input.",
                )
            elif runner is not None:
                if hasattr(runner, "set_evaluation_context"):
                    runner.set_evaluation_context(source["selectedContext"])
                for repeat in range(repetitions):
                    if budget_exhausted:
                        record["runs"].append(
                            {"success": False, "skipped": True, "errorCode": "MODEL_BUDGET_EXCEEDED"}
                        )
                        continue
                    run_dir = case_dir / f"run-{repeat + 1:02d}"
                    recorder = _RecordingRunner(runner)
                    run_record: dict = {}
                    try:
                        run = analyze_with_report(fixture, runner=recorder, limits=limits, out=run_dir)
                        verification = run.report.get("verification", {})
                        evaluation = score_judgment_run(
                            case,
                            baseline,
                            recorder.replies,
                            run.result,
                            bundles=recorder.bundles,
                            verification=verification,
                            wire_replies=recorder.wire_replies,
                        )
                        run_record.update(success=True, evaluation=evaluation, calls=run.report["calls"])
                    except AnalyzerError as error:
                        budget_exhausted = error.code == "MODEL_BUDGET_EXCEEDED"
                        run_record.update(success=False, errorCode=error.code)
                        path = run_dir / "run-report.json"
                        if path.is_file():
                            run_record["calls"] = json.loads(path.read_text())["calls"]
                        run_record["rawScoringUnavailable"] = (
                            "Pipeline failed; captured replies retained, never counted as passed."
                        )
                    finally:
                        write_json(run_dir / "captured-raw-replies.json", recorder.replies)
                        write_json(run_dir / "captured-wire-replies.json", recorder.wire_replies)
                        write_json(run_dir / "captured-contexts.json", recorder.bundles)
                    write_json(run_dir / "judgment-score.json", run_record)
                    record["runs"].append(run_record)
            else:
                record["modelEvaluationPerformed"] = False
            report["cases"].append(record)
        finally:
            release_snapshot(bundle["source"]["snapshotId"])
        write_json(destination / "judgment-report.json", report)
    runs = [run for case in report["cases"] for run in case["runs"]]
    measured = [run["evaluation"] for run in runs if run.get("success")]
    report["summary"] = {
        "caseCount": len(report["cases"]),
        "runCount": len(runs),
        "successfulRuns": sum(run.get("success", False) for run in runs),
        "failedRuns": sum(not run.get("success", False) and not run.get("skipped", False) for run in runs),
        "skippedRuns": sum(run.get("skipped", False) for run in runs),
        "rawNewClaimTruePositive": sum(score["claims"]["truePositive"] for score in measured),
        "rawNewClaimFalsePositive": sum(score["claims"]["falsePositive"] for score in measured),
        "unsupportedProposalCount": sum(score["claims"]["unsupportedProposalCount"] for score in measured),
        "escapedUnsupportedCount": sum(score["claims"]["escapedUnsupportedCount"] for score in measured),
        "modelIssueTruePositive": sum(score["issues"]["modelTruePositive"] for score in measured),
        "humanReviewCount": sum(len(score["humanReview"]) for score in measured),
        "baselinePassed": all(
            all(x["passed"] for x in case["baselineAssertions"]) and case["baselineStatusMatches"]
            for case in report["cases"]
        ),
        "measuredGatesPassed": all(score["gatePassed"] for score in measured) if measured else None,
    }
    write_json(destination / "judgment-report.json", report)
    return report
