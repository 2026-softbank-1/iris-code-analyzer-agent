# AI 제안의 의미 근거 검증 설계

상태: **설계안 — 운영 적용 전**. 작성 2026-10-01, 기준 코드 `32e8d69`. [판단 정책](ai-judgment-policy.md)과 [11개 평가 사례](../evaluations/ai-judgment-cases.json)를 구현 구조로 구체화한다. 이번 변경은 검증기·프롬프트·모델 설정을 바꾸지 않는다.

## 1. 문제와 결정

현재는 `suggested`가 유효한 evidence ID를 인용하면, 그 구간이 결론을 지지하지 않아도 병합될 수 있다. `app.listen(3000)` → PostgreSQL 필요 주장 반례는 모델 없이 재현됐다. 따라서 먼저 **제안의 수용 조건**을 바꾸어야 한다. 모델 변경은 이 수용 조건을 대신하지 못한다.

정적 baseline도 모든 의미를 검증한 정답으로 취급하지 않는다. AI는 새로운 해석과 누락·충돌을 제안하고, 별도의 검증 코드는 같은 불변 소스에서 관계를 다시 확인한다. 이미 정적 추출된 fact와 같은 값만 허용하는 방식은 새로운 발견까지 막으므로 사용하지 않는다.

```text
불변 소스 + 정적 baseline + 선택된 실행 조건
    → AI: 원자적 주장과 근거 위치를 제안
    → 계약·출처 검사
    → 유형별 의미 규칙으로 원본 AST/설정을 재검사
    → 반대 근거·미확정 조건 확인
    → supported / rejected / deferred
    → 검토용 결과와 실행에 사용할 입력을 각각 구성
```

