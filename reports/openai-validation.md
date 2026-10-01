# OpenAI GPT-6 Luna 검증 · 2026-10-01

`OPENAI_API`로 제공한 서버 키를 연결하고 사용자가 지정한 **openai / gpt-6-luna**로 실제 분석과 배포 계획을 검증했다. 최종 구조화 호출은 두 프로젝트의 소스 분석과 계획 생성 총 4회 모두 성공했다. 테스트 프론트에서도 OpenAI를 선택해 기존 소스 리뷰의 계획을 다시 생성했고 7.99초에 완료했다. 원시 산출물은 Git 제외된 `artifacts/openai/`와 `artifacts/review-demo/`에 있으며, 가공 가능한 검증 결과는 [JSON](openai-validation.json)에 저장했다.

## 연결과 검증 경계

- `OPENAI_API_KEY` 또는 별칭 `OPENAI_API`를 읽고 `OPENAI_MODEL=gpt-6-luna`를 사용한다. Hive 키를 OpenAI에 재사용하지 않는다. 프론트에는 공급자·모델·키 존재 여부만 제공한다.
- OpenCode 1.18.33의 OpenAI 제공자는 `@ai-sdk/openai`와 공식 Responses endpoint를 사용한다. 격리 프로세스에는 선택한 공급자의 키만 전달한다. `store=false`, reasoning `low`, 원격 재시도 0이며 모델을 자동 교체하지 않는다. [고정 OpenCode 구현](https://github.com/anomalyco/opencode/blob/v1.18.33/packages/opencode/src/provider/provider.ts), [인증 문서](https://developers.openai.com/api/reference/overview#authentication).
- Luna의 reasoning을 사용할 때 temperature를 생략한다. OpenAI 기본 출력은 OpenCode의 StructuredOutput 도구이며, snapshot/context 및 planning digest를 요청별 schema `const`로 고정한다. 최종 JSON은 기존 애플리케이션 계약·근거·관측값 검증도 통과해야 한다. [Luna 모델 문서](https://developers.openai.com/api/docs/models/gpt-6-luna), [모델 설정 문서](https://developers.openai.com/api/docs/guides/latest-model).
- 가격 snapshot과 ledger 예약은 cache read/write, reasoning 출력, 긴 context 할증을 반영한다. 실제 청구는 확인되지 않았으며 `cost=null`이다. SDK 비용 추정은 별도 필드에 남긴다. 모든 공급자는 기존 누적 USD 1 상한을 공유한다. 검증 종료 시 예약 포함 누적 계상액은 USD 0.878278215이며, 불완전 usage의 예약을 해제하지 않았다. [가격 문서](https://developers.openai.com/api/docs/pricing).

## 실제 호출 결과

| 프로젝트 | 최종 소스 분석 | 소스 품질 기준 | 최종 계획 생성 | 계획/실행 상태 |
| --- | --- | --- | --- | --- |
| Temp_log | complete · 5.11초 | 22/22, 경로 21/21, F1 1.0 | 5.97초 | needs_input / blocked |
| portpolio-production | complete · 5.22초 | 15/15, 관측 경로 없음 | 7.53초 | needs_input / blocked |

`evaluations/ground-truth.json`에서 검토한 소스 파일 digest가 이번 산출물의 manifest와 모두 일치한다. 저장된 최종 응답과 context/manifest/evidence를 기존 `score_result`로 평가했다. API 경로의 precision/recall 및 주요 관측값, 근거 digest와 비밀 파일 제외 기준을 통과했다.

두 소스 응답 모두 추가 제안을 비워 반환했다. 따라서 위 점수는 병합된 정적 관측과 검증 계약을 확인한 결과다. 모델이 새로운 의미 정보를 발견했거나 코드 전체를 이해했다는 평가가 아니다. 최종 structured 호출은 프로젝트당 한 번이며 여러 번의 재현성·통계적 안정성을 입증하지 않는다.

계획의 추천 사양·예산·트래픽은 운영 가정으로 표시했다. 검증된 이미지, 네트워크, Secret, 스토리지 등 실행 입력이 없어 `needs_input`을 유지하고 고정 템플릿 컴파일을 보류했다. `deploymentAuthorized=false`이며 Terraform apply, 클라우드 생성, Kubernetes 배포를 수행하지 않았다. 기존 Docker 테스트/부하 측정의 범위와 제한은 [용량 평가](runtime-capacity-evaluation.md)를 따른다.

## 실패 기록과 수정

초기 JSON 텍스트 방식에서는 portfolio가 통과했고 Temp_log는 `RESULT_SCHEMA_INVALID`로 거절됐다. Temp_log 모델이 64자리 context hash에서 한 글자를 누락해 63자리로 반환했기 때문이다. 잘못된 identity를 수정해서 받아들이지 않았다.

이에 snapshot/context 및 계획 digest를 요청마다 schema `const`로 고정하고 OpenAI 기본 출력을 구조화 도구 방식으로 바꿨다. 이후 최종 4회는 형식 복구 없이 통과했다. 초기 실패와 사용량도 JSON 보고서와 로컬 raw 산출물에 보존했다. 재시도 비용·예약을 지우지 않았다.

## 회귀와 프론트

- Python 3.11 및 3.13: 각각 **436 passed**. 고정된 실제 OpenCode 서버와 비용 없는 native inference 회귀도 포함한다.
- Python 3.13 branch-aware coverage: **86.25%**. Ruff lint/format, JavaScript 문법, Prettier 검사 통과.
- wheel 빌드 성공. 신규 pricing 모듈 및 최신 프론트 정적 파일이 wheel에 포함됨을 확인했다.
- 서버를 최신 코드로 재시작하고 `/api/config`의 기본 모델 `gpt-6-luna`, Hive/OpenAI 공급자 가용성을 확인했다.
- 프론트에서 기존 Temp_log 리뷰를 재사용해 **OpenAI AI 계획**을 생성하고 `succeeded`와 공급자·실제 호출 모델을 확인했다. 기존 리뷰의 소스 분석 방식은 정적으로 표시하고 계획만 AI 제안으로 표시했다. 이 UI 사례는 기존 커밋 `54fa8072d9fb`의 소스 리뷰이며 위 두 최종 로컬 소스 검증과 구분한다.

로컬 검증 명령:

```sh
IRIS_OPENCODE_EXECUTABLE=/path/to/pinned/opencode .venv/bin/pytest -q \
  --cov=iris_analyzer --cov-branch
IRIS_OPENCODE_EXECUTABLE=/path/to/pinned/opencode .venv-py311/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check src tests
node --check src/iris_analyzer/demo/static/app.js
npx prettier --check src/iris_analyzer/demo/static/{app.js,index.html,style.css}
uv build --wheel
```
