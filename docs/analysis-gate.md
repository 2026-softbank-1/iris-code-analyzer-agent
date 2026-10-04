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
- Phase 2 추가 필드(스키마에서는 선택이지만 이 구현은 항상 출력): `units[].env[].binding`, `units[].buildTarget`, `units[].buildArgs`, `units[].hostAliases[]`, `dependencies[].port/database/user/passwordInSource`. 아래 "프로젝트 내부 통신" 참조.
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
- **`build.target`**: Compose `build.target`이 있으면 Dockerfile의 해당 stage를 기준으로 EXPOSE·`ENV PORT`·CMD/ENTRYPOINT·베이스 이미지(role 판정의 nginx→web 등)를 계산한다. target 이름은 대소문자를 구분하지 않고 `FROM x AS name`으로 찾는다. stage가 `FROM <앞선 stage>`이면 Docker처럼 그 stage를 상속한다(EXPOSE는 누적하되 더 파생된 stage의 값을 우선, `ENV PORT`·CMD는 파생 stage가 덮어씀, ENTRYPOINT를 새로 지정하면 상속된 CMD는 초기화). target이 없으면 마지막 stage. 이 값은 `units[].buildTarget`(문자열 또는 `null`, 선택 필드)으로 낸다. target stage가 Dockerfile에 없으면 `port`는 `null`로 두고 소스 포트 추정도 하지 않으며 `build_target_not_found` 질문을 남긴다.
- **`build.args`**: 키만 `units[].buildArgs`(정렬된 문자열 배열, 선택 필드)로 내고 값은 어디에도 출력하지 않는다. WAS가 아직 빌드 인자를 전달하지 못하므로 unit마다 `build_args_present` 질문을 남긴다. (기존대로 `env[]`에는 stage=`build` 키로도 나온다.)
- `startCommand`는 Compose `entrypoint`/`command` 또는 Procfile 명령일 때만 채운다. `buildCommand`는 채우지 않는다(빌더가 결정).

## 프로젝트 내부 통신 (Phase 2 추가 필드)

`iris.analysis-gate.v1`에 선택 필드만 추가했다(구버전 결과도 스키마 통과). 값은 절대 출력하지 않고 id·포트·속성명·`path:line`만 낸다.

- `units[].env[].binding`: `{"kind":"dependency","targetId":"postgres","property":"url|host|port|user|password|database"}`, `{"kind":"unit","targetId":"api","property":"url|host|port"}` 또는 `null`(확인 못 함). runtime 변수만 대상이다(build arg는 `null`).
  - 1순위 Compose `environment` 값: 전체가 URL이고 호스트가 Compose 서비스명이면 `url`(`postgres://…@postgres:5432/app` → postgres url, `http://api:3000` → unit api url). 값이 서비스명이면 `*_HOST`류 키는 `host`. 같은 접두어의 `*_HOST`가 바인딩돼 있으면 `*_PORT/*_USER/*_PASSWORD/*_DB`는 `port/user/password/database`(unit 대상은 `port`만).
  - 2순위 키 이름: `DATABASE_URL/DB_URL`(unit이 의존하는 postgres, 없으면 mysql), `POSTGRES_URL`, `MONGO_URL/MONGODB_URI/MONGO_URI`, `REDIS_URL`, `MYSQL_URL`. 해당 엔진 의존성이 후보 1개로 정해질 때만 연결한다. Compose 값이 외부 호스트 URL이면 키 이름이 같아도 `null`로 둔다.
  - URL 바인딩(`property:"url"`)은 Compose에 적힌 URL에서 만든 경우 추가로 `scheme`(적힌 그대로: `http`, `postgresql`, `postgres+asyncpg`, `redis`, `mongodb` …), `urlSuffix`(host:port 뒤의 path+query+fragment 원문, 없으면 `""`; 예 `/api/v1?tenant=demo`, `/app?sslmode=disable`), `hasCredentials`(userinfo가 있었는지)를 낸다. 플랫폼은 대상의 host:port만 바꾸고 `scheme://` + host:port + `urlSuffix`로 URL을 다시 만든다. **userinfo(사용자·비밀번호)는 절대 출력하지 않는다.** URL을 안전하게 줄일 수 없으면 binding을 `null`로 둔다: `mongodb+srv` 등 `+srv` 스킴, host 뒤에 해석할 수 없는 `${VAR}` 보간(`/${PATH}`), 비밀로 보이는 쿼리 파라미터(`password|passwd|pwd|secret|token|credential|api_key|access_key|sig`가 이름에 들어간 것, 예 `?token=…`). 키 이름 휴리스틱으로만 연결한 binding(적힌 URL 없음)에는 이 세 필드가 없다.
- `units[].hostAliases[]`: `{"host","port","targetId","evidence":[{path,line}]}`. 다른 unit/dependency를 Compose 서비스명(또는 unit/dependency id)으로 부르는 곳을 모은다. `host`가 Compose에서 쓰는 이름, `targetId`는 결과의 unit/dependency id, `port`는 URL에 적힌 포트(없으면 대상의 포트, 모르면 `null`). 근거:
  - Compose `environment` 값(URL 호스트, 값 전체가 서비스명, `host:port`) — 증거는 compose 파일의 해당 키 줄.
  - nginx 계열 `*.conf`/`nginx*` 파일의 `proxy_pass`/`fastcgi_pass`/`grpc_pass`/`uwsgi_pass`와 `upstream { server host:port }`(상한 40개 파일; unit 루트가 가장 깊은 소유 unit에 귀속).
  - 소스 문자열 리터럴 `"http://api:3000"`, `'redis://redis:6379'` 등(unit 소유 파일, 깊이 5 이하, 파일 120개 상한).
  - `localhost`·외부 도메인·알 수 없는 호스트는 무시한다. 별칭 대상은 `dependsOn`에도 추가된다.
