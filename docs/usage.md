# 사용법

README의 [빠른 시작](../README.md#빠른-시작) 외 설치 옵션·실행 진입점·테스트를 정리한다. (README에서 원문 이동)

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

`OPENCODE_OUTPUT_MODE`를 생략하면 OpenAI는 구조화 응답 도구, Hive는 JSON 텍스트를 사용합니다. GPT-6 Luna 기본 reasoning은 `low`이며 이때 temperature를 보내지 않습니다. 분석 모델은 [v2 추가 제안 프로토콜](model-review-protocol.md)을 사용합니다. 서버가 요청·응답 연결을 확인한 뒤 snapshot/context를 붙이고 의미 검증을 수행합니다. 배포 계획의 digest 검증은 기존 계약을 유지합니다. 이전 분석 프로토콜의 실제 두 프로젝트 검증은 [OpenAI 검증 보고서](../reports/openai-validation.md)에 있습니다.

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

소스와 분석 산출물은 Git에서 제외된 `artifacts/review-demo/<id>/`에 남습니다. 화면의 근거와 모델 입력에는 기존 비밀 제외·마스킹을 적용하며, 소스 캐시는 운영자가 로컬에서 관리합니다. AI 요청은 기존 `artifacts/model-budget-ledger.json`과 누적 USD 1 상한을 공유합니다. 이 화면의 `/api/reviews`는 독립 테스트용 API입니다. 팀 WAS 연결 범위와 사용 예시는 [연결 준비 문서](control-plane-readiness.md), 검증 결과는 [프론트 검증 보고서](../reports/review-demo-validation.md)에 있습니다.

### 배포 계획과 실행 설정

```sh
uv run iris-deployment --repo ../tested_code/Temp_log --offline --out artifacts/deployment-static
uv run iris-deployment --repo ../tested_code/Temp_log --env-file ../.env --out artifacts/deployment-ai
```

미정인 클라우드·리전·트래픽·가용성·예산도 초기 가정으로 제안합니다. 소스 언어/버전 선언과 빠른 코드 검사, 사양·부분 비용·Kubernetes 계획을 별도 JSON으로 제공합니다. 측정한 부하 자료가 있으면 사양을 보정하며 build/test 성공을 실제 용량의 근거로 쓰지 않습니다. 준비된 이미지·네트워크·Secret·스토리지 등을 검증한 계획만 고정 Terraform/Helm 템플릿으로 변환합니다. 실제 apply·배포 승인은 수행하지 않습니다.

테스트 프론트에서는 배포 조건을 바꾸고 같은 소스의 계획만 다시 생성할 수 있습니다. [배포 계획 계약과 아키텍처](deployment-planning.md)에 모듈 경계, 입력, 가정·측정 처리, 비용과 템플릿 지원 범위를 설명했습니다.

### 소스 분석 후 서비스 빌드 인계

팀 WAS Worker가 호출하도록 준비한 빌드 준비 v2는(WAS 측 [iris-was PR #6](https://github.com/2026-softbank-1/iris-was/pull/6), 2026-10-04 기준 draft·미머지) 기존 Dockerfile과 binary 자산을 보존합니다. Dockerfile이 없으면 서비스 담당의 Railpack 경로를 추천하며, 분석기가 Dockerfile을 생성하지 않습니다. 명시적 빌더 선택을 보존하고 실제 선택·빌드·ECR push는 서비스가 담당합니다. [빌드 인계 계약](build-preparation.md), [v2 검증 결과](../reports/railpack-handoff-validation.md)를 참조합니다.

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

## 테스트와 품질

```sh
uv run ruff check .
uv run pytest --cov=iris_analyzer --cov-branch --cov-report=term-missing
IRIS_OPENCODE_EXECUTABLE=/path/to/opencode uv run pytest tests/test_opencode_runtime.py
```

CI는 Python 3.11·3.13에서 모델 비용이 발생하지 않는 검사를 실행합니다. 실제 모델 평가는 별도 CLI입니다. 품질 평가는 소스에서 검토한 실행 값·서비스·환경변수·의존성·연결과 API 경로 집합을 비교하고 경로 precision/recall/F1을 기록합니다. 재현성, 근거 digest, 비밀 파일 제외도 검사합니다. 최종 검증 결과와 병합 전 모델 응답을 따로 기록합니다. 자연어 해석 전부를 자동 검증했다고 주장하지 않습니다. 실행 결과는 [품질 보고서](../reports/quality-evaluation.md)에 정리합니다.

동적 import·라우트·포트, 런타임 분기와 해석할 수 없는 참조는 unresolved입니다. 설정을 실행하지 않으므로 계산된 값은 확정하지 않습니다. 환경 예시는 키만 추출하고 모든 값을 가립니다. 실제 .env, 키·비밀 파일, symlink는 제공하지 않습니다. 소스의 알려진 자격 증명 패턴도 줄 수를 유지해서 가립니다. 다른 언어·프레임워크는 extractor 확장이 필요합니다. 인증은 middleware 이름만으로 확정하지 않습니다.

지원 환경은 macOS와 Linux입니다. 테스트/fixture 디렉터리는 최초 배포 단위 발견에서 제외하며, 직접 import한 파일과 허용된 추가 요청의 자료는 근거로 읽을 수 있습니다.
