import asyncio
import copy
import json
import shutil
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from iris_analyzer.budget import BudgetedRunner
from iris_analyzer.contracts import AnalyzerError, digest
from iris_analyzer.deployment import client as planning_client
from iris_analyzer.deployment.advisor import ADVICE_SCHEMA, PlanningOpenCodeRunner, advisor_input
from iris_analyzer.deployment.cli import main
from iris_analyzer.deployment.dossier import prepare_readiness
from iris_analyzer.deployment.planner import prepare_planning_request
from iris_analyzer.opencode import ModelConfig
from iris_analyzer.pipeline import analyze_snapshot

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/separated-web-api"


@pytest.fixture
def source_documents():
    analysis = analyze_snapshot(FIXTURE)
    return analysis, prepare_readiness(FIXTURE, analysis=analysis)


def advice_for(bundle):
    return {
        "schemaVersion": "iris.planning-advice.v1",
        "analysisDigest": digest(bundle["analysisResult"]),
        "requestDigest": digest(bundle["planningRequest"]),
        "target": {"stack": "aws_eks", "cloud": "aws", "region": "ap-northeast-2", "architecture": "x86_64"},
        "instanceType": None,
        "availability": "single_az",
        "expectedRps": 20,
        "reason": "Initial unmeasured test assumptions; image, network and runtime bindings remain necessary",
        "workloads": [
            {
                "serviceId": service["serviceId"],
                "cpuMillicores": 250,
                "memoryMiB": 512,
                "reason": "Unmeasured starting envelope",
            }
            for service in bundle["analysisResult"]["services"]
        ],
    }


def deny_external_actions(monkeypatch, *, allow_git_metadata=False):
    original_popen = subprocess.Popen

    def popen(command, *args, **kwargs):
        if (
            allow_git_metadata
            and isinstance(command, list)
            and len(command) == 6
            and command[:3] == ["git", "--no-pager", "-C"]
            and command[-2:] == ["rev-parse", "HEAD"]
        ):
            return original_popen(command, *args, **kwargs)
        return denied(command, *args, **kwargs)

    def denied(*args, **kwargs):
        raise AssertionError("Offline planning must not execute source or contact external systems")

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(httpx.Client, "send", denied)
    monkeypatch.setattr(planning_client, "live_advisor", denied)


def test_offline_report_writes_inspectable_draft_without_external_actions(
    source_documents, tmp_path, monkeypatch
):
    analysis, readiness = source_documents
    deny_external_actions(monkeypatch)
    dossier, report = planning_client.plan_with_report(analysis, readiness, out=tmp_path / "plans")
    assert report["mode"] == "policy" and report["calls"] == []
    assert dossier["deploymentPlan"]["status"] == "needs_input"
    assert dossier["execution"]["status"] == "blocked"
    assert dossier["benchmarkPlan"]["executed"] is False
    assert dossier["benchmarkPlan"]["executionAuthorized"] is False
    root = tmp_path / "plans" / report["planDigest"]
    assert json.loads((root / "deployment-dossier.json").read_text()) == dossier
    assert json.loads((root / "planning-run-report.json").read_text()) == report
    assert {path.name for path in (root / "execution").iterdir()} == {"execution.json"}
    assert not (root / "planning-advice.json").exists()


def test_same_digest_reuses_execution_and_changed_request_gets_own_bundle(
    source_documents, tmp_path, monkeypatch
):
    analysis, readiness = source_documents
    deny_external_actions(monkeypatch)
    output = tmp_path / "plans"
    first, first_report = planning_client.plan_with_report(analysis, readiness, out=output)
    execution = output / first_report["planDigest"] / "execution/execution.json"
    original_content, original_mtime = execution.read_bytes(), execution.stat().st_mtime_ns
    repeated, repeated_report = planning_client.plan_with_report(analysis, readiness, out=output)
    assert repeated == first and repeated_report["planDigest"] == first_report["planDigest"]
    assert execution.read_bytes() == original_content and execution.stat().st_mtime_ns == original_mtime
    changed_request = {
        "schemaVersion": "iris.planning-request.v1",
        "target": {"stack": "aws_eks", "environment": "test"},
        "constraints": {"expectedRps": 100},
    }
    changed, changed_report = planning_client.plan_with_report(
        analysis, readiness, changed_request, out=output
    )
    assert changed_report["planDigest"] != first_report["planDigest"]
    assert len(list(output.iterdir())) == 2
    assert execution.read_bytes() == original_content
    assert changed["deploymentPlan"]["request"]["constraints"]["expectedRps"] == 100


