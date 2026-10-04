# AI 추가 검토 프로토콜 v2

2026-10-04. 모델 전송 형식은 `iris.model-review.v2`, 외부 분석 결과는 기존 AnalysisResult v1이다. 모델은 전체 결과를 재작성하지 않고 추가 제안만 반환한다. 구현은 `opencode/review_protocol.py`, 운영 프롬프트는 `deployment_v2.2`이다. DB 사용·내부/외부 대상·SQLite 저장소·멀티 이미지 의존 관계를 검토하며 생성/배포 승인을 출력하지 않는다.

## 입력과 응답

요청은 같은 불변 snapshot의 `contextBundle`, 읽기 전용 `staticAnalysis`, `executionMetadata`, `responseSchema`, `responseTemplate`을 포함한다. 평가용 `selectedExecutionContext`는 평가 호출자가 제공한 실행 조건이며 정답이나 사례 ID를 포함하지 않는다. 일반 실행에서 선택되지 않은 운영 조건을 모델이 선택 완료로 가정할 수 없다.

```json
{
  "kind": "review",
  "changes": [],
  "reviewFindings": [],
  "questions": []
}
```

각 change는 `target`, `serviceId`, `field`를 가진다. 컬렉션 target은 `apiRoutes`, `dependencies`, `connections`, `environmentKeys`이며 serviceId는 null이다. 서비스 target은 `services.runtime`, `services.buildCommand`, `services.startCommand`, `services.workingDirectory`, `services.outputDirectory`, `services.ports`, `services.healthchecks`이며 기존 serviceId가 필요하다. 임의 서비스 추가 및 같은 scalar 필드의 중복 변경은 거절한다.

field의 상태는 `suggested` 또는 `unknown`이다. 문자열·정수 포트·route 객체·connection 객체를 구분한다. API route는 `{method,path,component}`이며 scope는 source이다. component는 파일명이 아닌 실제 component root다. dependencies의 상세 값은 의미 검증 단계에서 지원하는 좁은 선언과 대조한다.

자료가 부족하면 review 대신 기존 `needs_files` 응답을 보낸다. `availablePaths`뿐 아니라 선택됐지만 일부 줄만 제공된 `expandableSelectedPaths`도 요청할 수 있다. 같은 불변 snapshot에서 허용된 파일만 확장하고 revision/contextHash를 갱신한다. 호출·파일·확장·토큰·누적 비용 한도를 유지하며 한도 도달은 보류로 남긴다.

## 출처와 의미 검증

모델은 sourceSnapshotId, contextHash, 최종 status, 승인 여부를 반환할 수 없다. 매 호출의 새 OpenCode 세션에서 요청 message ID와 응답의 parentID/assistant 역할을 대조한 뒤 서버가 출처를 붙인다. 잘못된 모델 해시를 고치는 절차가 아니다. 이전 전체 결과 응답 v1은 보관 프롬프트 비교에서만 명시적으로 선택한다.

서버 어댑터가 기존 서비스의 식별·형식 필드를 채워 canonical v1 제안으로 변환하고 `validate_analysis_with_report`에 전달한다. 어댑터가 복사한 관측값은 AI가 발견한 사실로 집계하지 않는다. 공개 `validate_analysis`도 같은 의미 검증을 거친다.

검증기는 근거 ID, 원본 파일 digest·줄·마스킹, 관계와 서비스·scope를 확인하고 주장마다 supported/rejected/deferred를 기록한다. 확인된 좁은 제안만 병합한다. 무관한 제안의 거절 자체는 정상 baseline을 차단하지 않는다. 실제 미해결 실행 조건이나 확인된 충돌은 질문과 needs_input을 유지한다.

`reviewFindings`는 documentation_mismatch, runtime_stage_mismatch, source_conflict, missing_evidence를 받는다. 자유로운 모델 설명은 원본 응답에 보존하고, 검증된 출력의 설명·blocking 여부는 플랫폼이 생성한다. 새 임의 질문이나 미검증 설명은 확인된 소스 하자로 승격하지 않는다. 지원하지 않는 의미 규칙은 deferred다.

## 산출물과 소비 경계

| 파일 | 용도 |
| --- | --- |
| revision-XX/model-request.json | 실제 모델 요청·프롬프트·입력·스키마 |
| revision-XX/model-wire-response.json | 검증된 모델 원본 delta 응답 |
| revision-XX/model-response.json | 어댑터가 출처와 서비스 형식을 붙인 canonical 응답 |
| model-response-raw.json | 파싱·계약 실패 진단용 안전한 원격 응답 기록 |
| verification-report.json | iris.analysis-verification.v1: 원본 식별자, baseline/proposal/result digest, 주장별 판정·규칙·검사 위치·미해결 사항 |
| analysis-result.json | 기존 소비자용 병합 AnalysisResult v1 |
| run-report.json | 호출·비용·지연·오류 및 verification sidecar |

검증 보고서의 `deploymentAuthorized`는 false다. 이 결과는 소스 선언의 검토이며 빌드·접속·부하·배포 성공을 증명하지 않는다. 별도 v1 JSON을 직접 받는 하위 모듈에 이 보고서만으로 인증된 실행 권한이 생기지 않는다. Worker는 자신이 호출한 분석 결과와 보고서를 함께 보관하고 source/result digest를 대조해야 한다.

Dockerfile 부재는 서비스 담당 Railpack 경로다. 확인된 실제 실패의 로그 기반 개선 인계는 별도 [계약 초안](remediation-handoff.md)이며, 이 변경이 자동 수정·재실행 루프를 구현한 것은 아니다.

## 평가

정적 baseline, 모델 원본 delta, 검증 판정, 최종 결과를 별도로 비교한다. 정확한 값이라도 근거 검증을 통과하지 못하면 신규 발견 TP가 아니다. 정적 중복·어댑터 복사·검증기 자체 발견은 AI 기여에서 제외한다. 검증기 반례는 실제 모델의 오답과 따로 집계한다. 자연어 설명 전체의 정확도는 자동 점수로 단정하지 않고 검토 대상을 보존한다.

[판단 기준](ai-judgment-policy.md), [검증 규칙과 한계](semantic-evidence-verification.md), [실제 평가 결과](../reports/ai-judgment-evaluation.md)를 함께 참조한다.
