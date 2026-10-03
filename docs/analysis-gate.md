# Analysis Gate — 단순 레포는 분석 생략, 복합 레포만 단위 분석

담당 경계는 **고정 SHA 소스 → 결정적 트리아지 → (복합이면) 배포 단위·의존성·환경변수·포트 정적 추출**이다. LLM·네트워크 호출이 없고 대상 저장소의 코드·스크립트를 설치·빌드·실행하지 않는다. 서비스 생성·빌드·배포는 iris-was(API·Build Worker)의 책임이다.

```text
WAS Build Worker: 고정 SHA 소스 확보
  → iris-analysis-gate (stdin JSON → stdout JSON)
      ├─ decision=skip     Dockerfile 1개 또는 Railpack 앱 1개 → 기존 단일 서비스 경로 그대로
      └─ decision=analyze  멀티 이미지/복합 → units·dependencies·questions → 웹에서 검토 후 apply
```

## 실행

```sh
echo '{"schemaVersion":"iris.analysis-gate-request.v1","sourceRoot":"/abs/checkout"}' \
  | uv run iris-analysis-gate --request-stdin
# 또는
python -m iris_analyzer.gate.cli --request-stdin
```

- 종료 코드: `0` 정상 응답(skip/analyze/unsupported 모두), `2` 잘못된 요청(`GATE_REQUEST_INVALID`, `GATE_SOURCE_NOT_FOUND`, `GATE_ROOT_DIRECTORY_NOT_FOUND`), `1` 내부 오류(`GATE_FAILED`, `GATE_RESULT_INVALID`).
- 오류는 stderr에 `{"error":{"code","message"}}` 한 줄만 쓴다. 경로·파일 내용·환경변수 값은 담지 않는다. 실패 시 stdout은 비어 있다.
- 요청은 64 KiB 이하. 라이브러리 호출은 `iris_analyzer.gate.run_gate(document) -> dict`.
- 스키마: [요청](../src/iris_analyzer/schemas/analysis-gate-request.schema.json), [응답](../src/iris_analyzer/schemas/analysis-gate.schema.json). 응답은 반환 전에 스키마로 검증한다.

## 요청 `iris.analysis-gate-request.v1`

```json
{
  "schemaVersion": "iris.analysis-gate-request.v1",
  "sourceRoot": "/worker/checkout",
  "rootDirectory": ".",
  "sourceSha": "0123456789abcdef0123456789abcdef01234567",
  "mode": "auto",
  "ai": false
}
```

| 필드 | 규칙 |
| --- | --- |
| `schemaVersion`, `sourceRoot` | 필수. `sourceRoot`는 존재하는 디렉터리의 절대 경로 |
| `rootDirectory` | 기본 `"."`. POSIX 상대 경로, `..`·절대 경로·역슬래시·제어문자 거절. 경로 구성요소에 심볼릭 링크가 있으면 거절. `./a/`는 `a`로 정규화 |
| `sourceSha` | 40자 소문자 hex 또는 `null`(기본). 응답에 그대로 돌려준다 |
| `mode` | `auto`(기본) 또는 `force`. `force`는 트리아지와 무관하게 units를 채우고 reasons에 `forced`를 더한다(complexity는 트리아지 값 유지) |
| `ai` | 기본 `false`. `true`여도 정적 분석만 하고 `questions`에 `ai_not_configured`를 남긴다 |

알 수 없는 필드는 거절한다.

## 응답 `iris.analysis-gate.v1`

필드 구성은 설계 문서(DESIGN-analysis-gate.md)의 계약 1과 같다. 요약:

