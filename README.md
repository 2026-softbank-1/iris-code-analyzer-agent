# Iris Code Analyzer

Iris의 전처리와 코드 분석 worker용 Python 패키지입니다. 저장소를 고정한 스냅샷으로 읽고 배포에 필요한 파일·관측값·줄 단위 근거를 구성한 다음 OpenCode를 통해 Hive 또는 OpenAI 모델에 전달합니다. 결과는 JSON Schema와 원본 근거 및 정적 관측값으로 다시 검증합니다.

Node.js workspace, Vite, Express, Dockerfile, Compose를 지원합니다. 분석 대상의 소스·설정·스크립트를 실행하지 않습니다. FastAPI Job 접수, PostgreSQL 저장, 실제 빌드·배포는 플랫폼 담당 모듈에서 연결합니다.

## 설치

Python 3.11 이상과 OpenCode **1.18.33**을 사용합니다.

```sh
uv sync --extra dev
npm install -g opencode-ai@1.18.33
cp .env.example .env
# .env의 HIVE_AI=xxx를 실제 키로 교체
```

uv 없이 설치하려면 가상환경에서 `pip install -e '.[dev]'`를 사용합니다. `uv.lock`에 Python 의존성 버전을 고정했습니다. OpenCode 실행 파일은 `--opencode-executable /path/to/opencode`로 지정할 수도 있습니다.

Hive 기본 설정은 `hive-ai / zai-org/glm-5.3-flash`입니다. 다른 모델은 `--model deepseek-ai/deepseek-v4.1-flash`로 지정합니다. `.env`의 `HIVE_MODEL`, `HIVE_BASE_URL`, `OPENCODE_PROVIDER`도 지원합니다. `.env`는 Git에서 제외되며 키는 모델 입력·보고서에 넣지 않습니다. 상위 폴더의 키는 `--env-file ../.env`로 사용합니다.

OpenAI는 `OPENAI_API` 또는 표준 이름 `OPENAI_API_KEY`를 읽습니다. 다음처럼 설정하면 기본 공급자가 바뀝니다. 키는 서버에만 두며 프론트에는 전달하지 않습니다.

```dotenv
OPENAI_API=your-key
OPENAI_MODEL=gpt-6-luna
OPENCODE_PROVIDER=openai
```

`OPENCODE_OUTPUT_MODE`를 생략하면 OpenAI는 구조화 응답 도구, Hive는 JSON 텍스트를 사용합니다. GPT-6 Luna 기본 reasoning은 `low`이며 이때 temperature를 보내지 않습니다. 모델 응답의 snapshot/context 및 계획 digest는 요청별 스키마에 고정하고 기존 검증을 유지합니다. 초기 JSON 텍스트 호출의 잘못된 해시를 거절한 기록과 실제 두 프로젝트 검증은 [OpenAI 검증 보고서](reports/openai-validation.md)에 있습니다.

```sh
uv run iris-analyzer analyze --repo ../tested_code/Temp_log \
  --env-file ../.env --provider openai --model gpt-6-luna --out artifacts/openai-analysis
uv run iris-deployment --repo ../tested_code/Temp_log \
  --env-file ../.env --provider openai --model gpt-6-luna --out artifacts/openai-deployment
```

공급자를 명시적으로 바꾸면 다른 공급자의 `OPENCODE_MODEL`, 응답 모드·reasoning·native JSON 설정을 가져오지 않습니다. 비용 상한은 양쪽 공급자가 같은 ledger를 공유합니다. OpenAI 비용은 cache write·reasoning·긴 context 할증까지 반영한 추정값이며 실제 청구는 별도로 확인해야 합니다.

## 실행

### GitHub 링크를 받는 테스트 프론트

```sh
uv sync --extra dev --extra demo
gh auth status  # 비공개 저장소는 서버에서 해당 저장소 접근 권한 필요
uv run --extra demo iris-review-demo --env-file ../.env
# 전역 opencode가 없으면 --opencode-executable /path/to/opencode 추가
```

