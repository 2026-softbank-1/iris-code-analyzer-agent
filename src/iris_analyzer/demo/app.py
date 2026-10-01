"""Standalone localhost UI; GitHub source is data, never built or executed."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..contracts import AnalyzerError
from ..deployment.client import plan_async
from ..deployment.contracts import validate_planning_request
from ..deployment.dossier import prepare_readiness
from ..integrations import AnalysisClientError, LocalAnalysisClient, create_live_runner_factory
from ..opencode import ModelConfig
from .github import download_archive, parse_github_url, resolve_revision, unpack_source

STATIC = Path(__file__).parent / "static"


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_url: str = Field(min_length=10, max_length=600)
    ref: str | None = Field(default=None, max_length=300)
    use_ai: bool = True
    planning_request: dict[str, Any] | None = None


class ReplanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    planning_request: dict[str, Any] | None = None
    use_ai: bool | None = None


@dataclass
class ReviewJob:
    id: str
    repository_url: str
    ref: str | None
    use_ai: bool
    state: str = "queued"
    stage: str = "queued"
    source_sha: str | None = None
    repository: str | None = None
    result: dict[str, Any] | None = None
    source_info: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    evidence: dict[str, dict] = field(default_factory=dict)
    planning_request: dict[str, Any] | None = None
    deployment_dossier: dict[str, Any] | None = None

    def public(self) -> dict:
        return {
            "id": self.id,
            "repositoryUrl": self.repository_url,
            "repository": self.repository,
            "ref": self.ref,
            "sourceSha": self.source_sha,
            "useAi": self.use_ai,
            "state": self.state,
            "stage": self.stage,
            "result": self.result,
            "sourceInfo": self.source_info,
            "error": self.error,
            "deploymentDossier": self.deployment_dossier,
        }


def create_app(
    *,
    env_file: Path | None = None,
    artifact_root: Path = Path("artifacts/review-demo"),
    executable: str = "opencode",
    model_config: ModelConfig | None = None,
) -> FastAPI:
    config = model_config or ModelConfig.from_env(env_file if env_file and env_file.is_file() else None)
    jobs: dict[str, ReviewJob] = {}
    tasks: set[asyncio.Task] = set()
    slot = asyncio.Semaphore(1)
    artifact_root = artifact_root.resolve()
    model_available = bool(config.api_key or config.server_url)

    def load_evidence(job: ReviewJob) -> None:
        for path in (
            artifact_root / job.id / "analysis/evidence.jsonl",
            artifact_root / job.id / "readiness/readiness-context/evidence.jsonl",
        ):
            if path.is_file():
                for line in path.read_text().splitlines():
                    item = json.loads(line)
                    job.evidence[item["evidenceId"]] = item

    def persist(job: ReviewJob) -> None:
        path = artifact_root / job.id / "review-job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({**job.public(), "planningRequest": job.planning_request}, ensure_ascii=False)
        )

    def persist_finished(job: ReviewJob) -> None:
        try:
            persist(job)
        except OSError:
            job.state = "failed"
            job.error = {"code": "REVIEW_IO_FAILED", "message": "분석 기록을 저장하지 못했습니다."}

    if artifact_root.exists():
        for path in sorted(artifact_root.glob("*/review-job.json"), key=lambda p: p.stat().st_mtime)[-24:]:
            try:
                data = json.loads(path.read_text())
                if not re.fullmatch(r"[a-f0-9]{24}", data["id"]) or path.parent.name != data["id"]:
                    continue
                restored = ReviewJob(data["id"], data["repositoryUrl"], data.get("ref"), data["useAi"])
                for attr, key in {
                    "source_sha": "sourceSha",
                    "repository": "repository",
                    "result": "result",
                    "source_info": "sourceInfo",
                    "error": "error",
                    "deployment_dossier": "deploymentDossier",
                    "planning_request": "planningRequest",
                    "state": "state",
                    "stage": "stage",
                }.items():
                    setattr(restored, attr, data.get(key))
                if restored.state not in {"succeeded", "failed"}:
                    restored.state = "failed"
                    restored.error = {
                        "code": "SERVER_RESTARTED",
                        "message": "서버 재시작 전 분석이 완료되지 않았습니다.",
                    }
                load_evidence(restored)
                jobs[restored.id] = restored
            except (OSError, ValueError, KeyError):
                continue

    @asynccontextmanager
    async def lifespan(app):
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Iris repository review demo", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.url.path.startswith("/api/") and origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"error": "이 로컬 화면에서 요청해 주세요."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    async def home():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    async def get_config():
        return {
            "modelAvailable": model_available,
            "model": config.model,
            "provider": config.provider,
            "supported": ["Node.js", "Vite", "Express", "Docker", "Compose"],
            "planningAdapters": ["aws_eks", "existing_kubernetes"],
        }

    async def plan_job(job):
        directory = artifact_root / job.id
        job.stage = "checking"
        readiness = await asyncio.to_thread(
            prepare_readiness,
            directory / "source",
            out=directory / "readiness",
            analysis=job.result["analysisResult"],
        )
        job.stage = "planning"
        dossier, _ = await plan_async(
            job.result["analysisResult"],
            readiness,
            job.planning_request,
            config=config if job.use_ai else None,
            ledger=artifact_root.parent / "model-budget-ledger.json",
            executable=executable,
            out=directory / "plans",
        )
        job.deployment_dossier = dossier
        load_evidence(job)

    async def run_job(job: ReviewJob) -> None:
        async with slot:
            try:
                job.state = "running"
                job.stage = "resolving"
                source = parse_github_url(job.repository_url)
                job.repository = source.name
                job.ref, job.source_sha = await resolve_revision(source, job.ref)
                directory = artifact_root / job.id
                directory.mkdir(parents=True, exist_ok=True)
                archive = directory / "source.tar.gz"
                source_root = directory / "source"
                job.stage = "downloading"
                await download_archive(source, job.source_sha, archive)
                job.source_info = await asyncio.to_thread(unpack_source, archive, source_root)
                archive.unlink()
                factory = None
                if job.use_ai:
                    factory = create_live_runner_factory(
                        config,
                        executable=executable,
                        budget_ledger=artifact_root.parent / "model-budget-ledger.json",
                        max_cost_usd=1.0,
                    )
                client = LocalAnalysisClient(runner_factory=factory)

                async def progress(event):
                    job.stage = event.stage

                outcome = await client.analyze_repository(
                    source_root, out=directory / "analysis", on_progress=progress
                )
                job.result = outcome.to_response_data()
                evidence_path = directory / "analysis" / "evidence.jsonl"
                job.evidence = {
                    item["evidenceId"]: item
                    for line in evidence_path.read_text().splitlines()
                    if (item := json.loads(line))
                }
                await plan_job(job)
                job.stage = "finished"
                job.state = "succeeded"
            except asyncio.CancelledError:
                job.state = "failed"
                job.error = {"code": "ANALYSIS_CANCELLED", "message": "분석이 취소되었습니다."}
                raise
            except (AnalyzerError, AnalysisClientError) as error:
                job.state = "failed"
                job.error = {
                    "code": error.code,
                    "message": str(error)
                    if isinstance(error, AnalyzerError)
                    else "분석을 완료하지 못했습니다. 오류 코드를 확인해 주세요.",
                }
            except Exception:
                # Do not expose provider bodies, credentials or local paths.
                job.state = "failed"
                job.error = {
                    "code": "REVIEW_FAILED",
                    "message": "분석 중 오류가 발생했습니다. 서버 로그와 산출물을 확인해 주세요.",
                }
            finally:
                persist_finished(job)

    @app.post("/api/reviews", status_code=202)
    async def create_review(payload: ReviewRequest):
        try:
            parse_github_url(payload.repository_url)
        except (AnalyzerError, ValueError) as error:
            raise HTTPException(422, detail="올바른 GitHub 저장소 링크를 입력해 주세요.") from error
        if payload.use_ai and not model_available:
            raise HTTPException(422, detail="서버의 HIVE_AI 키를 설정하거나 정적 분석을 선택해 주세요.")
        if payload.planning_request is not None:
            try:
                validate_planning_request(payload.planning_request)
            except (AnalyzerError, ValueError):
                raise HTTPException(422, detail="배포 조건이 버전별 입력 계약과 일치하지 않습니다.") from None
        if sum(job.state in {"queued", "running"} for job in jobs.values()) >= 3:
            raise HTTPException(429, detail="진행 중인 분석이 많습니다. 잠시 후 다시 시도해 주세요.")
        if len(jobs) >= 24:
            raise HTTPException(
                429, detail="테스트 세션의 분석 기록 한도에 도달했습니다. 서버를 재시작해 주세요."
            )
        job = ReviewJob(secrets.token_hex(12), payload.repository_url.strip(), payload.ref, payload.use_ai)
        job.planning_request = payload.planning_request
        jobs[job.id] = job
        task = asyncio.create_task(run_job(job))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return job.public()

    @app.post("/api/reviews/{job_id}/plan", status_code=202)
    async def replan(job_id: str, payload: ReplanRequest):
        job = jobs.get(job_id)
        if job is None or job.result is None:
            raise HTTPException(404, detail="분석 결과가 없습니다.")
        if job.state in {"queued", "running"}:
            raise HTTPException(409, detail="진행 중인 분석이 끝난 뒤 요청해 주세요.")
        if sum(j.state in {"queued", "running"} for j in jobs.values()) >= 3:
            raise HTTPException(429, detail="진행 중인 분석이 많습니다. 잠시 후 다시 시도해 주세요.")
        if payload.planning_request is not None:
            try:
                validate_planning_request(payload.planning_request)
            except (AnalyzerError, ValueError):
                raise HTTPException(422, detail="배포 조건이 버전별 입력 계약과 일치하지 않습니다.") from None
        use_ai = job.use_ai if payload.use_ai is None else payload.use_ai
        if use_ai and not model_available:
            raise HTTPException(422, detail="AI 키가 필요합니다.")
        job.planning_request, job.use_ai = payload.planning_request, use_ai
        job.state, job.stage, job.error = "queued", "planning", None

        async def run():
            async with slot:
                try:
                    job.state = "running"
                    await plan_job(job)
                    job.state, job.stage = "succeeded", "finished"
                except asyncio.CancelledError:
                    job.state = "failed"
                    job.error = {"code": "PLANNING_CANCELLED", "message": "배포 계획 생성이 취소되었습니다."}
                    raise
                except Exception as error:
                    job.state = "failed"
                    job.error = {
                        "code": error.code if isinstance(error, AnalyzerError) else "PLANNING_FAILED",
                        "message": "배포 계획을 완료하지 못했습니다. 분석 결과는 유지됩니다.",
                    }
                finally:
                    persist_finished(job)

        task = asyncio.create_task(run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return job.public()

    @app.get("/api/reviews/{job_id}")
    async def get_review(job_id: str):
        if job_id not in jobs:
            raise HTTPException(404, detail="분석을 찾지 못했습니다.")
        return jobs[job_id].public()

    @app.get("/api/reviews/{job_id}/plan")
    async def get_plan(job_id: str):
        job = jobs.get(job_id)
        if job is None or job.deployment_dossier is None:
            raise HTTPException(404, detail="배포 계획이 없습니다.")
        return JSONResponse(
            job.deployment_dossier,
            headers={"Content-Disposition": 'attachment; filename="iris-deployment-dossier.json"'},
        )

    @app.get("/api/reviews/{job_id}/result")
    async def download_result(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, detail="분석을 찾지 못했습니다.")
        if job.result is None:
            raise HTTPException(409, detail="완료된 분석 결과가 없습니다.")
        return JSONResponse(
            {
                "repository": job.repository,
                "sourceSha": job.source_sha,
                **job.result,
                "deploymentDossier": job.deployment_dossier,
            },
            headers={"Content-Disposition": 'attachment; filename="iris-review.json"'},
        )

    @app.get("/api/reviews/{job_id}/evidence/{evidence_id}")
    async def get_evidence(job_id: str, evidence_id: str):
        job = jobs.get(job_id)
        if job is None or evidence_id not in job.evidence:
            raise HTTPException(404, detail="근거를 찾지 못했습니다.")
        return job.evidence[evidence_id]

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--opencode-executable", default="opencode")
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/review-demo"))
    args = parser.parse_args()
    uvicorn.run(
        create_app(env_file=args.env_file, artifact_root=args.artifacts, executable=args.opencode_executable),
        host="127.0.0.1",
        port=args.port,
    )


if __name__ == "__main__":
    main()
