# 분석 → 서비스 빌드 → 로그 기반 재귀 개선 인계

2026-10-01 결정 반영. **분석 에이전트는 근거와 문제를 검증하고, Dockerfile이 없으면 서비스 담당의 Railpack 빌드로 넘긴다.** 분석기가 Dockerfile을 자동 생성하던 이전 초안은 종료한다. 확인된 실패와 코드 하자는 로그 기반 개선 에이전트로 인계하되, 실패 관측과 원인 확정을 구분한다.

구현 상태: 빌드 준비 경계는 v2 코드로 반영한다. 이 문서의 **개선 인계 계약·라우팅·재귀 실행은 설계/초안**이며 아직 오류 에이전트나 WAS 큐에 연결된 실행 기능이 아니다. 확인 시점의 `iris-error-check-agent` main은 README만 제공한다. [의미 근거 검증기](semantic-evidence-verification.md)도 아직 설계 단계이므로 현재 suggested 결과만으로 자동 수정을 승인할 수 없다.

## 1. 책임 분리

| 책임 | 담당 | 출력 |
| --- | --- | --- |
| 전처리·서비스/명령/설정 분석·의심 사항·근거 검증 | 분석 에이전트 | 고정 source identity, analysis/readiness, 검토/검증 기록, 빌드 힌트 |
| 기존 Dockerfile 또는 Railpack 선택, 실행 환경·설정 주입 | 백엔드/서비스 담당 — 김현겸 담당 흐름 | 확정된 builder/config, build run |
| Railpack prepare/BuildKit·이미지 빌드·ECR push·로그 수집 | 서비스 빌드 Worker | image digest 또는 실패 receipt, 단계별 로그·시간 |
| 로그·관련 소스에 근거한 원인 진단·최소 수정안 | 로그 기반 재귀 개선 에이전트 | 진단, patch 후보, 근거, 재검증 요청 |
| 허용된 격리 복사본의 patch 적용·재분석·재빌드·재시험·종료 판정 | 서비스 coordinator + 검증 runner | attempt별 결과, 검증된 후보 또는 중단 사유 |

