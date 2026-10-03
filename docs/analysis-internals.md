# 산출물·데이터 계약과 OpenCode 환경

(README에서 원문 이동)

## 산출물과 데이터 계약

전처리는 `manifest.json`, `context.json`, `evidence.jsonl`, `model-input.json`을 저장합니다. 전체 manifest는 로컬에 유지하고 모델에는 선정 파일 정보와 추가 요청이 가능한 경로만 전달합니다. 분석은 `analysis-result.json`, `verification-report.json`, `run-report.json`과 호출별 `revision-01/`, `revision-02/`를 추가합니다. 실제 요청은 `model-request.json`, 모델 delta 원본은 `model-wire-response.json`, 서버가 v1 형태로 변환한 응답은 `model-response.json`에 저장합니다. 실패 진단에는 안전하게 추린 `model-response-raw.json`을 사용합니다. 단일 완전 JSON 객체 추출 여부는 formatRecovery로 기록하며 여러 객체·불완전 JSON·잘린 응답은 거절합니다. run-report에는 단계 이벤트, 검증 판정, 모델·서버 버전, 세션 및 메시지 ID, 사용량과 지연, 오류 코드가 들어갑니다. 실제 청구액이 제공되지 않으면 null입니다.

스키마는 `src/iris_analyzer/schemas/`에 있습니다. [공통 계약](implementation-contract.md)에 모듈별 함수와 필드를 설명했습니다.

```python
from iris_analyzer.contracts import Limits
from iris_analyzer.pipeline import analyze_with_report
from iris_analyzer.preprocess import prepare_context, expand_context

bundle = prepare_context("/path/to/repository", limits=Limits())
expanded = expand_context(bundle, ["server/src/middleware.ts"])
run = analyze_with_report("/path/to/repository", on_event=persist_job_event)
result = run.result
```

on_event는 queued → preprocessing → analyzing → validating → succeeded와 expanding, needs_input, unsupported, failed 이벤트를 전달합니다. worker가 Job 기록에 연결하면 됩니다. 생성 시각·절대 경로는 재현 가능한 context에 넣지 않습니다. 확장은 현재 프로세스가 고정한 스냅샷에서만 읽고 revision·contextHash를 갱신합니다.

표시 필드는 `{value,status,scope,evidenceIds,reason}`입니다. detected는 동일 scope의 정적 관측값과 일치해야 합니다. 모델은 suggested/unknown 추가 제안만 반환하며 관측값은 서버가 보존합니다. unknown은 null과 이유를 담습니다. suggested는 ID 존재 검사에 더해 불변 원본의 관계·서비스·scope를 확인한 제안만 병합합니다. 무관한 제안은 제외하고 미지원 관계는 보류합니다. 확인된 충돌은 관측값을 보존하며 질문으로 남깁니다. 직접 라이브러리 `validate_analysis` 호출도 같은 검증을 적용합니다. 지원하는 좁은 규칙과 남은 한계는 [의미 검증 문서](semantic-evidence-verification.md)에 명시했습니다.

componentRoots는 코드 모듈, deploymentCandidates는 실행 단위입니다. Temp_log의 client/server는 하나의 Web/API 앱으로 합쳐지고 MongoDB·볼륨은 의존성으로 표현합니다. 개발·컨테이너·호스트 매핑 포트는 별도 scope입니다. workingDirectory는 런타임의 작업 디렉터리이며 소스/build 루트와 별개입니다. 포트폴리오의 Node 빌드 정보와 nginx 실행 런타임도 분리합니다. API 경로와 외부 공개 라우팅도 구분합니다.

## OpenCode 환경

기본 실행은 임시 빈 작업 디렉터리와 별도 HOME/XDG 경로에 플랫폼 설정만 제공하는 서버를 시작합니다. localhost 바인딩과 임시 비밀번호를 사용합니다. 분석 대상 저장소나 개발자 전역 설정·플러그인·MCP를 연결하지 않습니다. 탐색·셸 도구는 거절하고 구조화 응답 도구만 허용합니다. 도구·설정 격리이며 운영체제 수준의 샌드박스는 아닙니다.

어댑터는 health 버전, 실제 /doc OpenAPI 명세, provider/model 발견 결과를 확인하고 새 세션을 만듭니다. timeout·취소 시 원격 abort를 요청합니다. 끊긴 응답은 기존 세션 상태와 메시지를 조회해서 실행을 무조건 재제출하지 않습니다. 인증 오류와 없는 모델은 재시도하지 않습니다.

Hive GLM의 기본은 `json_text`이며 수신 후 동일한 애플리케이션 스키마·근거 검증을 통과해야 합니다. 실제 검사에서 Hive는 필수 StructuredOutput 도구 요청과 네이티브 json_schema를 HTTP 400으로 거절했습니다. 검증한 Hive GLM에는 네이티브 `response_format=json_object`를 함께 전달합니다. 해당 도구를 지원하는 provider에 연결할 때만 `--output-mode structured`를 명시합니다. GLM에는 기본 추론 강도 `low`와 temperature 0을 전달합니다. Hive가 추론 강도 파라미터를 수락한 사실과 실제 처리 강도가 정확히 적용된다는 보장은 구분합니다. `OPENCODE_URL`은 같은 버전·권한을 구성한 외부 서버용입니다.

설계 기준: 2026-09-30 인계서, Notion [term1_team_iris](https://app.notion.com/p/2668bee9ada4824682738196ada10126), [AI API](https://app.notion.com/p/3eb8bee9ada48064a1f8f9110465939c), [Hive 공식 문서](https://docs.thehive.ai/docs/chat-completions-openai-compatible-llms), [OpenCode 서버 문서](https://opencode.ai/docs/server/)와 고정 런타임의 실제 /doc.
