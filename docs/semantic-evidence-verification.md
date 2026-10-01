# AI 제안의 의미 근거 검증 구현과 확장 경계

상태: **운영 경로 연결 완료 — 제한된 의미 규칙 지원**. 최종 반영 2026-10-02. [판단 기준](ai-judgment-policy.md)의 다섯 열 판단표를 구현 구조와 연결한다. 모델 변경 제안은 검증 후에만 병합하며, 폭넓은 데이터 흐름 분석이나 자동 수리 에이전트까지 구현됐다는 뜻은 아니다.

## 1. 현재 처리 구조

과거에는 유효한 evidence ID를 가진 `suggested`가 실제 의미와 무관해도 병합될 수 있었다. `app.listen(3000)`을 근거로 PostgreSQL 필요성을 주장하는 합성 반례가 그 결함을 드러냈다. 현재는 public validation과 pipeline에 의미 검증 커널을 연결했고 해당 제안을 거절한다. 과거 감사 결과는 이전 구현의 기록으로 보존한다.

```text
불변 ContextBundle + 정적 baseline + 실행 메타데이터
    → OpenCode 모델: v2 변경 제안 또는 최소 추가 파일 요청
    → 응답 연결·wire schema 검사
    → adapter: 현재 context 식별자를 붙인 canonical v1 제안
    → public validator: identity·detected·근거 참조 검사
    → semantic kernel: 선택된 불변 원문 재검사
    → supported / rejected / deferred + 검토 sidecar
    → 수용된 값 병합·제한된 의무 해소·최종 상태 계산
    → analysis-result.json + verification-report.json
```

정적 baseline도 완전한 의미적 정답으로 간주하지 않는다. 커널은 모델 제안이 비어 있거나 static 모드인 경우에도 선택된 최종 이미지와 runtime 필드의 불일치, 일부 의심스러운 listener/health receiver, 이미 확인된 동일 scope 충돌을 감사한다. 이 발견은 `origin=verifier`로 남기며 모델의 성과가 아니다.

## 2. 모델 입출력: 전체 재작성 대신 변경 제안

`opencode/runner.py`의 현재 프롬프트는 `deployment_v2.0`이며 입력에 다음 내용을 제공한다.

- `contextBundle`: facts, 서비스 후보, 실제 evidence, 제공 범위, 미해결 참조와 적격 추가 경로.
- `staticAnalysis`: 동일 context에서 계산한 전체 정적 baseline.
- `executionMetadata`: buildTargets, 환경변수 소비자, serviceConnections.
- `responseSchema`와 `responseTemplate`: `iris.model-review.v2` wire 계약.

선택됐지만 일부 줄만 제공된 파일은 `expandableSelectedPaths`로 구분한다. snapshot registry에 원문이 있다는 사실만으로 모델이 그 내용을 이미 읽었다고 취급하지 않는다. 필요한 근거가 입력에 없으면 모델은 제한된 `needs_files` 절차를 사용한다.

```json
{
  "kind": "review",
  "changes": [{
    "target": "apiRoutes",
    "serviceId": null,
    "field": {
      "value": {"method": "GET", "path": "/health/ready", "component": "."},
      "status": "suggested",
      "scope": "source",
      "evidenceIds": ["<실제 context evidence ID>"],
      "reason": "Express receiver, 불변 문자열 식, 등록 호출의 관계를 확인한다."
    }
  }],
  "reviewFindings": [],
  "questions": []
}
```

위 내용은 구조 예시다. evidence가 없는 저장소에 그대로 적용하면 안 된다. 모델은 hash·schemaVersion·최종 분석 status·전체 서비스 메타데이터를 반환하지 않는다. 검증된 요청/응답 연결을 통해 adapter가 source identity를 부착한다. 잘못된 모델 hash를 사후 교정하는 방식이 아니다. wire 원문과 canonical 변환 결과는 각각 저장한다.

서비스 target은 `services.runtime`, `services.buildCommand`, `services.startCommand`, `services.workingDirectory`, `services.outputDirectory`, `services.ports`, `services.healthchecks`이다. collection target은 apiRoutes/dependencies/connections/environmentKeys이고 serviceId=null이다. 서비스 변경은 기존 serviceId를 요구한다. scalar 변경을 중복 제출하거나 존재하지 않는 서비스를 참조하면 거절한다. route/port/text/connection은 target별 value 구조를 검사하고, dependencies의 의미는 이어지는 커널 규칙으로 제한한다.

