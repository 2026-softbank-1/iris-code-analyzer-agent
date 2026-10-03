# Analysis Gate 실제 레포 검증 · 2026-10-04

분석기 브랜치 `feat/analysis-gate`(기능 커밋 `bad4914`)의 `python -m iris_analyzer.gate.cli --request-stdin`을 로컬 체크아웃에 실행했다. 요청은 `rootDirectory="."`, `sourceSha`=각 레포 HEAD, `ai=false`. 모델·네트워크 호출 0회. `durationMs`는 응답의 분석 시간, wall은 인터프리터 기동을 포함한 subprocess 전체 시간이다(macOS arm64, Python 3.13, 첫 실행은 파일 캐시 미적중).

| 레포 | 소스 커밋 | mode | decision / complexity | 사유 코드 | 결과 | durationMs / wall |
| --- | --- | --- | --- | --- | --- | --- |
| iris-multi-image-shop | `6836ce3da305` | auto | analyze / complex | multiple_dockerfiles, compose_multi_build, has_image_dependencies | units: `web`(web/Dockerfile, 80, web, dependsOn api), `api`(api/Dockerfile, 3000, api, postgres·redis), `worker`(worker/Dockerfile, 3001, worker, public=false, postgres·redis). deps: postgres `postgres:16-alpine`, redis `redis:7-alpine`. env: api·worker `DATABASE_URL`·`REDIS_URL` required, web `VITE_API_BASE_URL`(build) | 83 / 267 |
| Temp_log | `54fa8072d9fb` | auto | analyze / complex | multiple_dockerfiles(Dockerfile, Dockerfile.mongo), compose_multi_build(app, mongo), workspace_multi_app(client, server) | unit `app`(., Dockerfile, 4000). `mongo`는 Dockerfile.mongo가 `FROM mongo:8.0.32`라 unit이 아닌 dependency(mongodb) + 질문 `dependency_built_from_dockerfile` | 18 / 186 |
| portpolio-production | `f54230346fbf` | auto | skip / simple | single_railpack_app | simpleBuild `railpack`. `ops/compose.yaml`(nginx 이미지)·`ops/compose.tunnel.yaml`(cloudflared)은 이미지 전용이라 판정에 영향 없음, `tests/`·`docs/` 제외 | 9 / 117 |
| portfolio-production-test | `48f4ffdc3c68` | auto | skip / simple | single_railpack_app | simpleBuild `railpack` | 0 / 109 |
| iris-was (gate-wt/was) | `56feea328e13` | auto | skip / simple | single_dockerfile, has_image_dependencies | simpleBuild `dockerfile`/`Dockerfile`. `docker-compose.dev.yml`의 postgres는 이미지 의존성 정보로만 기록 | 4 / 104 |
| iris-web (gate-wt/web) | `91b700dde778` | auto | skip / simple | single_railpack_app | simpleBuild `railpack` | 5 / 108 |

`mode=force` 재실행: 모든 레포가 `decision=analyze`, 사유 끝에 `forced`, complexity는 트리아지 값 유지. 단순 레포는 루트 unit 1개(`app`)를 낸다 — iris-was `app`(Dockerfile, 8000, api, dependsOn postgres `postgres:18`), iris-web·portfolio 두 레포 `app`(railpack, web, 포트 미확인 → `port_unknown`). 복합 레포 결과는 auto와 같다. 분석 시간 0~9 ms, wall 107~172 ms.

모든 응답은 `iris.analysis-gate.v1` 스키마 검증을 통과했다(CLI가 반환 전에 검증).

## 규모

- 21만여 파일이 있는 상위 작업 디렉터리 전체(여러 레포·node_modules 포함)에 실행: 제외 디렉터리 가지치기 후 분석 1,578 ms, wall 1.93 s. 결과는 analyze(멀티 레포라 당연).
- 분석기 레포 자체: skip / single_railpack_app(`fixtures/`·`tests/` 제외 확인), wall 0.14 s.

## 배포 산출물

- wheel: `dist/iris_analyzer-0.1.0-py3-none-any.whl` (커밋 `7d07215`에서 `uv build --wheel`)
- sha256: `07752199ca3420031e977e680e5bd9f66f2a58b49ffa2be9449e45356eb1308e`
- 깨끗한 Python 3.11 venv에 wheel만 설치해 `python -m iris_analyzer.gate.cli`와 `iris-analysis-gate`로 iris-multi-image-shop 결과(위 표와 동일)와 잘못된 요청 exit 2를 확인했다.

## 테스트

- `uv run pytest tests/test_analysis_gate.py -q` → 29 passed
- `uv run pytest -q` → 783 passed, 8 skipped (기능 추가 전 754 passed, 8 skipped. 증가분 29개는 신규 테스트)
- `uv run ruff check .` → All checks passed (레포에 mypy 설정 없음. 참고로 `uvx mypy src/iris_analyzer/gate --ignore-missing-imports`에서 gate 패키지 오류 0건)

## 관찰과 한계

- Temp_log는 루트 Dockerfile 하나가 client·server를 함께 빌드하지만, 계약 규칙(같은 디렉터리 변형 Dockerfile도 단위 후보로 계산, 워크스페이스 앱 2+)에 따라 complex로 판정된다. 실제 추출 unit은 `app` 1개 + mongodb 의존성이라 웹에서 1개 서비스 생성으로 이어진다.
- Railpack 정적 사이트(Vite)의 포트는 빌더가 정하므로 force 결과에서 `port_unknown`으로 남는다. `preview`/`dev` 스크립트의 개발 포트는 런타임 포트로 쓰지 않는다.
- Compose `profiles`/`extends`/`include`, `env_file` 내용, 동적 포트는 해석하지 않는다.
