import copy
import json

import pytest

from iris_analyzer.cli import main
from iris_analyzer.contracts import MODEL_REPLY_SCHEMA, AnalyzerError, Limits, digest
from iris_analyzer.evaluation import evaluate_projects, score_result
from iris_analyzer.pipeline import analyze_snapshot, analyze_with_report
from iris_analyzer.preprocess import prepare_context, release_snapshot
from iris_analyzer.result import static_analysis


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    (repo / "package.json").write_text(
        json.dumps(
            {
                "name": "fixture",
                "scripts": {"build": "tsc", "start": "node dist/index.js"},
                "dependencies": {"express": "5.2.1"},
            }
        )
    )
    (repo / "src").mkdir()
    (repo / "src/index.ts").write_text(
        "import express from 'express';\nconst app = express();\n"
        "app.get('/health', (_req,res) => res.send('ok'));\n"
        "app.listen(3000, '0.0.0.0');\n"
    )
    (repo / "notes.ts").write_text("const observation = 'original';\n")
    return repo


class ObservedRunner:
    def __init__(self):
        self.calls = []

    def invoke_model(self, bundle):
        self.calls.append({"contextHash": bundle["contextHash"]})
        return {"kind": "analysis", "result": static_analysis(bundle)}


def test_offline_worker_events_and_artifacts(repository, tmp_path):
    events = []
    out = tmp_path / "result"
    run = analyze_with_report(repository, out=out, on_event=events.append)
    assert [e["stage"] for e in events[:3]] == ["queued", "preprocessing", "validating"]
    assert events[-1]["stage"] in {"succeeded", "needs_input"}
    assert run.report["mode"] == "static"
    assert run.report["calls"] == []
    assert json.loads((out / "analysis-result.json").read_text()) == run.result
    assert not (out / "model-response.json").exists()


def test_expansion_reads_fixed_snapshot_and_uses_new_revision(repository, tmp_path):
    class ExpansionRunner(ObservedRunner):
        def invoke_model(self, bundle):
            self.calls.append({"revision": bundle["revision"]})
            if len(self.calls) == 1:
                (repository / "notes.ts").write_text("const observation = 'changed-after-snapshot';\n")
                return {
                    "kind": "needs_files",
                    "requestedPaths": ["notes.ts"],
                    "reason": "Inspect observation",
                }
            assert bundle["revision"] == 2
            evidence = [e for e in bundle["evidence"] if e["path"] == "notes.ts"]
            assert evidence and "original" in evidence[0]["text"]
            assert "changed-after-snapshot" not in evidence[0]["text"]
            return {"kind": "analysis", "result": static_analysis(bundle)}

    run = analyze_with_report(repository, runner=ExpansionRunner(), out=tmp_path / "output")
    assert len(run.report["revisions"]) == 2
    assert run.report["revisions"][0]["contextHash"] != run.report["revisions"][1]["contextHash"]
    assert run.bundle["revision"] == 2


def test_repeated_file_requests_stop_with_observed_result(repository):
    class PersistentRunner(ObservedRunner):
        def invoke_model(self, bundle):
            self.calls.append({"revision": bundle["revision"]})
            return {"kind": "needs_files", "requestedPaths": ["notes.ts"], "reason": "Still uncertain"}

    runner = PersistentRunner()
    result = analyze_snapshot(repository, runner=runner, limits=Limits(max_expansions=1))
    assert len(runner.calls) == 2
    assert result["status"] == "needs_input"
    assert any(q["key"] == "additional_files" for q in result["questions"])
    assert result["apiRoutes"]  # An incomplete model never erases observed routes.


def test_model_failure_persists_code_and_calls(repository, tmp_path):
    class FailingRunner(ObservedRunner):
        def invoke_model(self, bundle):
            self.calls.append({"error": "MODEL_AUTH_FAILED"})
            raise AnalyzerError("MODEL_AUTH_FAILED", "Rejected")

    out = tmp_path / "failure"
    with pytest.raises(AnalyzerError, match="Rejected"):
        analyze_with_report(repository, runner=FailingRunner(), out=out)
    report = json.loads((out / "run-report.json").read_text())
    assert report["status"] == "failed"
    assert report["errors"] == [{"code": "MODEL_AUTH_FAILED"}]
    assert report["events"][-1]["stage"] == "failed"


def test_saved_model_input_matches_exact_prompt_envelope(repository, tmp_path):
    class RequestRunner(ObservedRunner):
        def invoke_model(self, bundle):
            envelope = {
                "responseSchema": MODEL_REPLY_SCHEMA,
                "contextBundle": bundle,
                "responseTemplate": {"kind": "analysis"},
            }
            self.last_request = {
                "system": "platform prompt",
                "parts": [{"type": "text", "text": json.dumps(envelope)}],
            }
            self.calls.append(
                {"requestPayloadDigest": digest(self.last_request), "modelPromptDigest": digest(envelope)}
            )
            return {"kind": "analysis", "result": static_analysis(bundle)}

    out = tmp_path / "artifacts"
    run = analyze_with_report(repository, runner=RequestRunner(), out=out)
    request = json.loads((out / "model-request.json").read_text())
    actual_input = json.loads((out / "model-input.json").read_text())
    assert actual_input == json.loads(request["parts"][0]["text"])
    assert digest(request) == run.report["calls"][0]["requestPayloadDigest"]
    assert digest(actual_input) == run.report["calls"][0]["modelPromptDigest"]