`needs_files`는 `requestedPaths`와 `reason`을 갖는 별도 응답이다. `.env`·제외 파일·없는 경로·이미 충분히 제공된 파일을 임의로 읽는 권한이 아니다. 기존 expansion·입력·비용 한도를 사용한다.

## 3. 불변 근거와 의미 규칙

`verification/source.py`는 동일 snapshot의 registry bytes를 읽는다. registry가 없으면 마스킹되지 않은 evidence들이 원본 전체를 재구성하고 manifest digest와 일치할 때만 사용한다. live checkout을 다시 읽거나 대상 프로그램·설치 명령·모듈·shell·eval을 실행하지 않는다. evidence의 실제 텍스트와 마스킹 상태도 captured bytes와 대조한다. 전체 원문을 확인할 수 없는 파일은 `unavailableSourcePaths`로 기록하고 해당 규칙을 보류한다.

일반 관측은 선택된 검증 원문에서 다시 추출한 값·scope·서비스와 비교한다. 필요한 fact evidence의 **전체 구간**을 인용이 덮어야 한다. receiver 선언 한 줄만으로 listener 포트를 지지하거나, 같은 Compose 파일의 무관한 행을 DB 증거로 쓰는 방식은 허용하지 않는다. Express의 신규 route·소스 listener·health route는 별도 lexical 규칙도 적용한다.

| 규칙 영역 | 현재 구현한 확인 | 아직 지원하지 않거나 증명하지 않는 것 |
| --- | --- | --- |
| 신규 직접 Express route | 검증된 Express factory → 변경되지 않은 receiver → top-level 등록 호출 → literal/불변 const 문자열 연결. method/path 인용 구간 확인 | 임의 함수 계산, 모듈 간 값 추론, router mount 전체, framework wrapper, 실제 HTTP 성공 |
| source listener·health route 제안 | 원문 witness와 독립 receiver/scope 확인. literal/불변 상수 listener, 직접 GET 등록의 좁은 관계 | 동적 env의 운영값, 모든 server 프레임워크, 실제 readiness·DB 정상 상태 |
| 기존 명령·runtime·출력 경로·환경 키·연결·스토리지 제안 | 재추출된 동일 값·scope·소유 서비스와 전체 witness 일치. 미선택 조건부 fact는 제안의 확정 근거로 사용하지 않음 | 임의 새 명령 실행 가능성, npm wrapper를 소스 원문으로 승격, 환경 키 requiredness·서비스 통신을 이름만으로 확정 |
| DB 관련 제안 | 재추출된 좁은 dependency 선언과 일치하는 범위만 수용. 무관한 listener 인용이나 근거 없는 추가 host/port/required 속성 제외 | 범용 client 생성→connect→설정→실제 endpoint dataflow, 접속·인증·DB 생성 필요성 |
| README 검토 | **실제로 인용된** 비마스킹 줄의 명시적인 production/container 명령·포트를 해당 실행 선언과 비교 | 자연어 전체 의미 판독, 읽지 않은 뒤쪽 줄의 충돌, development 값을 운영 충돌로 처리 |
| Docker runtime 감사 | 확인 가능한 단일 Docker target/final stage와 상속, canonical runtime 필드의 불일치 감지 | 임의 base image 내부 프로그램, 모든 Compose profile/override 조합 자동 선택 |
| 기존 source 충돌 감사 | 같은 컨테이너 scope의 기존 충돌 및 일부 shadowed/nested/mutated receiver 관측을 별도 검토 | 정적 추출기 전체의 의미적 무결성 보증, 저장소 전체의 반대 근거 부재 |

불변 문자열 연결로 얻은 `GET /health/ready`는 baseline에 없더라도 검증해 수용한다. `let` 재할당, parameter shadowing, receiver alias mutation, 객체의 외부 호출 escape, 미확인 import, 동적 eval, 조건부 등록은 보류한다. switch/do/for-of 등도 top-level 등록으로 간주하지 않는다.

DB 주장에서는 “pg가 manifest에 선언됨”, “client가 생성됨”, “특정 endpoint를 참조함”, “실제로 접속됨”을 구분해야 한다. 현재 일반 수용 규칙이 확인하는 좁은 dependency 선언을 뒤의 세 단계로 확대하지 않는다. `EVIDENCE_PREDICATE_MISMATCH`는 **해당 제안의 근거 실패**이며 저장소에 그 DB가 절대로 없다는 결론이 아니다.

최종 nginx runtime 누락은 추출기에서 수정되어 container scope의 runtime.name/image를 관측한다. 정상 nginx 사례가 이제 정적 결과에 포함되면 모델의 신규 발견으로 계산하지 않는다. runtime 감사는 오래되거나 손상된 baseline을 주입하는 회귀 테스트로 유지한다.

