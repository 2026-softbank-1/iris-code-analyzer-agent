# 비용·worker 연결·AI 판단 평가·개선 인계

(README에서 원문 이동)

## 비용과 worker 연결

CLI는 기본 `artifacts/model-budget-ledger.json`에 비용을 먼저 예약하고, 제공된 사용량에 따라 추정 비용으로 정산합니다. `--budget-ledger`로 같은 파일을 여러 프로세스에서 공유할 수 있으며 파일 잠금과 fsync를 사용합니다. 기본 누적 상한은 `--max-cost-usd 1.0`입니다. 사용량을 받지 못하면 예약액을 유지합니다. 허용된 원격 재시도와 구조화 모드의 미확인 이전 단계 비용도 보수적으로 유지합니다. 실제 청구액은 알 수 없으면 null입니다.

2026-10-01 [Hive 공식 모델 페이지](https://thehive.ai/models/zai-org/glm-5.3-flash)에서 GLM의 100만 토큰당 입력 USD0.05, 출력 USD0.17, 캐시 읽기 USD0.01을 확인했습니다. 이 가격 스냅샷은 추정용입니다. 다른 모델은 검토한 `--pricing-json` 파일에 input/output/cacheRead 단가를 제공해야 금전 상한을 적용할 수 있습니다. 변경된 공급자 요금은 담당자가 다시 검토해야 합니다.

로컬 파일을 공유하지 않는 worker·서버·별도 저장소는 비용 기록을 공유하지 않습니다. 팀의 프로젝트 총 예산은 플랫폼의 공용 예산 기록과 예약 트랜잭션으로 연결해야 합니다. 이 라이브러리 자체가 프로젝트 전체 청구서를 조회하거나 전역 원화 상한을 보장하지 않습니다.

worker는 `analyze_with_report`에 모델 runner와 Job 이벤트 저장 콜백을 제공합니다. pipeline은 성공·실패 모두 스냅샷 참조를 해제합니다. prepare_context/expand_context를 직접 사용한 호출자는 확장이 끝난 뒤 `release_snapshot(snapshotId)`를 호출합니다. 같은 스냅샷을 쓰는 동시 작업은 참조 수로 보호합니다.

플랫폼의 HTTP /analyze 요청 형식, 소스 전달 방식, 환경변수 등록 및 데이터베이스 저장은 팀과 계약을 맞춰 연결할 부분입니다. 이 패키지의 테스트는 분석 모듈의 테스트이며 전체 배포 시스템의 E2E 완료를 뜻하지 않습니다. 공개 노출 경로와 인증 판단은 확정하지 않습니다. API 경로는 코드 내부에서 관측한 경로이며 공개 라우팅과 운영 정책은 후속 계획에서 결정합니다.

## AI 판단·근거·기여 평가 기준

[AI 판단 정책](ai-judgment-policy.md)은 각 항목을 입력 → 확인할 근거 → 허용 결론 → 금지 단정 → 기대 결과로 정리합니다. 운영 프롬프트·입력, 의미 검증기, 원본 delta 기반 기여 평가에 반영했습니다. [실제 Hive 반복 평가](../reports/ai-judgment-evaluation.md), [수동 검토](../reports/ai-judgment-manual-review.md), [구성한 검증기 반례](../reports/ai-judgment-audit.json)를 구분해서 제공합니다. 모델 응답 성공·검증 통과·새 사실 발견·실제 배포 성공은 각각 다른 지표입니다.

```sh
uv run python scripts/evaluate_ai_judgment.py --live --env-file ../.env \
  --provider hive-ai --repetitions 3 --max-output-tokens 8192 \
  --max-model-calls 150 --max-total-tokens 1500000 \
  --max-cost-usd 20 --out artifacts/ai-judgment/run
```

위 평가의 USD 20은 이번 작업에서 승인받은 누적 상한이며 라이브러리 기본 상한은 USD 1을 유지합니다. 실제 요청의 프롬프트·스키마·baseline·실행 메타데이터까지 비용 예약에 포함합니다. 보관 프롬프트는 `--prompt-file`, 별도 사례는 `--corpus evaluations/ai-judgment-holdout.json`으로 평가합니다.

## 검증된 문제의 로그 기반 개선 인계

실패 로그·원본 식별자·검증 기록을 묶어 원인 미확정은 진단 전용, 검증된 코드 하자는 수정안 제안으로 넘기는 [개선 인계 설계](remediation-handoff.md)를 추가했습니다. [draft 계약](../contracts/remediation-handoff.v1.schema.json)은 형식 초안이며 실제 오류 에이전트·WAS 큐 연결은 아직 구현하지 않았습니다. Dockerfile 부재·설정 누락·인프라 장애는 코드 하자로 자동 분류하지 않습니다.

1002 분석기·WAS·프론트 연동 변경 및 현재 설계: [1002-integration-design.md](1002-integration-design.md).