LLM의 설명·확신도·검토 모델의 동의는 검증 통과 권한이 아니다. Structured Outputs도 스키마 준수와 내용 정확성을 구분해야 한다. 공식 문서 역시 구조화된 출력에 오류가 남을 수 있다고 설명한다. [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs#handling-mistakes).

## 2. 검증 단위: 원자적 주장

기존 ModelReply를 한 번에 맞다/틀리다로 평가하지 않고, 주장마다 타입과 적용 조건을 둔다. 예를 들어 “PostgreSQL 5432가 필수이며 접속 가능”은 다음 네 주장으로 나눈다.

1. PostgreSQL client 라이브러리가 선언됨.
2. 해당 서비스의 선택된 소스 경로에 client 생성/connect 호출이 있음.
3. 연결 설정이 특정 endpoint/port를 참조함.
4. 실행 환경에서 실제 접속됨.

1의 근거가 2~4를 증명하지 않는다. 4는 소스 분석만으로 supported가 될 수 없고, 별도 실행 검증이 필요하다. 조건부 connect라면 조건도 주장에 남긴다.

제안 계약 `iris.analysis-claims.v1` 초안:

```json
{
  "schemaVersion": "iris.analysis-claims.v1",
  "sourceSnapshotId": "<snapshot-hash>",
  "contextHash": "<model-context-hash>",
  "baselineDigest": "<static-baseline-hash>",
  "executionSelectionDigest": "<selected-entrypoint-profile-stage-hash>",
  "claims": [{
    "claimId": "c-route-ready",
    "subject": {"serviceId": "<existing-service-id>", "component": "."},
    "predicate": "route.registered",
    "value": {"method": "GET", "path": "/health/ready"},
    "scope": "source",
    "conditions": [],
    "supportLocators": [
      {"evidenceId": "<receiver-id>"},
      {"evidenceId": "<constant-expression-id>"},
      {"evidenceId": "<registration-id>"}
    ],
    "derivationHint": {"rule": "express.const-route.v1"},
    "summary": "기존 Express receiver의 상수 경로 등록을 확인한다."
  }]
}
```

위 ID와 hash는 설명용이다. 실제 JSON Schema는 predicate별 value 타입을 분리하고 임의 필드를 금지한다. `summary`는 짧은 근거 요약이며 내부 추론 과정 제출을 요구하지 않는다. `derivationHint`, locator와 conditions는 전부 **검증할 제안**이다. 모델이 rule ID나 관계를 썼다는 이유로 참이 되지 않는다. `verified`, `approved`, `deploymentAuthorized` 같은 권한 필드는 모델 응답에 두지 않는다.

서비스 추가/분리 제안은 기존 serviceId를 조작하지 않고 별도 검토 항목으로 낸다. `npm run build` 래퍼나 초기 사양 같은 정책 제안은 `source_observation`과 다른 출처로 처리한다.

## 3. 검증 계층과 책임

| 단계 | 수행 주체·검사 | 실패 처리 |
| --- | --- | --- |
| 입력 바인딩 | 서버가 snapshot, context, baseline, 선택한 실행 조건, 정책 버전을 확인 | 다른 revision/service의 재사용·변조는 rejected |
| 근거 위치 | 실제 허용 파일·범위·digest·마스킹 여부 확인 | 위조/이탈은 rejected, 필요한 내용 미제공은 deferred |
| 의미 규칙 | AST·JSON/YAML·Docker 구문에서 predicate별 관계를 재구성 | 무관 인용·틀린 도출은 rejected, 미지원 구문은 deferred |
| 적용 조건 | 서비스 소유권, build/runtime, Compose 순서/profile, entrypoint/stage/override 확인 | 조건 미선택·외부 값 미확정은 deferred |
| 반대 근거 | 제안자가 인용한 줄 외에도 해당 유형의 관련 설정·참조를 제한된 범위에서 확인 | 실제 충돌이면 기록, 필요한 검사 범위가 빠졌으면 deferred |
| 수용·실행 입력 | 플랫폼이 결과와 사용 목적을 판단 | 미검증 주장은 실행 입력에서 제외; 필수 값이면 blocking |

검증 코드는 `get_snapshot` 등 기존 불변 자료 경계를 사용하고 소스를 실행하지 않는다. 임의 파일 시스템 경로, shell/eval, 네트워크 탐색으로 관계를 “증명”하지 않는다. parser가 재구성한 symbol binding·제어 조건·scope·값을 모델 제안과 비교한다. 인용된 구절에 키워드가 있는지만 확인하는 정규식으로 대체하지 않는다.

검증 범위는 predicate별 참조 의존 관계로 제한한다. 조사한 파일/구간·미확인 참조·한도 소진을 report에 남긴다. 새 파일이 필요하면 기존 eligible 추가 파일 절차를 거쳐 context를 갱신하고 다시 검증한다. “제공된 범위에서 반대 근거를 못 찾음”을 “저장소 전체에 반대 근거 없음”으로 바꾸지 않는다.

## 4. 최초 구현할 규칙

| 주장 | 최소 확인 관계 | supported가 뜻하지 않는 것 |
| --- | --- | --- |
| `dependency.declared` | 해당 package manifest의 dependency 이름·버전 선언 | 실제 사용·DB 필수성·설치 완료 |
| `database.client_declared` | library binding → 해당 client/호출 → 같은 서비스의 config 참조 | 실제 실행·연결 성공·외부 DB 생성 필요 |
| `database.endpoint_referenced` | 위 사용 위치 → 특정 env/config key → 별도 확인한 target mapping | 비밀값 복원·네트워크 접근·인증 성공 |
| `port.listener_declared` | 올바른 server receiver → literal/제한된 상수식/선택한 env → listen | EXPOSE의 실제 listen 보장·공개 포트·실행 성공 |
| `route.registered` | framework receiver → 경로 표현식 → 등록 호출; router mount면 prefix 관계까지 | 경로 이름으로 readiness 판정·HTTP200 보장 |
| `runtime.image_declared` | 선택한 Docker target/final stage와 stage 상속 | build image가 runtime임·설치 patch/CPU 호환성 |
| `command.declared` | 소유 package/stage/cwd → script 또는 ENTRYPOINT/CMD → 선택된 override | 명령 실행 가능·build/start 성공 |
| `environment.consumed` | 실제 읽는 binding/schema → key → default/필수 guard → component·phase | 이름만으로 필수·Secret·모든 서비스의 소유권 확정 |

초기 규칙은 좁은 문법을 확실하게 지원한다. 예를 들어 문자열 literal과 immutable const의 연결은 지원하되, 재할당·shadowing·미확인 import·동적 eval·임의 함수 호출은 보류한다. wrapper와 프레임워크 확장은 각각 버전이 있는 verifier rule로 추가한다.

**새 발견을 허용하는 양성 예:** 현재 정적 추출기가 놓치는 `const healthPath = '/health/' + 'ready'; app.get(healthPath, handler)`는 검증기가 receiver와 const binding, 문자열 결합, 등록 호출을 독립 확인하면 supported가 된다. baseline에 같은 fact가 없다는 이유로 거절하지 않는다.

**문제의 반례:** `app.listen(3000)`은 port listener 규칙의 입력이 될 수 있지만 DB client/endpoint 규칙의 관계를 제공하지 않는다. PostgreSQL 제안은 `EVIDENCE_PREDICATE_MISMATCH`로 거절한다. 이는 **그 제안의 근거가 부족하다는 판정**이며 저장소에 PostgreSQL이 절대 없다는 결론이 아니다.

## 5. 판정과 실행 영향은 분리

| 판정 | 뜻 | 결과 처리 |
| --- | --- | --- |
| supported | 명시된 범위·조건에서 해당 좁은 선언을 검증함 | 검증된 제안으로 노출. 다른 조건·실행 성공으로 확대하지 않음 |
| rejected | 위조/무관 근거, 잘못된 도출 또는 반박된 주장 | 그 주장만 제외하고 원문·실패 사유 보존 |
| deferred | 근거·선택·외부 설정·검증 규칙이 부족 | missing obligations와 최소 다음 행동 제공 |

`impact=advisory|blocking`은 플랫폼이 결정한다. 모델이 자기 제안의 중요도를 낮춰 실행을 우회할 수 없다.

- 실제 실행 설정이 명확한데 README가 오래된 경우: advisory.
- 정상 앱에 추가한 무관한 PostgreSQL 제안이 rejected인 경우: 제안 제거. 그 사실만으로 기존 정상 앱 전체를 막지 않는다.
- 배포가 소비할 필수 포트·명령·필수 env가 미검증인 경우: blocking.
- optional 정보가 미확정인 경우: 모든 서비스를 일괄 차단하지 않는다.

원래 정적 관측은 보존하고 검토 상태를 별도로 붙인다. suspicious detected를 검토하지 않고 자동 신뢰하는 우회 경로를 만들지 않는다. 실행에 사용되는 정적 관측에도 적용 조건과 필요한 rule 검사를 수행한다.

## 6. 모듈·산출물·신뢰 경계

```python
# 신규 모듈 제안 — 아직 구현된 공개 API가 아님
verify_claims(bundle, baseline, proposals, execution_selection, policy_version)
# -> VerificationReport

admit_analysis(baseline, proposals, verification_report)
# -> AnalysisEnvelope

require_execution_support(envelope, used_claim_ids)
# -> VerifiedExecutionInputs 또는 부족한 조건
```

- `verification/claims.py`: 원자적 주장·predicate별 schema·정규화.
- `verification/kernel.py`, `verification/rules/*`: 불변 소스 재검사와 제한된 도출 규칙.
- `verification/admission.py`: verified 제안 구성, advisory/blocking, 실행 입력 허용.
- `schemas/analysis-claims.schema.json`, `analysis-verification.schema.json`: 별도 v1 계약. 기존 analysis-result v1에 임의 필드를 추가하지 않는다.
- `pipeline.py`, `result.py`: raw proposal → report → admitted result 순서로 연결. 기존 무조건 suggested 병합 경로를 제거한다.
- `deployment/planner.py`, `build/prepare.py`, compiler: 실제 소비하는 claim 목록과 검증 결과를 확인한다.

Report는 snapshot/context/baseline/proposal/실행조건 digest와 verifier version, 사용한 rule version, 판정·reasonCode·검사 범위·미해결 의무를 가진다. **digest는 인증이 아니다.** 외부 요청자가 report나 `supported=true`를 만들어 보내도 통과시키지 않는다. 같은 신뢰 Worker에서 재검증하거나 서버가 소유한 검증 기록을 참조한다. cache key에는 소스·선택 조건·제안·verifier/rule version을 포함한다.

`detected/suggested`는 발견 출처이고 `supported/rejected/deferred`는 검증 결과다. AI가 제안한 값을 검사했다고 AI 기여 이력을 정적 발견으로 바꾸지 않는다. v1 호환 결과와 별도 review/verification 문서를 함께 제공하되, 배포 adapter가 review를 무시하고 예전 v1만 소비하는 경로는 차단한다. 기존 캐시 중 검증 기록 없는 결과는 재검증하거나 review-only로 취급한다.

## 7. 모델 역할과 비교 설계

현재 `gpt-6-luna`는 공식 문서에서 집중된 고빈도 작업용 효율 모델로 설명하며 reasoning none/low/medium/high/xhigh/max를 지원한다. 문서에 파라미터 수가 제시되지는 않아 이름이나 context 길이만으로 모델 크기·검증 성능을 단정하지 않는다. 프로젝트 `ModelConfig`의 Luna 기본 reasoning은 **low**다. [공식 모델 문서](https://developers.openai.com/api/docs/models/gpt-6-luna).

초기 선택은 **Luna를 proposer로 유지하고 검증 계층부터 적용**한다. 더 큰 모델은 필요한 근거를 찾거나 복잡한 관계를 해석하는 성공률을 개선할 수 있지만, 잘못된 제안 수용을 막는 계약을 대체하지 않는다. 본 설계에서 실제 모델/effort는 바꾸지 않았다.

| 비교군 | 바꾸는 것 | 확인할 효과 |
| --- | --- | --- |
| A | 현재 Luna low + 현재 입력/프롬프트/검증 | 기준선 |
| B | 같은 Luna low + 새 검증 계층, 입력/프롬프트는 우선 동일 | 검증기만으로 escaped false claim이 줄어드는지 |
| C | B + 실제 baseline/실행 조건/반대 근거를 포함한 입력과 새 프롬프트 | 제안과 적절한 보류의 품질 변화 |
| D | C와 같은 구성, Luna medium | reasoning 비용·지연 대비 개선 |
| E | C와 같은 입력·검증, 선택한 상위 모델 | 크기/모델 변경의 추가 이득 |

미확정이라고 모두 큰 모델로 보내지 않는다. 파일이 없으면 확보하고, profile이 없으면 선택 입력을 받고, rule이 없으면 verifier 지원을 추가한다. **필요한 자료와 검증 규칙이 있는데도 추론이 어려운 경우만** 상위 모델 재검토 후보로 둔다.

검토용 LLM을 추가한다면 제안자의 장황한 설명/확신도 대신 원문 근거·주장·반대 후보를 받아 독립적으로 반박을 찾게 한다. 같은 모델 또는 다른 모델도 같은 오류를 낼 수 있어 동의 표결로 자동 승인하지 않는다. LLM-only 판단은 검토 의견으로 남고, hard rule 실패·자료 부재를 뒤집을 수 없다.

## 8. 수용 기준과 적용 순서

1. 현재 PostgreSQL 반례를 포함해 무관 evidence ID, scope/service/stage 바꿔치기, 위조 검증 결과, source/context replay를 차단한다.
2. baseline에 없던 정상 문자열 결합 route는 검증해 수용한다. 재할당/shadowing/미확인 import 변형은 보류한다.
3. dev/container/host 포트 차이를 오충돌로 막지 않고, 같은 실행 경로의 충돌은 탐지한다.
4. pg 설치만으로 DB 연결을 확정하지 않는다. 최종 nginx runtime과 Node build를 분리한다.
5. rejected optional 제안은 정상 실행을 막지 않고, 미검증 필수 입력은 Helm/Dockerfile 생성 등 실행용 변환에 들어가지 못한다.
6. 근거 ID를 무관한 ID로 교체하거나, 실제 선언을 바꾸거나, 참조를 제거한 변형 테스트에서 판정이 그에 맞게 바뀌어야 한다.
7. raw AI 오답, 검증기에 차단된 오답, 빠져나간 오답, 유용한 신규 발견, 적절/불필요한 보류를 따로 집계한다.

먼저 원문 제안을 별도 보존하고 미검증 제안의 실행 입력 사용을 차단한다. 다음으로 typed claims와 port/command/runtime/DB·양성 route 규칙을 구현하고, 반대 근거 탐색·admission·downstream 소비를 연결한다. 그 뒤 A~E를 동일 사례와 반복 조건에서 비교한다.

현재 11개 사례는 smoke/regression 출발점이다. prompt 예제로 사용한 사례와 평가용 저장소·변형을 분리하고, unseen 사례를 추가한다. 각 군의 모델 식별자·prompt/rule 버전·입력 hash·effort·usage·latency·실패와 zero-denominator N/A를 기록한다. 제한된 테스트에서 escaped 오류 0건은 필수 회귀 조건이지 일반 정확성 증명이 아니다. 새 유료 실험은 기존 공유 예산에 먼저 예약하며, 한도를 자동 증가시키지 않는다.