## 4. 판정, 검토 의견, 실행 영향

| 판정 | 의미 | canonical 결과 처리 |
| --- | --- | --- |
| supported | 지원하는 좁은 규칙으로 원문 관계를 확인 | suggested 출처를 유지하며 병합. reason은 검증기의 제한된 선언 요약으로 교체 |
| rejected | 무관한 근거·잘못된 관계·허용되지 않는 의미 확대 | 해당 제안을 제외. 기존 정상 관측 전체를 차단하지 않음 |
| deferred | 원문·규칙·선택 조건·관계가 부족 | scalar는 unknown, collection은 제외하고 검토 기록 보존 |

원래 model reason은 raw reply에서 볼 수 있다. canonical 이유에 “접속 성공”, “필수 DB”, “테스트 완료” 같은 검증되지 않은 설명을 통과시키지 않는다. 코드에 값이 선언됐다는 것과 그 값이 운영에서 성공했다는 것은 다른 주장이다.

모델 `reviewFindings`의 category는 documentation_mismatch/runtime_stage_mismatch/source_conflict/missing_evidence다. category 자체도 다시 검증한다. 모델이 제출한 blocking이나 verified를 신뢰하는 필드는 없다. sidecar의 `blocking`과 checked reason은 플랫폼이 작성한다. 문서 불일치는 확인된 운영 경로를 유지하는 advisory이며, 필수 실행 관측의 확인된 충돌은 blocking이다.

새 모델 질문은 기존 source/config 의무와 일치할 때 그 의무의 검증된 이유로 보존한다. 근거 없는 추가 질문·coverage 설명·optional unknown collection을 canonical 차단 사유로 사용하지 않는다. 실제 누락된 필수 포트·명령 같은 baseline 의무는 유지한다. 커널의 자체 audit과 모델이 제안하고 검증된 finding은 origin으로 구분한다.

### 제한된 의무 해소

`resolvedObligations`는 현재 신규 Express route 보완에만 적용된다. 기존 unresolved의 key가 `runtime.httpRoutes`, reason이 `Express route path cannot be resolved statically`인 경우, **그 unresolved 항목의 evidenceIds만으로** 동일한 method/path/component를 다시 확인한다. source 항목의 digest를 기록하고 원본 bundle은 수정하지 않는다.

같은 key/reason의 unresolved가 모두 해결된 경우에만 그에 대응하는 canonical 질문을 제거한다. 한 route가 확인됐다고 다른 env 기반 route를 해결하지 않는다. 여러 등록 호출이 같은 줄 evidence를 공유하여 구분되지 않으면 의무를 해소하지 않는다. 이는 일반적인 미해결 항목 자동 삭제나 모델의 자체 완료 선언이 아니다.

## 5. 실제 API·파일과 보고서

```python
from iris_analyzer.result import validate_analysis_with_report, validate_analysis
from iris_analyzer.verification import verify_proposals

# 내부 kernel: 구조/identity 검사 뒤에 사용
admitted_reply, verification = verify_proposals(canonical_reply, bundle)

# 공개 병합 경계: 기존 v1 구조·identity·detected 검사와 kernel을 모두 수행
result, verification = validate_analysis_with_report(canonical_reply, bundle)
result = validate_analysis(canonical_reply, bundle)  # 같은 gate, 결과만 반환
```

- `opencode/review_protocol.py`: v2 변경 제안 schema와 현재 source context에 대한 canonical 변환.
- `opencode/runner.py`, `prompts/deployment.txt`: 실제 baseline/메타데이터 입력, 응답 검증, wire 원문 보존.
- `verification/source.py`: 불변 원문 확보·digest/마스킹 검증·선택 범위 내 재추출.
- `verification/rules/express.py`, `rules/findings.py`: 제한된 AST 관계 증명과 독립 baseline/review 감사.
- `verification/kernel.py`: 제안별 판정·제외·검토·제한된 의무 해소.
- `result.py`: public validation, static 관측 보존, 검증된 blocking 적용, 최종 결과 계산.
- `pipeline.py`: AI/static 공통 검증, context 확장, 결과와 sidecar 저장.
- `judgment_evaluation.py`: baseline·raw 제안·검증 후 결과·reviewed gold를 분리한 평가.