브라우저에서 <http://127.0.0.1:8765/>를 열고 GitHub 저장소 링크를 입력합니다. 브랜치·태그·커밋을 따로 지정하거나 `/tree/test/feature` 링크를 사용할 수 있습니다. AI 공급자에서 Hive 또는 OpenAI를 선택할 수 있으며, 선택한 공급자의 키가 없으면 정적 분석을 제공합니다. 서비스 구성, 실행 명령, 포트, API 경로, 환경변수 키, 의존성, 확인할 항목을 표시합니다. 근거 버튼은 마스킹한 파일의 줄을 열고 JSON 다운로드는 고정 소스 커밋과 분석 결과를 저장합니다.

테스트 서버는 `127.0.0.1`에 바인딩합니다. 인증된 GitHub CLI로 커밋 SHA를 먼저 고정하고 tarball을 자료로 읽습니다. 다운로드한 프로젝트를 설치·빌드·실행하지 않습니다. 압축 파일 32 MiB, 압축 해제 선언 크기 100 MiB, 파일당 1 MB, 분석 파일 2,000개, 아카이브 항목 10,000개를 제한합니다. 동시 분석 1개, 대기 포함 3개, 프로세스당 기록 24개입니다. 기록 한도에 도달하면 서버를 재시작합니다. 화면의 최근 기록은 탭을 새로고침하면 초기화됩니다.

소스와 분석 산출물은 Git에서 제외된 `artifacts/review-demo/<id>/`에 남습니다. 화면의 근거와 모델 입력에는 기존 비밀 제외·마스킹을 적용하며, 소스 캐시는 운영자가 로컬에서 관리합니다. AI 요청은 기존 `artifacts/model-budget-ledger.json`과 누적 USD 1 상한을 공유합니다. 이 화면의 `/api/reviews`는 독립 테스트용 API입니다. 팀 WAS 연결 범위와 사용 예시는 [연결 준비 문서](docs/control-plane-readiness.md), 검증 결과는 [프론트 검증 보고서](reports/review-demo-validation.md)에 있습니다.

### 배포 계획과 실행 설정

```sh
uv run iris-deployment --repo ../tested_code/Temp_log --offline --out artifacts/deployment-static
uv run iris-deployment --repo ../tested_code/Temp_log --env-file ../.env --out artifacts/deployment-ai
```

미정인 클라우드·리전·트래픽·가용성·예산도 초기 가정으로 제안합니다. 소스 언어/버전 선언과 빠른 코드 검사, 사양·부분 비용·Kubernetes 계획을 별도 JSON으로 제공합니다. 측정한 부하 자료가 있으면 사양을 보정하며 build/test 성공을 실제 용량의 근거로 쓰지 않습니다. 준비된 이미지·네트워크·Secret·스토리지 등을 검증한 계획만 고정 Terraform/Helm 템플릿으로 변환합니다. 실제 apply·배포 승인은 수행하지 않습니다.

테스트 프론트에서는 배포 조건을 바꾸고 같은 소스의 계획만 다시 생성할 수 있습니다. [배포 계획 계약과 아키텍처](docs/deployment-planning.md)에 모듈 경계, 입력, 가정·측정 처리, 비용과 템플릿 지원 범위를 설명했습니다.

### 소스 분석 뒤 Dockerfile 준비

팀 WAS Worker가 호출하는 빌드 준비 계약을 추가했습니다. 기존 Dockerfile과 binary 자산을 보존하고, Dockerfile이 없으면 지원하는 Node/npm·기본 Vite 프로파일의 고정 템플릿으로 생성합니다. [빌드 준비 구조와 실행법](docs/build-preparation.md), [검증 결과](reports/build-preparation-validation.md)를 참조합니다.

### 기존 분석 CLI

