import asyncio
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from iris_analyzer.contracts import AnalyzerError
from iris_analyzer.integrations import (
    AnalysisClientError,
    AnalysisProgress,
    BackendJobStatus,
    LocalAnalysisClient,
)
from iris_analyzer.result import static_analysis

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "separated-web-api"


def test_worker_bridge_preserves_result_and_keeps_progress_nonterminal(tmp_path):
    async def check():
        records = []
        owner_thread = threading.get_ident()

        async def sink(progress):
            assert threading.get_ident() == owner_thread
            records.append(progress)
            await asyncio.sleep(0)

        outcome = await LocalAnalysisClient().analyze_repository(
            FIXTURE, out=tmp_path / "output", on_progress=sink
        )
        assert outcome.execution_status == BackendJobStatus.SUCCEEDED
        assert outcome.analysis_status == "complete"
        assert not outcome.review_required
        assert all(p.job_status_hint == BackendJobStatus.RUNNING for p in records)
        assert records[0].stage == "queued"
        assert records[-1].stage == "succeeded"
        payload = outcome.to_response_data()
        assert payload["contractVersion"].endswith("draft")
        assert payload["analysisResult"]["services"]
        payload["analysisResult"]["services"].clear()
        assert outcome.analysis_result["services"]  # Response caller cannot mutate outcome.
        assert str(FIXTURE) not in str(payload)

    asyncio.run(check())


@pytest.mark.parametrize("fixture,status", [("dynamic-routes", "needs_input")])
def test_review_outcome_does_not_become_operational_failure(fixture, status):
    async def check():
        outcome = await LocalAnalysisClient().analyze_repository(FIXTURE.parent / fixture)
        assert outcome.analysis_status == status
        assert outcome.review_required
        assert outcome.execution_status == BackendJobStatus.SUCCEEDED
        assert outcome.to_response_data()["analysisStatus"] == status

    asyncio.run(check())


def test_unsupported_analysis_is_a_completed_review_outcome(tmp_path):
    (tmp_path / "main.py").write_text("print('fixture')\n")

    async def check():
        outcome = await LocalAnalysisClient().analyze_repository(tmp_path)
        assert outcome.analysis_status == "unsupported"
        assert outcome.execution_status == BackendJobStatus.SUCCEEDED
        assert outcome.review_required

    asyncio.run(check())


def test_worker_does_not_block_async_loop_and_bounds_runner_concurrency():
    active = 0
    maximum = 0
    guard = threading.Lock()

    class Runner:
        calls = []

        def invoke_model(self, bundle):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.04)
            with guard:
                active -= 1
            return {"kind": "analysis", "result": static_analysis(bundle)}

    @contextmanager
    def factory(cancelled):
        yield Runner()

    async def check():
        client = LocalAnalysisClient(runner_factory=factory, max_concurrency=1)
        ticking = 0

        async def heartbeat():
            nonlocal ticking
            for _ in range(20):
                ticking += 1
                await asyncio.sleep(0.004)

        await asyncio.gather(
            client.analyze_repository(FIXTURE), client.analyze_repository(FIXTURE), heartbeat()
        )
        assert ticking == 20
        assert maximum == 1

    asyncio.run(check())


def test_repeated_cancellation_closes_runner_before_slot_release():
    closed = threading.Event()

    class Runner:
        calls = []

        def __init__(self, cancelled):
            self.cancelled = cancelled

        def invoke_model(self, bundle):
            assert self.cancelled.wait(2)
            time.sleep(0.03)  # Cancellation teardown is still in progress.
            raise AnalyzerError("MODEL_CANCELLED", "safe")

    @contextmanager
    def factory(cancelled):
        try:
            yield Runner(cancelled)
        finally:
            closed.set()

    async def check():
        started = asyncio.Event()
        client = LocalAnalysisClient(runner_factory=factory)

        async def sink(event):
            if event.stage == "analyzing":
                started.set()

        task = asyncio.create_task(client.analyze_repository(FIXTURE, on_progress=sink))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        assert client._slots._value == 1

    asyncio.run(check())


def test_client_error_is_sanitized_and_does_not_declare_paid_retry():
    class Runner:
        calls = []

        def invoke_model(self, bundle):
            raise AnalyzerError(
                "MODEL_AUTH_FAILED", "synthetic credential must not escape", {"secret": "synthetic"}
            )

    @contextmanager
    def factory(cancelled):
        yield Runner()

    async def check():
        with pytest.raises(AnalysisClientError) as error:
            await LocalAnalysisClient(runner_factory=factory).analyze_repository(FIXTURE)
        assert error.value.code == "MODEL_AUTH_FAILED"
        assert not error.value.retryable
        assert "credential" not in str(error.value)

    asyncio.run(check())


def test_progress_persistence_error_propagates_to_queue_owner():
    class PersistenceError(Exception):
        pass

    async def check():
        async def sink(event):
            raise PersistenceError("fixture database failure")

        with pytest.raises(PersistenceError):
            await LocalAnalysisClient().analyze_repository(FIXTURE, on_progress=sink)

    asyncio.run(check())


@pytest.mark.parametrize(
    "event",
    [
        {"stage": "surprise"},
        {"stage": "queued", "elapsedSeconds": -1},
        {"stage": "queued", "elapsedSeconds": float("nan")},
    ],
)
def test_unknown_progress_is_rejected(event):
    with pytest.raises(AnalyzerError) as error:
        AnalysisProgress.from_event(event)
    assert error.value.code == "INTEGRATION_EVENT_INVALID"