- `dependencies[]`: `port`(Compose `ports/expose`의 컨테이너 포트, 없으면 엔진 기본 5432/6379/3306/27017, `other`는 `null`), `database`(`POSTGRES_DB`/`MYSQL_DATABASE`/`MONGO_INITDB_DATABASE`, 없으면 이 DB를 가리키는 URL의 경로), `user`(`POSTGRES_USER`/`MYSQL_USER`/`MONGO_INITDB_ROOT_USERNAME`, 없으면 URL의 사용자, 없으면 postgres=`postgres`·mysql=`root`·그 외 `null`), `passwordInSource`(Compose에 비밀번호가 하드코딩됐거나 `${VAR:-기본값}` 기본값·URL 리터럴 비밀번호가 있으면 `true`. 값은 출력하지 않는다).

## DB 초기화 스크립트 (계약 F)

`dependencies[].initScripts[]`(선택 필드, 있을 때만 출력)는 Compose DB 서비스의 volumes 중 컨테이너 경로가 `/docker-entrypoint-initdb.d`(디렉터리) 또는 그 바로 아래 파일인 bind mount를 해석한 결과다. 공식 postgres/mysql/mongo 이미지는 데이터 디렉터리가 비어 있을 때 한 번만 이 파일들을 실행한다.

```json
{"path": "db/schema.sql", "kind": "sql", "sha256": "…", "size": 1827, "order": 0, "supported": true}
```

- 마운트 형태: 짧은 문법(`./db:/docker-entrypoint-initdb.d:ro`, `./db/a.sql:/docker-entrypoint-initdb.d/001-a.sql`)과 긴 문법(`type: bind`, `source`, `target`). 상대 경로는 해당 compose 파일 위치 기준. 디렉터리 마운트는 비재귀로 정규 파일만 나열하고(숨김 파일 제외), 파일 마운트는 컨테이너 쪽 파일명을 이름으로 쓴다. 같은 이름이 겹치면 나중 마운트가 이긴다.
- `path`는 레포 루트 기준 POSIX 경로, `order`는 컨테이너 파일명 정렬(이미지 실행 순서와 동일) 기준 0부터. `sha256`은 내용 해시(64 MiB 초과 시 `null`). **내용은 출력하지 않는다.**
- 무시: 심볼릭 링크(경로 구성요소 포함), 레포 밖·요청 `rootDirectory` 밖 경로, 절대/`~`/변수 경로, 명명 볼륨, 정규 파일이 아닌 것, 엔진이 실행하지 않는 확장자.
- 엔진별 확장자: postgres/mysql `.sql`, `.sql.gz`, `.sh`; mongodb `.js`, `.sh`; redis·other는 해당 없음(필드 생략). 최대 20개.
- `supported:false` + 질문: `.sh`는 `init_script_unsupported`(플랫폼이 실행하지 않음). 파일당 1 MiB 초과는 그 파일만, 지원 파일 합계가 1 MiB를 넘으면 해당 DB의 지원 스크립트 전부 `init_script_too_large`.
- `analyze`일 때만 출력한다.

## 질문 코드

`port_unknown`, `build_target_not_found`, `build_args_present`, `dockerfile_missing`, `dependency_built_from_dockerfile`, `image_service_ignored`, `compose_build_unsupported`(원격/동적 context, `dockerfile_inline`), `unit_outside_scope`, `compose_variant`, `compose_invalid`, `no_builder_signal`, `scan_truncated`, `file_too_large`, `init_script_unsupported`, `init_script_too_large`, `ai_not_configured`. skip 응답에는 `ai_not_configured`·`scan_truncated`만 남긴다.

## WAS 연동 메모

- 설치: 이 레포 고정 커밋에서 `uv build`로 만든 wheel을 WAS `vendor/`에 두고 sha256을 manifest에 기록한다. 기존 의존성(PyYAML, jsonschema 등) 외 추가 의존성은 없다.
- 호출: `[sys.executable, "-m", "iris_analyzer.gate.cli", "--request-stdin"]`. 실측 분석 수 ms(인터프리터 기동 포함 약 0.1초), 21만 파일 디렉터리(제외 디렉터리 가지치기 후)에서 약 1.6초.
- 검증 결과: [reports/analysis-gate-validation.md](../reports/analysis-gate-validation.md)

## 한계

- 정적 규칙 기반이라 동적 포트·런타임 분기·Compose `profiles`/`extends`/`include`, `env_file` 내용은 해석하지 않는다.
- 여러 compose 파일에 같은 서비스가 있으면 루트의 표준 파일 정의를 우선하고 `compose_variant` 질문을 남긴다(override 병합 없음).
- `multi_language_roots`는 디렉터리 수 기준이라 루트 앱 + 하위 보조 패키지 구성에서 analyze로 기울 수 있다(안전한 방향: 사용자가 단위를 골라 생성).
- Railpack 정적 사이트(Vite 등)의 수신 포트는 빌더가 정하므로 `port_unknown`으로 남는다.
