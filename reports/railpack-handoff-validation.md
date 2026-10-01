# 서비스 소유 Railpack 인계 v2 검증 · 2026-10-01

팀 서비스 담당의 결정에 맞춰 **Dockerfile이 없을 때 분석기에서 Dockerfile을 생성하는 경로를 제거**했다. 기존 파일과 분석 결과를 인계하고, 서비스가 기존 Dockerfile 또는 Railpack으로 실제 빌드를 수행한다. v1 생성 이미지 평가 기록은 과거 검증으로 보존한다.

## 구현 경계

- 요청/응답은 `iris.build-preparation-request.v2` / `iris.build-preparation.v2`다. v1과 `allowGeneration`은 거절한다.
- `buildHandoff`는 owner=service, recommendedBuilder, requestedBuilder, decisionRequired, reasonCode를 제공한다.
- auto 추천은 서비스에 저장된 builder를 덮어쓰지 않는다. 명시한 Railpack을 Dockerfile 감지로 바꾸지 않고, 명시한 Dockerfile이 없다고 Railpack으로 자동 변경하지 않는다.
- ready는 원본 아카이브 준비를 뜻한다. 실행 권한은 false이며 Railpack 지원 여부·이미지 빌드·ECR push 성공을 의미하지 않는다.
- 원본 모든 파일·실행 비트·manifest·근거·아카이브 해시를 WAS가 독립 검증한다. 생성 Dockerfile을 포함해 모든 추가 파일을 거절한다.

## 실제 WAS → 분석기 호출

| 사례 | 결과 | 확인 |
| --- | --- | --- |
| Temp_log · auto · 기존 Dockerfile | ready / dockerfile 추천 | 원본 Dockerfile 포함, 서비스 결정 필요 |
| portfolio · auto · Dockerfile 없음 | ready / railpack 추천 | Dockerfile 생성 없음, 소스 하자 취급 없음 |
| Temp_log · 명시적 Railpack | ready / railpack | 선택 보존, 원래 Dockerfile도 원본 파일로 보존 |
| portfolio · 명시적 Dockerfile | needs_input / dockerfile | 자동 전환·생성 없음, archive 미제공 |

두 사용자 제공 소스를 실제 WAS CLI로 분석기에 전달했다. ready인 세 아카이브의 각 파일이 원본에 존재하고 바이트가 일치함을 확인했다. 상세 source SHA·manifest·archive digest 및 인계 필드는 [JSON](railpack-handoff-validation.json)에 있다. 클라우드 호출·유료 모델 호출·실제 Railpack 빌드는 각각 0회다.

## 테스트

- 분석기 Python3.11·3.13 각각 **553 passed**, 비용 없는 native OpenCode 검사 포함.
- WAS **244 passed / PostgreSQL 전용8 skipped**(로컬), Ruff/format/strict mypy 통과. PR CI에서 DB·마이그레이션도 실행한다.
- v2 빌드 준비 회귀 **40개**: 요청 버전, 명시 선택 보존, 후보 모호성, 경로·byte 무결성, 누락 파일, legacy generation 거절 등을 검증한다.
- 개선 인계 draft 계약 회귀 **15개**: 로그 필수, 원인 미확정 patch 금지, 비수정 대상 분류 제외, 비밀 본문/외부 URL/경로/권한/무제한 회차 거절 등 표현 가능한 형태를 검사한다.

## 재귀 개선 계약 상태

[로그 기반 개선 인계](../docs/remediation-handoff.md)는 source/run/설정/검증 receipt와 마스킹한 로그를 묶는 설계다. 원인 미확정은 diagnose_only, 검증된 코드 하자는 propose_patch로 구분한다. Dockerfile 부재·설정 누락·인프라 장애를 소스 하자로 자동 분류하지 않는다.

현재 JSON Schema 통과는 trusted receipt의 진위나 원인의 정확성을 보장하지 않는다. 실제 receipt resolver, 의미 검증기, WAS 큐 dispatch, 오류 에이전트 consumer와 제한된 재빌드 loop는 구현 전이다. 이 테스트를 재귀 개선 E2E 또는 자동 수정 성공으로 표시하지 않는다.