@pytest.mark.parametrize("change", ["edited", "extra-file", "symlink"])
def test_rerun_rejects_changed_execution_artifacts_without_overwriting(source_documents, tmp_path, change):
    analysis, readiness = source_documents
    output = tmp_path / "plans"
    _, report = planning_client.plan_with_report(analysis, readiness, out=output)
    root = output / report["planDigest"]
    original_dossier = (root / "deployment-dossier.json").read_bytes()
    execution = root / "execution"
    if change == "edited":
        (execution / "execution.json").write_text("tampered")
    elif change == "extra-file":
        (execution / "unexpected.tf").write_text('resource "unexpected" "source" {}')
    else:
        outside = tmp_path / "outside.json"
        outside.write_text("outside must remain unchanged")
        (execution / "execution.json").unlink()
        (execution / "execution.json").symlink_to(outside)
    with pytest.raises(AnalyzerError) as changed:
        planning_client.plan_with_report(analysis, readiness, out=output)
    assert changed.value.code == "EXECUTION_ARTIFACT_CHANGED"
    assert (root / "deployment-dossier.json").read_bytes() == original_dossier
    if change == "symlink":
        assert outside.read_text() == "outside must remain unchanged"


def test_async_repeated_cancellation_waits_for_worker_context_cleanup(monkeypatch):
    entered, cleanup_started, finish_cleanup, cleaned = (threading.Event() for _ in range(4))

    @contextmanager
    def factory():
        entered.set()
        try:
            yield
        finally:
            cleanup_started.set()
            assert finish_cleanup.wait(2)
            cleaned.set()

    def worker(analysis, readiness, request, *, cancelled, **options):
        with factory():
            assert cancelled.wait(2)
            raise AnalyzerError("PLANNING_CANCELLED", "fixture cancellation")

    monkeypatch.setattr(planning_client, "plan_with_report", worker)

    async def check():
        task = asyncio.create_task(planning_client.plan_async({}, {}))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        assert await asyncio.to_thread(cleanup_started.wait, 2)
        assert not task.done() and not cleaned.is_set()
        task.cancel()
        finish_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()

    asyncio.run(check())


