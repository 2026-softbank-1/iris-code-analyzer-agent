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

## Phase 2 재검증 · env 바인딩 / 호스트 별칭 / DB 접속 정보 (2026-10-04)

기능 커밋 `f6ff307`의 wheel을 깨끗한 venv에 설치해 같은 요청(`rootDirectory="."`, auto)으로 재실행했다. 두 레포 모두 decision/complexity/사유 코드/unit 목록/`dependsOn`은 Phase 1 결과와 같고, 추가 필드만 늘었다. durationMs 12 / 16.

iris-multi-image-shop (`6836ce3da305`):

| unit | env binding | hostAliases (host:port → target, 증거) |
| --- | --- | --- |
| web | 없음(`VITE_API_BASE_URL`은 build arg → null) | `api:3000` → `api` (`web/nginx/default.conf:14` proxy_pass) |
| api | `DATABASE_URL` → dependency `postgres` url, `REDIS_URL` → dependency `redis` url | `postgres:5432` → postgres (`compose.yaml:31`), `redis:6379` → redis (`compose.yaml:32`) |
| worker | `DATABASE_URL` → postgres url, `REDIS_URL` → redis url | `postgres:5432` (`compose.yaml:57`), `redis:6379` (`compose.yaml:58`) |

dependencies: `postgres` = `{port:5432, database:"iris_shop", user:"iris_demo", passwordInSource:true}`(compose에 `${POSTGRES_PASSWORD:-…}` 기본값이 하드코딩돼 있음, 값은 출력되지 않음), `redis` = `{port:6379, database:null, user:null, passwordInSource:false}`.

Temp_log (`54fa8072d9fb`): unit `app`의 `MONGO_URI` → dependency `mongo` url, hostAliases `mongo:27017` → `mongo`(`compose.yaml:20`). dependency `mongo` = `{port:27017, database:"archlog", user:"root", passwordInSource:false}`(`MONGO_INITDB_ROOT_PASSWORD`는 `${…:?}`라 소스에 값 없음; `database`는 `MONGO_INITDB_DATABASE`가 없어 URI 경로에서 가져옴).

두 결과 JSON에서 compose 하드코딩 기본 비밀번호(`iris_demo_local`)는 나타나지 않는다(사용자명 `iris_demo`, 키 이름 `MONGO_APP_PASSWORD`만 존재).

- wheel: `dist/iris_analyzer-0.1.0-py3-none-any.whl` (커밋 `f6ff307`에서 `uv build --wheel`)
- sha256: `c3be0a5ef1ea8e11c67f863a0b309b8557ab03ef51cb518504daee3c441ec0e1`
- 테스트: `uv run pytest -q` → 790 passed, 8 skipped (Phase 1 783 → +7 신규, fixture `fixtures/gate-links`). `uv run ruff check .` → All checks passed. 기존 테스트 중 필드 완전 일치를 단언하던 2건(`env` 행, `dependencies` 행)은 새 필드를 포함하도록 기대값만 갱신했다.

## 계약 F 검증 · DB 초기화 스크립트 `initScripts` (2026-10-04)

기능 커밋 `5922112`로 같은 요청(`rootDirectory="."`, auto)을 재실행했다. decision/complexity/units는 이전 결과와 같고 `dependencies[].initScripts`만 추가됐다. 내용은 출력되지 않고 경로·종류·해시·크기·순서·지원 여부만 나온다.

| 레포 | dependency | initScripts | 질문 |
| --- | --- | --- | --- |
| iris-multi-image-shop (`6836ce3da305`) | postgres (compose `./db/schema.sql:/docker-entrypoint-initdb.d/001-schema.sql`, `./db/seed.sql:/docker-entrypoint-initdb.d/002-seed.sql`) | `db/schema.sql` (sql, 1827 B, order 0, supported), `db/seed.sql` (sql, 595 B, order 1, supported) | 없음 |
| iris-multi-image-shop | redis | 필드 없음(해당 없음) | 없음 |
| Temp_log (`54fa8072d9fb`) | mongo (Dockerfile.mongo 빌드 DB, `./docker/mongo-init.js:/docker-entrypoint-initdb.d/init.js`) | `docker/mongo-init.js` (js, 223 B, order 0, supported) | 기존 `dependency_built_from_dockerfile`만 |