```sh
uv run python scripts/preprocess_repository.py \
  --repo ../tested_code/Temp_log --out artifacts/preprocess
uv run python scripts/analyze_repository.py \
  --repo ../tested_code/Temp_log --offline --out artifacts/static
uv run python scripts/analyze_repository.py \
  --repo ../tested_code/Temp_log --env-file ../.env --out artifacts/analysis
uv run python scripts/verify_opencode.py \
  --env-file ../.env --repetitions 2 --out artifacts/verification
uv run python scripts/evaluate_quality.py \
  --tested-code ../tested_code --offline --out artifacts/quality-static
uv run python scripts/evaluate_quality.py \
  --tested-code ../tested_code --env-file ../.env --repetitions 2 \
  --reasoning-effort low --max-output-tokens 8192 --max-total-tokens 750000 --out artifacts/quality-live
```

같은 명령은 `iris-analyzer preprocess|analyze|verify|evaluate`에서도 제공합니다. verify의 기본 소스는 `fixtures/separated-web-api`입니다. 품질 평가는 사용자가 제공한 `Temp_log`와 `portpolio-production`을 사용합니다. 원본 대상 프로젝트는 이 저장소에 복제하지 않았습니다. `evaluations/ground-truth.json`의 검토한 파일 digest가 달라지면 `EVALUATION_FIXTURE_CHANGED`로 중단하므로 정답과 소스를 함께 검토해서 갱신해야 합니다.

기본 예산은 bundle 180,000 UTF-8 JSON bytes, 파일당 1,000,000 bytes, 추가 요청 최대 5개, 확장 1회입니다. `--max-bundle-bytes`, `--max-file-bytes`, `--max-expansions`, `--max-requested-files`로 조정합니다. 호출 전체 시간 예산은 `--timeout`으로 지정합니다. 정확한 모델 tokenizer가 없으므로 UTF-8 bytes를 보수적인 토큰 상한으로 사용합니다. 실제 요청의 스키마·프롬프트·출력과 허용된 단계·재시도·대화 성장분까지 따로 예약합니다. 기본 호출 수는 8회, 누적 토큰 한도는 500,000, 출력 한도는 8,192, 원격 모델 재시도는 0입니다. `--max-model-calls`, `--max-total-tokens`, `--max-output-tokens`, `--max-remote-retries`로 조정하며 자동으로 값이나 모델을 바꾸지 않습니다. 최종 평가 설정은 출력 8,192와 누적 토큰 750,000을 사용했습니다. 모델을 자동 교체하거나 실패 후 한도를 자동으로 올리지 않습니다.

종료 코드 0은 정상 분석이며 결과 status는 complete, needs_input, unsupported 중 하나입니다. complete는 분석 프로필의 필수 정보가 채워졌다는 뜻입니다. 실제 빌드·실행 성공은 후속 모듈에서 확인합니다. 오류는 2, 품질 기준 미달 또는 검증 호출 실패는 1입니다.

## 산출물과 데이터 계약

전처리는 `manifest.json`, `context.json`, `evidence.jsonl`, `model-input.json`을 저장합니다. 전체 manifest는 로컬에 유지하고 모델에는 선정 파일 정보와 추가 요청이 가능한 경로만 전달합니다. 분석은 `analysis-result.json`, `run-report.json`과 호출별 `revision-01/`, `revision-02/`를 추가합니다. 실제 요청은 `model-request.json`, 응답은 `model-response.json`에 저장합니다. 응답 형식 보정이나 실패 진단에는 안전하게 추린 `model-response-raw.json`을 사용합니다. 보정 여부는 formatRecovery로 기록하며 여러 객체·불완전 JSON·잘린 응답은 거절합니다. run-report에는 단계 이벤트, 모델·서버 버전, 세션 및 메시지 ID, 사용량과 지연, 오류 코드가 들어갑니다. 사용량·비용이 제공되지 않으면 null입니다.

스키마는 `src/iris_analyzer/schemas/`에 있습니다. [공통 계약](docs/implementation-contract.md)에 모듈별 함수와 필드를 설명했습니다.

```python
from iris_analyzer.contracts import Limits
from iris_analyzer.pipeline import analyze_with_report
from iris_analyzer.preprocess import prepare_context, expand_context

bundle = prepare_context('/path/to/repository', limits=Limits())
expanded = expand_context(bundle, ['server/src/middleware.ts'])
run = analyze_with_report('/path/to/repository', on_event=persist_job_event)
result = run.result
```