def test_per_run_model_call_logs_do_not_duplicate(repository):
    runner = ObservedRunner()
    first = analyze_with_report(repository, runner=runner)
    second = analyze_with_report(repository, runner=runner)
    assert len(runner.calls) == 2
    assert len(first.report["calls"]) == len(second.report["calls"]) == 1


def test_model_mutation_does_not_change_pipeline_input(repository):
    class MutatingRunner(ObservedRunner):
        def invoke_model(self, bundle):
            reply = {"kind": "analysis", "result": static_analysis(bundle)}
            bundle["facts"].clear()
            return reply

    before = analyze_snapshot(repository)
    after = analyze_snapshot(repository, runner=MutatingRunner())
    assert before == after


def test_cli_preprocess_in_repo_output_is_reproducible(repository, capsys):
    out = repository / "generated"
    args = ["preprocess", "--repo", str(repository), "--out", str(out)]
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert first == second


def test_cli_invalid_budget_returns_machine_error(repository, tmp_path, capsys):
    assert (
        main(
            [
                "preprocess",
                "--repo",
                str(repository),
                "--out",
                str(tmp_path / "out"),
                "--max-bundle-bytes",
                "1",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["errorCode"] == "CONTEXT_BUDGET_EXCEEDED"


def test_verify_startup_failure_records_no_model_call(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HIVE_AI", raising=False)
    env = tmp_path / "test.env"
    env.write_text("HIVE_AI=xxx\n")
    out = tmp_path / "verification"
    assert main(["verify", "--env-file", str(env), "--out", str(out)]) == 2
    assert json.loads(capsys.readouterr().err)["errorCode"] == "MODEL_AUTH_MISSING"
    report = json.loads((out / "model-verification-report.json").read_text())
    assert report["modelCalled"] is False
    assert report["failureCount"] == 1
    assert report["usage"] is None
    assert json.loads((out / "run-report.json").read_text())["calls"] == []


def test_evaluation_rejects_changed_reviewed_sources(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "package.json").write_text("{}")
    truth = tmp_path / "truth.json"
    truth.write_text(
        json.dumps(
            {
                "reviewDate": "2026-10-01",
                "projects": [{"name": "project", "sourceFiles": {"package.json": "0" * 64}}],
            }
        )
    )
    with pytest.raises(AnalyzerError) as exc:
        evaluate_projects(tmp_path, truth, out=tmp_path / "results")
    assert exc.value.code == "EVALUATION_FIXTURE_CHANGED"


def test_quality_checks_result_workdir_separately_from_correct_context(repository):
    (repository / "Dockerfile").write_text(
        'FROM node:24\nWORKDIR /app\nEXPOSE 3000\nCMD ["node","dist/index.js"]\n'
    )
    bundle = prepare_context(repository)
    result = static_analysis(bundle)
    truth = {
        "serviceCount": 1,
        "roles": ["api"],
        "componentRoots": ["."],
        "facts": [{"key": "docker.workdir", "value": "/app", "scope": "container"}],
        "serviceFields": [
            {"role": "api", "field": "workingDirectory", "value": "/app", "scope": "container"}
        ],
        "routes": [["GET", "/health"]],
        "requiredEnvironmentKeys": [],
        "dependencies": [],
        "frontendBaseUrls": [],
    }
    assert score_result(result, bundle, truth)["passed"]
    altered = copy.deepcopy(result)
    altered["services"][0]["workingDirectory"]["value"] = "/wrong"
    score = score_result(altered, bundle, truth)
    assert not score["passed"]
    assert any(check["passed"] for check in score["checks"] if check["key"].startswith("docker.workdir:"))
    release_snapshot(bundle["source"]["snapshotId"])


def test_partial_selected_declaration_is_exposed_for_bounded_expansion(tmp_path):
    from iris_analyzer.preprocess import (
        compact_model_input,
        expand_context,
        prepare_context,
        release_snapshot,
    )

    (tmp_path / "package.json").write_text(
        '{"dependencies":{"express":"5"},"scripts":{"start":"node index.js"}}'
    )
    (tmp_path / "index.js").write_text(
        "const express=require('express');\nconst app=express();\nconst path='/x/'+'ready';\napp.get(path,(_,r)=>r.send('ok'));\napp.listen(3000);\n"
    )
    bundle = prepare_context(tmp_path)
    try:
        payload = compact_model_input(bundle)
        assert "index.js" in {row["path"] for row in payload["expandableSelectedPaths"]}
        expanded = expand_context(bundle, ["index.js"])
        assert "index.js" not in {
            row["path"] for row in compact_model_input(expanded)["expandableSelectedPaths"]
        }
        assert any("'/x/'+'ready'" in row["text"] for row in expanded["evidence"])
    finally:
        release_snapshot(bundle["source"]["snapshotId"])