- `decision`: `skip | analyze`. `complexity`: `simple | complex | unsupported`(unsupported도 decision은 analyze).
- `simpleBuild`: skip일 때만 `{builder, dockerfilePath}`, 아니면 `null`. `dockerfilePath`는 **요청 rootDirectory 기준**.
- `units`/`dependencies`: analyze일 때만 채운다(skip이면 빈 배열).
- `units[].rootDirectory`는 레포 루트 기준, `units[].dockerfilePath`는 **해당 unit rootDirectory 기준**(WAS `detect_builder`가 root_directory 기준으로 Dockerfile을 찾음). Compose `build.context: .` + `dockerfile: api/Dockerfile`이면 `rootDirectory="."`, `dockerfilePath="api/Dockerfile"`.
- 그 밖의 경로(`signals`, `reasons[].paths`, `evidence`)는 레포 루트 기준 POSIX 상대 경로다.
- `env[]`는 키·단계(`runtime`/`build`)·필수 여부만 담고 **값은 내보내지 않는다**. 필수: 값이 비었거나 `${VAR}`/`${VAR:?}` 보간, 연결 URL 키(`DATABASE_URL`, `REDIS_URL`, `MONGO_URI` …), 비밀 키 이름(`SECRET`, `PASSWORD`, `TOKEN` …).
- `dependencies[].image`는 Compose 이미지 또는 DB Dockerfile의 FROM, 코드 의존성으로만 추론한 경우 `null`.
- `questions[].unitId`는 전역 질문이면 `null`.
- `analysis = {"engine":"static","durationMs":…,"modelCalls":0}`, `executionAuthorized`는 항상 `false`.

## 판정 규칙

스캔 범위는 요청 rootDirectory 하위다. 제외: `.git node_modules vendor dist build .next coverage test tests __tests__ fixtures examples example docs .github venv __pycache__ bower_components`와 숨김 디렉터리. 심볼릭 링크는 따라가지 않고, 항목 6만 개·깊이 10단계를 넘으면 `scan_truncated`, 512 KiB를 넘는 설정 파일은 읽지 않고 `file_too_large` 질문을 남긴다.

복합(analyze) 사유 코드 — 하나라도 있으면 complex:

| 코드 | 조건 |
| --- | --- |
| `multiple_dockerfiles` | 빌드 대상 Dockerfile 2개 이상. `Dockerfile.mongo` 같은 같은 디렉터리 변형도 센다. `Dockerfile.dev/.local/.test/.ci/.debug/.e2e` 등 개발용 변형은 `signals`에만 기록하고 세지 않는다 |
| `compose_multi_build` | `build:`가 있는 Compose 서비스 2개 이상(모든 compose 파일 합산, 서비스 이름 기준 중복 제거, `*.test/.ci/.e2e` 변형 파일 제외) |
| `workspace_multi_app` | npm/yarn `workspaces`, `pnpm-workspace.yaml`, `lerna.json` 멤버 중 실행 가능한 앱 2개 이상(`start` 스크립트, 서버 프레임워크, 또는 Next/Nuxt/Astro/Vite(+index.html) 등 웹 프레임워크 + dev/build). 라이브러리 패키지는 제외 |
| `multi_language_roots` | Dockerfile이 포함(COPY)하지 않는 서로 다른 디렉터리 2곳 이상에 런타임 매니페스트(워크스페이스 멤버 제외) |
| `procfile_multi_process` | 요청 루트 Procfile 프로세스 타입 2개 이상(`release` 제외) |
| `single_unit_in_subdirectory` | 단위가 1개지만 요청 루트가 아닌 하위 디렉터리에 있음 → 루트 디렉터리 확인 필요 |
| `multiple_units` | 위 조건은 없지만 추출 결과 단위가 2개 이상 |
| `dockerfile_missing` | Compose가 가리키는 Dockerfile이 없음 |

단순(skip) 사유: `single_dockerfile`(요청 루트에서 빌드되는 Dockerfile 1개, builder=dockerfile) 또는 `single_railpack_app`(Dockerfile 없음, 루트 매니페스트로 Railpack 빌드). DB/Redis 같은 이미지 전용 Compose 서비스는 단순 판정을 막지 않고 `has_image_dependencies` 정보 항목으로 남긴다. Dockerfile·런타임 매니페스트가 모두 없으면 `unsupported` + `no_builder_signal`. 사용자가 요청하면 `forced`가 추가된다.

Railpack 인식 매니페스트: `package.json`, `requirements.txt`, `pyproject.toml`, `Pipfile`, `setup.py`, `go.mod`, `Gemfile`, `Cargo.toml`, `composer.json`, `pom.xml`, `build.gradle(.kts)`, `mix.exs`, `deno.json(c)`, 요청 루트의 `index.html`/`Staticfile`(정적).

## 단위 추출

기존 전처리 Docker 추출기의 Compose 포트·명령·YAML 위치 해석(`_compose_port`, `_command`, `_yaml_mapping`, `_yaml_lines`)과 `safe_repository_path`를 그대로 재사용한다.