현재 sidecar의 버전은 `iris.analysis-verification.v1`, verifier는 `iris.semantic.v1`이다. sourceSnapshotId/contextHash/baselineDigest/proposalDigest/resultDigest를 기록한다. decisions에는 fieldPath, supported/rejected/deferred, ruleId, reasonCode, evidenceIds, proposedDigest, inspectedPaths, supportingLocators, missingObligations가 들어간다. 질문·coverage 검토처럼 source field가 아닌 판정에는 source locator가 없을 수 있다. reviewFindings는 checked reason, origin, blocking, 필요한 인용 위치·기존 obligationKeys를 보존한다.

pipeline 산출물은 `analysis-result.json`, `verification-report.json`, `run-report.json`이며 revision별 `model-input.json`, `model-response.json`, v2 사용 시 `model-wire-response.json`을 보존한다. 모델 원문을 지워 검증기가 차단한 오류를 숨기지 않는다. `detected/suggested`는 발견 출처, `supported/rejected/deferred`는 검증 판정으로 서로 다른 축이다.

**신뢰 경계:** hash는 변경·입력 혼합을 검출하며 인증을 대신하지 않는다. 외부가 작성한 sidecar의 supported 값만으로 수용 권한을 부여하지 않는다. 공개 분석 경계는 커널을 다시 호출한다. standalone v1 결과를 받는 downstream 통합에서는 검증된 worker 산출물의 출처를 함께 유지해야 하며, 오래된 v1 JSON만 전달했다고 새 의미 검증을 통과한 것으로 취급하면 안 된다. 모든 외부 저장소·캐시·독립 deployment consumer에 인증된 검증 영수증 체계가 완성됐다고 주장하지 않는다.

## 6. 평가와 남은 확장

[판단 평가 보고서](../reports/ai-judgment-evaluation.md)가 실제 실행 조건과 결과의 기록이다. 모델/provider/effort, prompt/protocol/rule 버전, 입력 hash, raw/수용된 주장, 실패·지연·usage·비용을 함께 확인한다. 이 문서에는 진행 중인 유료 평가의 미확인 최종 호출 수나 정답률을 기재하지 않는다.

정적 재출력은 신규 발견이 아니다. 이미 정적으로 해결된 사례는 새 발견 기회에서 제외하고, verifier 자체 발견도 AI TP로 계산하지 않는다. raw의 잘못된 주장, verifier에 차단된 주장, 최종 결과로 빠져나간 주장, 지원되지 않아 보류된 주장을 구분한다. 의미 규칙의 지원 실패와 실제 세계에서 거짓이라는 판정도 동일시하지 않는다. 분모가 0인 precision/recall은 N/A다.

현재 corpus와 별도의 holdout·변형 테스트는 회귀와 제한된 모델 평가의 출발점이다. 동일 사례 반복 호출의 안정성, 미관측 프레임워크·언어, 복잡한 module/router/조건부 설정은 별도 범위로 확장해야 한다. 제한된 사례에서 실패가 없다는 결과는 일반 정확성이나 production 적합성 보증이 아니다. 새 실험은 공유 예산에 먼저 예약하며 승인된 한도를 임의로 변경하지 않는다.

향후 일반화할 대상은 DB client→connect→endpoint의 세밀한 predicate, 모듈 간 router/상수 관계, 더 넓은 실행 선택/override 검증, 소비 claim별 인증된 downstream 검증 경계다. 초기 설계에 등장한 별도 `iris.analysis-claims.v1`, 범용 `verify_claims`/`require_execution_support` API가 현재 구현된 계약이라고 혼동하지 않는다. 현재 운영 계약은 v2 wire 변경 제안과 canonical v1 결과, 검증 sidecar다.

## 7. 빌드·개선 에이전트 경계

Dockerfile이 없는 소스는 서비스 담당 Railpack 경로로 인계한다. 분석기 Dockerfile 자동 생성은 build-preparation v2에서 제거했다. 기존 Dockerfile과 소스 archive의 무결성은 빌드 준비 단계에서 확인하지만, 준비 완료가 실제 Railpack 지원·이미지 빌드·ECR push·배포 성공을 의미하지 않는다. 전달된 Git SHA는 caller의 고정 commit 확인 책임과 별도 content manifest를 함께 사용한다.

검증된 빌드/실행 실패와 코드 하자를 로그 기반 개선 에이전트에 넘기는 조건은 [개선 인계 설계](remediation-handoff.md)에 정리돼 있다. 실제 repair dispatcher·자동 patch 반복·재배포 루프는 구현된 것으로 보고하지 않는다. source review 결과만으로 실제 실패 로그나 수정·배포 권한을 만들어내지 않는다.