on_event는 queued → preprocessing → analyzing → validating → succeeded와 expanding, needs_input, unsupported, failed 이벤트를 전달합니다. worker가 Job 기록에 연결하면 됩니다. 생성 시각·절대 경로는 재현 가능한 context에 넣지 않습니다. 확장은 현재 프로세스가 고정한 스냅샷에서만 읽고 revision·contextHash를 갱신합니다.

표시 필드는 `{value,status,scope,evidenceIds,reason}`입니다. detected는 동일 scope의 정적 관측값과 일치해야 합니다. suggested는 유효한 근거를 가진 모델 제안입니다. 모델용 제안 스키마는 suggested/unknown만 허용하고 이미 감지한 값의 반복을 금지합니다. detected는 전처리와 결과 병합기가 보존합니다. unknown은 값이 null이며 이유를 담습니다. 모델이 관측값을 누락하면 검증기가 복원합니다. 추천이 관측값과 충돌하면 관측값을 보존하고 질문을 추가합니다. 잘못된 detected 값과 존재하지 않는 근거는 오류로 거절합니다.

componentRoots는 코드 모듈, deploymentCandidates는 실행 단위입니다. Temp_log의 client/server는 하나의 Web/API 앱으로 합쳐지고 MongoDB·볼륨은 의존성으로 표현합니다. 개발·컨테이너·호스트 매핑 포트는 별도 scope입니다. workingDirectory는 런타임의 작업 디렉터리이며 소스/build 루트와 별개입니다. 포트폴리오의 Node 빌드 정보와 nginx 실행 런타임도 분리합니다. API 경로와 외부 공개 라우팅도 구분합니다.

## OpenCode 환경

기본 실행은 임시 빈 작업 디렉터리와 별도 HOME/XDG 경로에 플랫폼 설정만 제공하는 서버를 시작합니다. localhost 바인딩과 임시 비밀번호를 사용합니다. 분석 대상 저장소나 개발자 전역 설정·플러그인·MCP를 연결하지 않습니다. 탐색·셸 도구는 거절하고 구조화 응답 도구만 허용합니다. 도구·설정 격리이며 운영체제 수준의 샌드박스는 아닙니다.

어댑터는 health 버전, 실제 /doc OpenAPI 명세, provider/model 발견 결과를 확인하고 새 세션을 만듭니다. timeout·취소 시 원격 abort를 요청합니다. 끊긴 응답은 기존 세션 상태와 메시지를 조회해서 실행을 무조건 재제출하지 않습니다. 인증 오류와 없는 모델은 재시도하지 않습니다.

Hive GLM의 기본은 `json_text`이며 수신 후 동일한 애플리케이션 스키마·근거 검증을 통과해야 합니다. 실제 검사에서 Hive는 필수 StructuredOutput 도구 요청과 네이티브 json_schema를 HTTP 400으로 거절했습니다. 검증한 Hive GLM에는 네이티브 `response_format=json_object`를 함께 전달합니다. 해당 도구를 지원하는 provider에 연결할 때만 `--output-mode structured`를 명시합니다. GLM에는 기본 추론 강도 `low`와 temperature 0을 전달합니다. Hive가 추론 강도 파라미터를 수락한 사실과 실제 처리 강도가 정확히 적용된다는 보장은 구분합니다. `OPENCODE_URL`은 같은 버전·권한을 구성한 외부 서버용입니다.

## 테스트와 품질

```sh
uv run ruff check .
uv run pytest --cov=iris_analyzer --cov-branch --cov-report=term-missing
IRIS_OPENCODE_EXECUTABLE=/path/to/opencode uv run pytest tests/test_opencode_runtime.py
```

CI는 Python 3.11·3.13에서 모델 비용이 발생하지 않는 검사를 실행합니다. 실제 모델 평가는 별도 CLI입니다. 품질 평가는 소스에서 검토한 실행 값·서비스·환경변수·의존성·연결과 API 경로 집합을 비교하고 경로 precision/recall/F1을 기록합니다. 재현성, 근거 digest, 비밀 파일 제외도 검사합니다. 최종 검증 결과와 병합 전 모델 응답을 따로 기록합니다. 자연어 해석 전부를 자동 검증했다고 주장하지 않습니다. 실행 결과는 [품질 보고서](reports/quality-evaluation.md)에 정리합니다.

