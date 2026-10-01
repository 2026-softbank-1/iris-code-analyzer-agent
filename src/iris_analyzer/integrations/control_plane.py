"""Async library boundary for the observed Iris WAS worker architecture.

No queue kind, HTTP route, database schema or source checkout contract is created
here. A caller supplies an already materialized, worker-owned source directory.
"""

from __future__ import annotations

import asyncio
import copy
import math
import threading
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator, Protocol

from ..budget import BudgetedRunner
from ..contracts import AnalyzerError, Limits, validate_result
from ..pipeline import ModelRunner, PipelineRun, analyze_with_report


class BackendJobStatus(StrEnum):
    """Existing iris-was JobStatus codes; analysis status is a separate value."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"


_KNOWN_STAGES = {
    "queued",
    "preprocessing",
    "analyzing",
    "expanding",
    "validating",
    "succeeded",
    "needs_input",
    "unsupported",
    "failed",
}


@dataclass(frozen=True)
class AnalysisProgress:
    stage: str
    job_status_hint: BackendJobStatus
    elapsed_seconds: float
    source_snapshot_id: str | None = None
    context_hash: str | None = None
    revision: int | None = None

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> AnalysisProgress:
        stage = event.get("stage")
        if stage not in _KNOWN_STAGES:
            raise AnalyzerError("INTEGRATION_EVENT_INVALID", "Unknown analysis stage")
        # Pipeline terminal events precede artifact writes. Queue owners commit
        # final status with the returned outcome/error, not a progress callback.
        status = BackendJobStatus.RUNNING
        elapsed = event.get("elapsedSeconds", 0.0)
        if (
            isinstance(elapsed, bool)
            or not isinstance(elapsed, (int, float))
            or not math.isfinite(elapsed)
            or elapsed < 0
        ):
            raise AnalyzerError("INTEGRATION_EVENT_INVALID", "Invalid analysis event time")
        return cls(
            str(stage),
            status,
            float(elapsed),
            event.get("snapshotId"),
            event.get("contextHash"),
            event.get("revision"),
        )


@dataclass(frozen=True)
class AnalysisOutcome:
    analysis_result: dict[str, Any]
    run_report: dict[str, Any]

    @property
    def execution_status(self) -> BackendJobStatus:
        # An executed analysis may honestly report missing/unsupported input.
        # This is not the WAS's operational MANUAL_INTERVENTION failure state.
        return BackendJobStatus.SUCCEEDED

    @property
    def analysis_status(self) -> str:
        return str(self.analysis_result["status"])

    @property
    def review_required(self) -> bool:
        return self.analysis_status != "complete"

    def to_response_data(self) -> dict[str, Any]:
        """Proposed JSON data payload, wrapped by WAS ApiResponse at its router.

        Keep analysis_result as a JSON dictionary: unknown Field.value=null is
        required by its schema and must survive WAS's exclude_none serialization.
        No local source path, credential or source-bearing run artifact is exposed.
        """
        validate_result(self.analysis_result)
        return {
            "contractVersion": "iris.control-plane.analysis.v1-draft",
            "analysisExecutionStatus": self.execution_status.value,
            "analysisStatus": self.analysis_status,
            "analysisMode": self.run_report["mode"],
            "deploymentAuthorized": False,
            "reviewRequired": self.review_required,
            "sourceSnapshotId": self.analysis_result["sourceSnapshotId"],
            "contextHash": self.analysis_result["contextHash"],
            "analysisResult": copy.deepcopy(self.analysis_result),
        }


class AnalysisClientError(Exception):
    """Sanitized client error; retry/resume policy belongs to the queue owner."""

    retryable = False

    def __init__(self, code: str):
        self.code = code
        super().__init__("Repository analysis failed")


ProgressSink = Callable[[AnalysisProgress], Awaitable[None]]
RunnerFactory = Callable[[threading.Event], AbstractContextManager[ModelRunner]]


class AnalysisClient(Protocol):
    async def analyze_repository(
        self,
        source_root: str | Path,
        *,
        out: str | Path | None = None,
        on_progress: ProgressSink | None = None,
    ) -> AnalysisOutcome: ...


class LocalAnalysisClient:
    """Run synchronous preprocessing/model I/O off the backend event loop.

    Runner lifetime is owned by the worker thread. Cancellation waits for that
    lifetime to end before releasing the slot, so another job cannot inherit an
    abandoned model call. Runner factories must honor the supplied cancel event.
    """

    def __init__(
        self,
        *,
        runner_factory: RunnerFactory | None = None,
        limits: Limits | None = None,
        max_concurrency: int = 1,
    ):
        if type(max_concurrency) is not int or max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer")
        self.runner_factory = runner_factory
        self.limits = limits or Limits()
        self._slots = asyncio.Semaphore(max_concurrency)

    async def analyze_repository(
        self,
        source_root: str | Path,
        *,
        out: str | Path | None = None,
        on_progress: ProgressSink | None = None,
    ) -> AnalysisOutcome:
        loop = asyncio.get_running_loop()
        cancelled = threading.Event()

        def progress(event: dict[str, Any]) -> None:
            if cancelled.is_set() and event["stage"] != "failed":
                raise AnalyzerError("ANALYSIS_CANCELLED", "Analysis was cancelled")
            if on_progress:
                future = asyncio.run_coroutine_threadsafe(
                    on_progress(AnalysisProgress.from_event(event)), loop
                )
                future.result()  # Database/event errors propagate to the worker owner.

        def execute() -> PipelineRun:
            if cancelled.is_set():
                raise AnalyzerError("ANALYSIS_CANCELLED", "Analysis was cancelled")
            context = self.runner_factory(cancelled) if self.runner_factory else nullcontext(None)
            with context as runner:
                if cancelled.is_set():
                    raise AnalyzerError("ANALYSIS_CANCELLED", "Analysis was cancelled")
                return analyze_with_report(
                    source_root, runner=runner, limits=self.limits, out=out, on_event=progress
                )

        async with self._slots:
            task = asyncio.create_task(asyncio.to_thread(execute))
            try:
                run = await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled.set()
                # No thread/process or slot is leaked. Model abort is performed
                # by OpenCodeRunner when it observes the cancellation event.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue  # Repeated cancellation must not abandon the thread.
                    except Exception:
                        break  # Retrieve the failure below while preserving cancellation.
                if task.done() and not task.cancelled():
                    task.exception()  # Consume its outcome; original cancellation wins.
                raise
            except AnalyzerError as error:
                raise AnalysisClientError(error.code) from None
            except OSError:
                raise AnalysisClientError("ANALYSIS_IO_FAILED") from None
        return AnalysisOutcome(copy.deepcopy(run.result), copy.deepcopy(run.report))


def create_live_runner_factory(
    config: Any,
    *,
    budget_ledger: str | Path,
    max_cost_usd: float = 1.0,
    executable: str | Path = "opencode",
    pricing: dict[str, Any] | None = None,
) -> RunnerFactory:
    """Create isolated/budgeted runners using caller-injected worker settings."""
    from ..opencode import IsolatedOpenCodeServer, OpenCodeRunner

    @contextmanager
    def factory(cancelled: threading.Event) -> Iterator[ModelRunner]:
        if config.server_url:
            with OpenCodeRunner(config, cancel_event=cancelled) as model:
                yield BudgetedRunner(model, budget_ledger, max_cost_usd=max_cost_usd, pricing=pricing)
        else:
            with IsolatedOpenCodeServer(config, executable=executable) as server:
                with OpenCodeRunner(server.config, cancel_event=cancelled) as model:
                    yield BudgetedRunner(model, budget_ledger, max_cost_usd=max_cost_usd, pricing=pricing)

    return factory