sha256: schema.sql `10c3dca9…1cd3`, seed.sql `d3195f26…a35f`, mongo-init.js `3132a18c…f993e`. `mongo-health.js`는 `/opt/health.js`로 마운트되어 대상이 아니다. analysis durationMs 21 / 89.

- wheel: `dist/iris_analyzer-0.1.0-py3-none-any.whl` (커밋 `5922112`에서 `uv build --wheel`)
- sha256: `675acf6e3cda4e9acddb42878fa6f84e61d3e90d5051ff8d68a60793f64dc888`
- 테스트: `uv run pytest -q` → 800 passed, 8 skipped (Phase 2 790 → +10: 디렉터리 마운트, 파일 마운트, 긴 문법+gz, 심볼릭 링크·레포 밖·절대 경로 무시, 요청 root 밖, `.sh` unsupported, 파일 oversize, 합계 oversize, mongo `.js`/redis 없음, Dockerfile 빌드 DB). `uv run ruff check .` → All checks passed.

## 리뷰 P1 수정 검증 · `build.target` / URL path·query 보존 (2026-10-04)

기능 커밋 `f6679dd`(브랜치 `fix/gate-build-target-url-suffix`)의 wheel을 깨끗한 venv에 설치해 같은 요청(`rootDirectory="."`, auto)으로 재실행했다. decision/complexity/사유 코드/unit·dependency 목록과 포트·role은 이전 결과와 같고 추가 필드만 늘었다. 응답 JSON에 compose 하드코딩 비밀번호(`iris_demo_local`)는 나타나지 않는다.

| 레포 | 소스 커밋 | 결과 |
| --- | --- | --- |
| iris-multi-image-shop | `6836ce3da305` | compose가 이미 `target: runtime`을 쓴다. `api` 3000/api, `web` 80/web, `worker` 3001/worker로 이전과 동일(`buildTarget:"runtime"`). `web`은 `build.args`가 있어 `buildArgs:["VITE_API_BASE_URL"]` + 질문 `build_args_present`(web). api·worker `DATABASE_URL` → postgres url `{scheme:"postgresql", urlSuffix:"/iris_shop", hasCredentials:true}`, `REDIS_URL` → redis url `{scheme:"redis", urlSuffix:"", hasCredentials:false}`. durationMs 12 |
| Temp_log | `54fa8072d9fb` | unit `app` 4000(`buildTarget:null`, `buildArgs:[]`). `MONGO_URI` → mongo url `{scheme:"mongodb", urlSuffix:"/archlog?authSource=archlog", hasCredentials:true}` — 수정 전에는 `/archlog?authSource=…`가 결과에서 사라졌다. durationMs 11 |

- wheel: `dist/iris_analyzer-0.1.0-py3-none-any.whl` (커밋 `f6679dd`에서 `uv build --wheel`)
- sha256: `ed93a0a95488f31c8f526c1f2d1f1de9e08a8f7cfd5c3a707246d2c5a3abfc40`
- 테스트: `uv run pytest -q` → 806 passed, 8 skipped (이전 800 → +6: 다단계 Dockerfile target api/web과 상속 EXPOSE, 파생 stage EXPOSE 우선·기본 마지막 stage, target 없음, build args 키만, URL path/query/fragment 보존 + DB `sslmode` + 자격 증명 미노출 + `mongodb+srv`·비밀 쿼리·미해석 보간 null). 기존 2건은 binding에 새 필드가 생겨 기대값만 갱신했다. `uv run ruff check .` → All checks passed.