1. **Compose**: build 서비스 → unit(`builder=dockerfile`, context → rootDirectory, dockerfile → dockerfilePath). 이미지 서비스 → dependency(`postgres|redis|mysql|mongodb`, 메시지 큐·검색 등은 `other`). 프록시·터널·관리 도구(cloudflared, traefik, adminer …)는 무시한다. DB를 직접 빌드하는 서비스(`Dockerfile.mongo`, `FROM mongo`, `command: mongod`)는 unit이 아니라 dependency로 두고 `dependency_built_from_dockerfile` 질문을 남긴다.
2. **Compose에 없는 Dockerfile** → 디렉터리 단위 unit(변형은 `<dir>-<variant>` id, 루트는 `app`).
3. **Railpack**: Dockerfile이 포함하지 않는 워크스페이스 앱·매니페스트 디렉터리 → `builder=railpack` unit.
4. **Procfile** 프로세스 2개 이상이면 루트 unit을 프로세스별 unit으로 나누고 `startCommand`에 명령을 넣는다.

- 포트: Compose `ports`(컨테이너 쪽)/`expose`/`environment.PORT` → Dockerfile 최종 stage `EXPOSE`/`ENV PORT`/CMD `--port` → `package.json` `start`/`serve` 스크립트 → 소스 리터럴(`process.env.PORT || 3000`, `.listen(3000)`, `uvicorn.run(port=…)`; 상한 120개 파일). 못 찾으면 `null` + `port_unknown`.
- `dependsOn`: Compose `depends_on`, 환경변수 URL의 호스트명(`@postgres:5432`)과 스킴(`redis://`), unit 매니페스트의 클라이언트 라이브러리(`pg`, `ioredis`, `bullmq`, `mongoose`, `psycopg`, `redis` …). 같은 엔진은 하나의 dependency로 합친다.
- `env`: Compose `environment`(runtime), `build.args`(build), unit 루트의 `.env.example` 계열 키. 실제 `.env`는 읽지 않는다.
- `role`: 이름(worker/queue/consumer/cron → `worker`), 최종 이미지 nginx/caddy/httpd → `web`, 이름(web/frontend/client → `web`, api/server/backend → `api`), 프레임워크 의존성 순. `public`은 worker가 아니면 `true`.
- `startCommand`는 Compose `entrypoint`/`command` 또는 Procfile 명령일 때만 채운다. `buildCommand`는 채우지 않는다(빌더가 결정).

## 질문 코드

`port_unknown`, `dockerfile_missing`, `dependency_built_from_dockerfile`, `image_service_ignored`, `compose_build_unsupported`(원격/동적 context, `dockerfile_inline`), `unit_outside_scope`, `compose_variant`, `compose_invalid`, `no_builder_signal`, `scan_truncated`, `file_too_large`, `ai_not_configured`. skip 응답에는 `ai_not_configured`·`scan_truncated`만 남긴다.

## WAS 연동 메모

- 설치: 이 레포 고정 커밋에서 `uv build`로 만든 wheel을 WAS `vendor/`에 두고 sha256을 manifest에 기록한다. 기존 의존성(PyYAML, jsonschema 등) 외 추가 의존성은 없다.
- 호출: `[sys.executable, "-m", "iris_analyzer.gate.cli", "--request-stdin"]`. 실측 분석 수 ms(인터프리터 기동 포함 약 0.1초), 21만 파일 디렉터리(제외 디렉터리 가지치기 후)에서 약 1.6초.
- 검증 결과: [reports/analysis-gate-validation.md](../reports/analysis-gate-validation.md)

## 한계

- 정적 규칙 기반이라 동적 포트·런타임 분기·Compose `profiles`/`extends`/`include`, `env_file` 내용은 해석하지 않는다.
- 여러 compose 파일에 같은 서비스가 있으면 루트의 표준 파일 정의를 우선하고 `compose_variant` 질문을 남긴다(override 병합 없음).
- `multi_language_roots`는 디렉터리 수 기준이라 루트 앱 + 하위 보조 패키지 구성에서 analyze로 기울 수 있다(안전한 방향: 사용자가 단위를 골라 생성).
- Railpack 정적 사이트(Vite 등)의 수신 포트는 빌더가 정하므로 `port_unknown`으로 남는다.