동적 import·라우트·포트, 런타임 분기와 해석할 수 없는 참조는 unresolved입니다. 설정을 실행하지 않으므로 계산된 값은 확정하지 않습니다. 환경 예시는 키만 추출하고 모든 값을 가립니다. 실제 .env, 키·비밀 파일, symlink는 제공하지 않습니다. 소스의 알려진 자격 증명 패턴도 줄 수를 유지해서 가립니다. 다른 언어·프레임워크는 extractor 확장이 필요합니다. 인증은 middleware 이름만으로 확정하지 않습니다.

설계 기준: 2026-09-30 인계서, Notion [term1_team_iris](https://app.notion.com/p/2668bee9ada4824682738196ada10126), [AI API](https://app.notion.com/p/3eb8bee9ada48064a1f8f9110465939c), [Hive 공식 문서](https://docs.thehive.ai/docs/chat-completions-openai-compatible-llms), [OpenCode 서버 문서](https://opencode.ai/docs/server/)와 고정 런타임의 실제 /doc.

## 비용과 worker 연결

CLI는 기본 `artifacts/model-budget-ledger.json`에 비용을 먼저 예약하고, 제공된 사용량에 따라 추정 비용으로 정산합니다. `--budget-ledger`로 같은 파일을 여러 프로세스에서 공유할 수 있으며 파일 잠금과 fsync를 사용합니다. 기본 누적 상한은 `--max-cost-usd 1.0`입니다. 사용량을 받지 못하면 예약액을 유지합니다. 허용된 원격 재시도와 구조화 모드의 미확인 이전 단계 비용도 보수적으로 유지합니다. 실제 청구액은 알 수 없으면 null입니다.

2026-10-01 [Hive 공식 모델 페이지](https://thehive.ai/models/zai-org/glm-5.3-flash)에서 GLM의 100만 토큰당 입력 USD0.05, 출력 USD0.17, 캐시 읽기 USD0.01을 확인했습니다. 이 가격 스냅샷은 추정용입니다. 다른 모델은 검토한 `--pricing-json` 파일에 input/output/cacheRead 단가를 제공해야 금전 상한을 적용할 수 있습니다. 변경된 공급자 요금은 담당자가 다시 검토해야 합니다.

로컬 파일을 공유하지 않는 worker·서버·별도 저장소는 비용 기록을 공유하지 않습니다. 팀의 프로젝트 총 예산은 플랫폼의 공용 예산 기록과 예약 트랜잭션으로 연결해야 합니다. 이 라이브러리 자체가 프로젝트 전체 청구서를 조회하거나 전역 원화 상한을 보장하지 않습니다.

worker는 `analyze_with_report`에 모델 runner와 Job 이벤트 저장 콜백을 제공합니다. pipeline은 성공·실패 모두 스냅샷 참조를 해제합니다. prepare_context/expand_context를 직접 사용한 호출자는 확장이 끝난 뒤 `release_snapshot(snapshotId)`를 호출합니다. 같은 스냅샷을 쓰는 동시 작업은 참조 수로 보호합니다.

플랫폼의 HTTP /analyze 요청 형식, 소스 전달 방식, 환경변수 등록 및 데이터베이스 저장은 팀과 계약을 맞춰 연결할 부분입니다. 이 패키지의 테스트는 분석 모듈의 테스트이며 전체 배포 시스템의 E2E 완료를 뜻하지 않습니다. 공개 노출 경로와 인증 판단은 확정하지 않습니다. API 경로는 코드 내부에서 관측한 경로이며 공개 라우팅과 운영 정책은 후속 계획에서 결정합니다.

지원 환경은 macOS와 Linux입니다. 테스트/fixture 디렉터리는 최초 배포 단위 발견에서 제외하며, 직접 import한 파일과 허용된 추가 요청의 자료는 근거로 읽을 수 있습니다.