Railpack은 앱을 분석해 build plan을 만들고 BuildKit으로 이미지를 구성한다. 분석 에이전트가 중간 Dockerfile을 만들어 주는 것을 전제로 하지 않는다. Railpack 버전·provider·build plan·override·이미지 platform은 서비스가 기록한다. [공식 구조](https://railpack.com/architecture/overview), [BuildKit 연결](https://railpack.com/platforms/buildkit-frontend).

```text
고정 소스 → 전처리/분석 → 근거·적용 조건 검증
  ├ 입력 부족/불명확 → 자료·설정 확인 요청
  ├ 검증된 정적 의심 → trusted check/재현 요청 → 실제 검사 로그 확보
  └ 빌드 준비 → 서비스 담당 결정
       ├ 기존 Dockerfile → 해당 파일로 빌드
       └ Dockerfile 없음 → Railpack으로 빌드
                 ↓
          실패 결과·마스킹 로그 수집
                 ↓
          인계 조건/문제 분류 검증
       ├ 환경·권한·인프라 → 서비스/플랫폼 담당
       ├ 원인 미확정 → 진단 전용 인계
       └ 코드 하자 검증됨 → 수정안 제안 인계
                 ↓
       격리 patch 후보 → 새 snapshot → 재분석·재검증·재빌드/시험
       ├ 성공 후보 → 서비스의 기존 검토·승인·배포 경로
       └ 실패 → 새 로그로 제한된 다음 회차 또는 중단
```

## 2. 무엇을 넘기는가

| 입력/상황 | 확인할 근거 | 전달 대상·행동 | 넘기면 안 되는 결론 |
| --- | --- | --- | --- |
| Dockerfile 없음 | 선택한 root와 파일 목록·명시한 builder | 서비스의 Railpack 선택. 정상 경로 | 소스 하자, AI Dockerfile 생성 필요 |
| Secret/PORT/profile 미제공 | 소비 위치·필수성·설정 바인딩 | 서비스 설정 입력 | 모델이 값 추측, 코드를 고쳐 검사를 무력화 |
| 정적 parser warning 또는 모호한 충돌 | 전체 파일 여부·문법 모드·선택 조건·반대 근거 | 분석 보류 또는 trusted compiler/check 재현 | severity=error 또는 evidence ID 존재만으로 코드 하자 확정 |
| 실제 build/test/runtime 실패, 원인 미확정 | 동일 snapshot/config의 실패 receipt와 로그 | `diagnose_only` | 실패 로그가 있다는 이유만으로 소스 patch 허용 |
| 동일 조건에서 확인한 코드 하자 | 실패 로그, 원인에 연결된 소스, trusted finding receipt | `propose_patch` | AI의 확신도·자기평가만으로 verified 설정 |
| registry 권한·네트워크·rate limit·용량 제약 | 단계·오류 코드·리소스/권한 상태 | 서비스/플랫폼 복구·허용된 재시도 | 모든 실패를 코드 버그로 분류하거나 인증 코드를 제거 |

**실패를 관측한 사실과 그 원인을 확인한 사실은 다르다.** 오류 에이전트가 아직 원인을 모르는 실패 로그를 받아 진단하는 것은 가능하지만, 그 단계의 권한은 진단 전용이다. 수정안 생성은 코드 원인이 검증된 다음 회차의 별도 인계로 승격한다.

정적 검사로 직접 확인되는 문제도 인계할 수 있다. 이때 필요한 로그는 실제 trusted checker의 진단 기록이다. 실행하지 않은 빌드 로그를 만들어 넣거나, 마스킹 때문에 깨진 snippet을 구문 오류로 전달하지 않는다. 현재 readiness의 parser 오류는 compiler 확인이 필요하다는 한계를 그대로 유지한다.

## 3. 인계 계약 초안

`iris.remediation-handoff.v1-draft`: [JSON Schema](../contracts/remediation-handoff.v1.schema.json), [예제](../evaluations/remediation-handoff-cases.json).

| 묶음 | 필수 정보 |
| --- | --- |
| identity | handoffId, incidentId, serviceId, idempotencyKey |
| source | repositoryId, commit SHA, snapshot/context/포함 파일 manifest digest |
| analysis | analysis digest, trusted verification receipt 참조 |
| failure | stage, runId, 실행 설정 digest, 실패 fingerprint, classification, causeStatus, exitCode, trusted failure receipt |
| evidence | 마스킹한 로그 artifact 참조·digest·종류·크기, finding/check receipt 참조 |
| repair policy | diagnose_only/propose_patch, 서버가 발급한 권한 receipt, attempt·최대 회차·시간/비용 한도, 허용/보호 경로, 재검증 계획 참조 |

원본 로그 본문·키 값·서명 URL·호스트 임의 경로를 직접 넣지 않는다. 접근 제어가 있는 내부 artifact 서비스의 참조를 사용한다. 실제 로그 producer가 마스킹을 수행하며, 패킷의 `redacted=true`만 믿지 않는다. 로그가 잘린 경우 해당 coverage를 receipt에 기록하고 원인 확정에 필요한 부분이 없으면 다시 수집한다.

**JSON Schema 통과는 인계 승인도, 실제 하자 검증도 아니다.** 인계하는 서비스는 다음을 별도로 확인한다.

1. receipt와 artifact를 신뢰 저장소에서 읽고 해당 service·source·run·설정에 속하는지 검증한다. SHA 문자열은 인증을 대신하지 않는다.
2. analysis/readiness와 failure가 같은 원본·설정에 연결됐는지 확인한다. 새로운 소스의 실패에 이전 receipt를 재사용하지 않는다.
3. diagnose_only에서는 patch를 허용하지 않는다. propose_patch는 코드 하자 원인 검증과 finding receipt가 있어야 한다.
4. 로그가 실제 실패 단계의 자료인지, 마스킹/크기/coverage와 source 근거가 충분한지 확인한다.
5. attempt가 현재 DB 기록과 일치하고 `attempt < maxPatchAttempts`인지, 시간·공유 예산이 남았는지 원자적으로 검사한다. 이러한 교차 기록 검사는 schema만으로 할 수 없다.
6. 허용된 수정 경로, 현재 소스 head, 동시 작업/취소 상태와 권한 receipt를 확인한다.

실제 신규 Job kind·endpoint·DB 상태 enum은 WAS/오류 에이전트 담당과 확정할 통합 계약이다. 이 초안만으로 현재 WAS enum에 새 작업 종류가 생긴 것으로 간주하지 않는다.

## 4. 재귀 개선은 제한된 상태 흐름

`observed → awaiting_evidence | diagnose_only → cause_verified → patch_proposed → candidate_validated → resolved_candidate | retry | stopped`

- patch는 원본을 덮어쓰지 않고 incident/attempt별 격리 복사본에서 검증한다. 원본 base SHA, patch digest, 새 source snapshot을 모두 기록한다.
- 분석 에이전트는 새 snapshot을 다시 분석한다. 이전 검증 기록을 그대로 가져오지 않는다. 변경된 소스·설정이 영향을 주는 claim을 재검증한다.
- 실제 빌드 방식과 Railpack 설정은 서비스가 계속 소유한다. 오류 에이전트가 실패를 피하려고 Railpack을 임의 Dockerfile로 바꾸거나 기반 이미지를 몰래 교체하지 않는다.
- 기존 실패를 재현한 검사와 관련 회귀 검사, source 검증, 빌드·필요한 smoke를 trusted runner가 다시 실행한다. 오류 문자열이 없어지거나 exit 0 하나가 나왔다고 해결로 확정하지 않는다.
- 테스트/assertion/healthcheck/security 규칙을 삭제·완화해 성공시키지 않는다. 새 회귀 테스트 추가는 허용 범위에서 가능하지만 기존 기준 변경은 별도 검토 대상이다.
- source patch와 서비스 설정 변경은 별도 변경안으로 취급한다. Secret·권한·인프라 구성 변경은 소스 수정을 위한 자동 권한에 포함하지 않는다.
- 성공은 `resolved_candidate`다. 원본 저장소 push/merge·배포는 서비스의 기존 승인 흐름이며 인계 패킷의 두 권한은 false다.

초기 정책 제안은 **최대 patch 3회, 진단 재질문 2회**, 명시된 시간·비용 한도다. 아직 실행 설정에 적용한 값은 아니다. 기존 모델 공유 예산과 서비스의 작업 예산을 모두 만족해야 하며 자동으로 한도를 늘리지 않는다.

중단 조건: 동일 normalized 실패의 반복, 동일 patch/tree 재등장, 두 회차 연속 실질 개선 없음, 원인 재분류가 인프라/설정으로 바뀜, 허용 경로 밖 변경, 검증 악화, source head 변경, 예산/시간 소진, 사용자 취소. fingerprint는 timestamp/run UUID 같은 비본질 요소만 정규화하고 코드·포트·설정 차이를 지워 다른 오류를 합치지 않는다.

같은 인계의 중복 전달은 idempotencyKey로 기존 incident/attempt를 조회한다. 새 원본의 동일 오류와 동일 실행의 재전달을 구분하며, lease·attempt 증가·예산 예약은 서비스 DB에서 원자적으로 관리한다. 무한 재귀나 에이전트끼리 직접 재호출하는 구조는 사용하지 않는다.

## 5. 구현/검증 경계

- 반영하는 코드: 분석기/WAS 빌드 준비 v2. AI Dockerfile 생성 제거, 원본 파일만 아카이브, Dockerfile 유무에 따른 서비스용 builder 제안, 명시적 builder 선택 보존.
- 준비하는 설계: 의미 검증 receipt → 실패 분류 → 진단/수정 인계 → 제한된 재검증 loop, draft schema·예제·형식 회귀.
- 남은 실행 연동: 의미 검증기 실제 구현, 신뢰 receipt 저장/검증, WAS 작업 큐와 로그 artifact API, 오류 에이전트 진단/patch consumer, 실제 Railpack/CodeBuild 재빌드 coordinator.

수용 시험에는 최소한 다음을 포함한다: Dockerfile 부재가 repair 인계되지 않음, missing Secret이 patch로 해결되지 않음, 위조·다른 snapshot 로그가 거절됨, 원인 미확정이면 diagnose_only, verified 코드 하자만 propose_patch, 회차/예산 소진 중단, 반복 patch 차단, 기존 테스트 약화 차단, 수정 후 새 snapshot·새 빌드 결과 확인. schema 테스트는 이 중 표현 가능한 형태 제약만 확인하며 실행 loop E2E로 부르지 않는다.