def test_offline_cli_writes_analysis_readiness_and_dossier_without_running_source(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    marker = tmp_path / "source-executed"
    package = repo / "api/package.json"
    document = json.loads(package.read_text())
    document.setdefault("scripts", {})["postinstall"] = (
        "node -e \"require('fs').writeFileSync('" + str(marker) + "','executed')\""
    )
    package.write_text(json.dumps(document))
    deny_external_actions(monkeypatch, allow_git_metadata=True)
    monkeypatch.setattr(
        ModelConfig,
        "from_env",
        lambda *args, **kwargs: pytest.fail("Offline CLI must not read model configuration"),
    )
    output = tmp_path / "output"
    assert main(["--repo", str(repo), "--out", str(output), "--offline"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["plannerMode"] == "policy" and printed["execution"] == "blocked"
    assert (output / "analysis/analysis-result.json").is_file()
    assert (output / "source-readiness.json").is_file()
    assert (output / "deployment-dossier.json").is_file()
    assert (output / "readiness-context/evidence.jsonl").is_file()
    assert not marker.exists()
    dossier = json.loads((output / "deployment-dossier.json").read_text())
    assert (
        dossier["sourceLink"]["sourceSnapshotId"] == dossier["deploymentPlan"]["source"]["sourceSnapshotId"]
    )


@pytest.mark.parametrize("symlink", [False, True])
def test_cli_rejects_output_inside_source_before_any_write(tmp_path, monkeypatch, capsys, symlink):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    inside = repo / "planning-output"
    output = inside
    if symlink:
        inside.mkdir()
        output = tmp_path / "outside-link"
        output.symlink_to(inside, target_is_directory=True)
    deny_external_actions(monkeypatch)
    assert main(["--repo", str(repo), "--out", str(output), "--offline"]) == 2
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "code": "PLANNING_OUTPUT_INVALID"}
    assert not inside.exists() or not list(inside.iterdir())


def test_mock_advice_uses_exact_input_digests_and_shared_budget_guard(
    source_documents, tmp_path, monkeypatch
):
    analysis, readiness = source_documents
    config = ModelConfig(server_url="http://unused.test", api_key="fixture")
    observed, closed = [], []

    class Runner:
        def __init__(self, config, *, cancel_event):
            self.config, self.calls = config, []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def invoke_model(self, bundle):
            observed.append(copy.deepcopy(bundle))
            self.calls.append({"usage": {"input": 10, "output": 20}, "remoteRetryAttempts": 0})
            return advice_for(bundle)

    monkeypatch.setattr(planning_client, "PlanningOpenCodeRunner", Runner)
    ledger = tmp_path / "budget.json"
    dossier, report = planning_client.plan_with_report(
        analysis, readiness, config=config, ledger=ledger, out=tmp_path / "plans"
    )
    expected = advisor_input(analysis, prepare_planning_request(), readiness=readiness)
    assert observed == [expected] and closed == [True]
    assert report["mode"] == "ai" and len(report["calls"]) == 1
    assert dossier["deploymentPlan"]["plannerMode"] == "ai"
    entries = json.loads(ledger.read_text())["entries"]
    assert len(entries) == 1 and entries[0]["state"] == "settled"
    assert entries[0]["contextHash"] == expected["contextHash"]
    assert entries[0]["actualCostUsd"] is None
    advice_path = tmp_path / "plans" / report["planDigest"] / "planning-advice.json"
    stored = json.loads(advice_path.read_text())
    assert stored["analysisDigest"] == digest(analysis)
    assert stored["requestDigest"] == dossier["deploymentPlan"]["requestDigest"]


def test_live_advisor_normalizes_relative_executable_before_runtime_boot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = ModelConfig(api_key="fixture")
    events, instances, received = [], [], []

    class Server:
        def __init__(self, config, *, executable):
            received.append(executable)
            self.config = replace(config, server_url="http://unused.test")

        def __enter__(self):
            events.append("server-enter")
            return self

        def __exit__(self, *args):
            events.append("server-exit")

    class Runner:
        def __init__(self, config, *, cancel_event):
            self.config, self.calls = config, []
            instances.append(self)

        def __enter__(self):
            events.append("runner-enter")
            return self

        def __exit__(self, *args):
            events.append("runner-exit")

    monkeypatch.setattr(planning_client, "IsolatedOpenCodeServer", Server)
    monkeypatch.setattr(planning_client, "PlanningOpenCodeRunner", Runner)
    with planning_client.live_advisor(
        config, ledger=tmp_path / "budget.json", executable="./tools/opencode"
    ) as runner:
        assert isinstance(runner, BudgetedRunner)
        assert runner.runner is instances[0]
    assert received == [str((tmp_path / "tools/opencode").resolve())]
    assert events == ["server-enter", "runner-enter", "runner-exit", "server-exit"]


class FakeClient:
    """Minimal existing-client protocol; never constructs HTTP or subprocesses."""

    def __init__(self, config):
        self.config = config
        self.attempts = 0
        self.server_version, self.schema_hash = "1.18.33", "f" * 64
        self.effective_model_policy = {"temperature": 0}
        self.remote_retry_attempts, self._pending_messages = {}, {}
        self.verify_count, self.session_count, self.prompts, self.aborts = 0, 0, [], []
        self.stale_output = False

    def verify(self, deadline):
        self.verify_count += 1

    def create_session(self, deadline):
        self.session_count += 1
        return "ses_fixture_" + str(self.session_count)

    def prompt(self, session, payload, *, deadline, cancel_event):
        self.prompts.append(copy.deepcopy(payload))
        document = json.loads(payload["parts"][0]["text"])
        reply = advice_for(document["context"])
        if self.stale_output:
            reply["analysisDigest"] = "0" * 64
        return {
            "info": {
                "id": "message-fixture",
                "parentID": payload["messageID"],
                "role": "assistant",
                "providerID": self.config.provider,
                "modelID": self.config.model,
                "tokens": {"input": 10, "output": 20},
                "finish": "stop",
                "structured": reply,
            },
            "parts": [{"type": "text", "text": json.dumps(reply)}],
        }

    def abort(self, session):
        self.aborts.append(session)
        return True

    def close(self):
        pytest.fail("Injected caller-owned client must not be closed by runner")


@pytest.mark.parametrize("output_mode", ["json_text", "structured"])
def test_advisor_hooks_validate_output_reuse_injected_client_and_fresh_sessions(
    source_documents, monkeypatch, output_mode
):
    analysis, readiness = source_documents
    config = ModelConfig(server_url="http://unused.test", api_key="fixture", output_mode=output_mode)
    client = FakeClient(config)
    validations = []

    class SpyRunner(PlanningOpenCodeRunner):
        def _validate_output(self, reply):
            validations.append(reply)
            return super()._validate_output(reply)

    monkeypatch.setattr(
        "iris_analyzer.opencode.runner.OpenCodeClient",
        lambda *args, **kwargs: pytest.fail("Injected client must be reused"),
    )
    bundle = advisor_input(analysis, readiness=readiness)
    changed_bundle = advisor_input(
        analysis,
        {
            "schemaVersion": "iris.planning-request.v1",
            "target": {"stack": "aws_eks", "environment": "test"},
            "constraints": {"expectedRps": 50},
        },
        readiness=readiness,
    )
    with SpyRunner(config, client=client) as runner:
        assert runner.invoke_model(bundle) == advice_for(bundle)
        assert runner.invoke_model(changed_bundle) == advice_for(changed_bundle)
        assert runner.client is client
        assert [record["promptVersion"] for record in runner.calls] == ["planning_advisor_v1"] * 2
        assert runner.calls[0]["sessionId"] != runner.calls[1]["sessionId"]
    assert len(validations) == 2 and client.verify_count == 2 and client.session_count == 2
    assert len(client.prompts) == 2 and client.aborts == []
    document = json.loads(client.prompts[0]["parts"][0]["text"])
    assert document["analysisDigest"] == digest(analysis)
    assert document["requestDigest"] == digest(bundle["planningRequest"])
    assert "contextBundle" not in document and "Terraform/HCL/YAML" in client.prompts[0]["system"]
    second_document = json.loads(client.prompts[1]["parts"][0]["text"])
    assert second_document["requestDigest"] == digest(changed_bundle["planningRequest"])
    assert second_document["requestDigest"] != document["requestDigest"]
    if output_mode == "structured":
        expected_schema = copy.deepcopy(ADVICE_SCHEMA)
        expected_schema["properties"]["analysisDigest"]["const"] = digest(analysis)
        expected_schema["properties"]["requestDigest"]["const"] = digest(bundle["planningRequest"])
        assert client.prompts[0]["format"]["schema"] == expected_schema
        assert "const" not in ADVICE_SCHEMA["properties"]["analysisDigest"]
        assert (
            client.prompts[1]["format"]["schema"]["properties"]["requestDigest"]["const"]
            == second_document["requestDigest"]
        )
    else:
        assert "format" not in client.prompts[0]


def test_tampered_advisor_input_stops_before_remote_calls_and_stale_output_aborts(source_documents):
    analysis, readiness = source_documents
    config = ModelConfig(server_url="http://unused.test", api_key="fixture")
    client = FakeClient(config)
    bundle = advisor_input(analysis, readiness=readiness)
    tampered = copy.deepcopy(bundle)
    tampered["contextHash"] = "0" * 64
    with PlanningOpenCodeRunner(config, client=client) as runner:
        with pytest.raises(AnalyzerError) as invalid:
            runner.invoke_model(tampered)
        assert invalid.value.code == "PLANNING_ADVICE_INVALID"
        assert client.verify_count == 0 and client.prompts == []
        client.stale_output = True
        with pytest.raises(AnalyzerError) as stale:
            runner.invoke_model(bundle)
        assert stale.value.code == "PLANNING_ADVICE_STALE"
        assert len(client.prompts) == 1 and len(client.aborts) == 1
