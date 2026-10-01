# AI 판단·의미 검증 구현 및 실제 평가

2026-10-02. Hive `zai-org/glm-5.3-flash`, reasoning low, temperature 0, native JSON object, OpenCode 1.18.33. **모델 크기를 바꾸지 않고 입력·제안 계약·근거 검증·평가를 함께 개선했다.** 운영 프롬프트는 deployment_v2.1, 모델 응답은 iris.model-review.v2, 외부 AnalysisResult는 v1을 유지한다.

## 요청 네 항목에 대한 대응

| 요청 | 구현·확인한 내용 | 확인 자료 |
| --- | --- | --- |
| 정적 분석 이후 AI의 판단 | baseline·실행 메타데이터를 읽고 관계/실행 조건/누락/충돌을 검토한다. complete이어도 검토하며, 새로운 내용이 없으면 빈 제안이 정상이다. | [운영 프롬프트](../src/iris_analyzer/opencode/prompts/deployment.txt), [v2 계약](../docs/model-review-protocol.md) |
| 항목별 기준 | 서비스·runtime·명령·포트·환경변수·DB/연결·스토리지·probe·사양을 `입력 → 근거 → 허용 결론 → 금지 단정 → 기대 결과`로 정리했다. | [판단 정책](../docs/ai-judgment-policy.md) |
| 충분한 근거·충돌 처리 | 유효한 ID와 의미 지지를 구분한다. 불변 원본의 줄·digest·binding·service/scope·반대 선언을 확인하고 supported/rejected/deferred를 반환한다. README와 실행 설정, 개발/운영 범위를 구분한다. | [검증 규칙과 한계](../docs/semantic-evidence-verification.md), [구성한 반례 재검증](ai-judgment-audit.json) |
| AI의 실제 기여 평가 | 정적 baseline, 모델 원본 delta, 검증 판정, 최종 결과를 비교했다. 기존 관측·어댑터 복사·검증기 자체 발견을 AI의 신규 발견에서 제외했다. 정상·충돌·정보 부족·주입·재할당·shadowing 사례를 반복 평가했다. | [가공용 평가 JSON](ai-judgment-evaluation.json), [원본 응답/판정](ai-judgment-model-evidence.json), [수동 검토](ai-judgment-manual-review.md) |

## 실제 모델 결과

주 평가 11사례 중 **구성한 검증기 반례 1개는 유료 모델 평가에서 제외**했다. 나머지 10사례와 별도 5사례를 각각 3회 평가했다. 45회는 논리적인 평가 실행 수이며, 필요한 파일 확장 때문에 실제 모델 호출은 68회다.

| 평가 | 파이프라인 성공 | 새 경로를 발견하고 근거 검증 통과 | 새 문서 불일치 검증 통과 |
| --- | ---: | ---: | ---: |
| 주 평가 10사례 × 3회 | 30/30 | 3/3 양성 실행 | 3/3 해당 실행 |
| 별도 5사례 × 3회 | 15/15 | 3/3 양성 실행 | 해당 양성 사례 없음 |

**성공 45/45는 모델 답변 100% 정확도를 뜻하지 않는다.** 새 값 제안 7건을 수동으로 보면, 6건은 올바른 조합형 route이고 1건은 변수 가림과 외부 입력을 무시한 잘못된 무조건 route 제안이었다. 잘못된 제안은 `LEXICAL_RELATION_UNRESOLVED`로 보류되어 최종 apiRoutes에는 들어가지 않았다. 정확한 값을 반복한 port 3000 제안 3건은 신규 발견에서 제외했다. 이 중 1건은 인용이 부족해 보류됐다. unknown/null 포트는 값 제안과 별도로 집계한다.

- **추가 발견:** 정적 분석에 없던 immutable const 문자열 조합의 `/health/ready`, `/ops/alive`를 원본 선언·Express receiver·등록 호출까지 검증했다. 일치하는 기존 route 미해결 항목만 해소했다.
- **정상 결과 유지:** 이미 관측한 런타임·포트·DB 선언을 새 발견으로 세지 않았다. Node 빌드와 최종 nginx 런타임은 추출기 수정으로 이미 분리되므로 모델의 빈 제안이 정상이다.
- **적절한 보류:** 재할당·shadowing·외부 PORT·DB import만 존재하는 사례에서 검증되지 않은 실행 사실은 확정하지 않았다. 일부 사례에서 모델의 답이 틀려도 최종 결과의 보류는 유지됐다.
- **남은 누락:** 설정 파일을 먼저 요청한 후 entrypoint의 binding까지 필요해진 사례는 기본 확장 1회 한도 때문에 포트가 끝까지 unknown이다. 파일 요청 기준 통과를 분석 완성으로 세지 않는다.
- **검증 규칙의 한계:** 모델이 지적한 빈 lockfile와 `npm ci`의 문제처럼 소스상 타당한 추가 검토 의견도 현재 지원 규칙 밖이면 canonical 결과에 반영하지 못한다. 모든 코드 하자를 검증하는 엔진은 아니다. 세부 자연어 판정은 수동 검토 자료에 남겼다.

자동 summary의 unsupportedProposalCount는 rejected만 센다. 따라서 이 값이 0이어도 **deferred된 잘못된 제안 1건이 존재한다.** humanReviewCount 50은 원본 설명과 새 의견의 검토 대상을 뜻하며 오류 50건을 의미하지 않는다. 자동 gate는 검토한 정답 항목만 평가한다.

별도로 자연어 finding 21건 중 2건은 관찰을 runtime_stage_mismatch로 과도하게 분류해 거절됐다. 질문 10건에도 범위가 지나치게 넓거나 소스 확인보다 사용자 설정 질문을 먼저 한 사례가 있다. 수동 검토는 독립 코딩 에이전트가 수행했으며 외부 사람의 블라인드 감사는 아니다.

## 이전 방식과 비교 및 실패 이력

| 단계 | 평가 실행 성공 | 주 양성 route | 문서 불일치 |
| --- | ---: | ---: | ---: |
| 보관 v1.3 프롬프트 + 전체 결과 응답 계약 | 28/30 | 2/3 | 0/3 |
| 중간 v1.6 프롬프트 + 전체 결과 응답 계약 | 26/30 | 0/3 | 2/3 |
| 최종 v2.1 프롬프트 + delta 응답 계약 | 30/30 | 3/3 | 3/3 |

v1.6은 일부 의미 검토가 개선됐지만 detected 복사·필수 필드 누락으로 전체 성공률이 악화됐다. route의 container scope, component에 파일명을 쓰는 오류도 실제로 나왔다. 형식을 완화해 통과시키지 않고 모델의 책임을 원자적 delta로 줄였으며, route scope와 component 의미를 명시했다. 출처·최종 상태·기존 서비스 형식은 서버가 관리한다. 기본 v2는 이전 전체 결과 응답을 수용하지 않는다.

이 비교는 **프롬프트와 전송 계약을 함께 변경한 결과**이며 프롬프트만의 효과나 모델 규모 차이로 해석할 수 없다. 같은 현재 전처리·의미 검증 조건에서 보관 프롬프트를 실행했다. 별도 검증 집합도 개발 과정에서 결과를 확인했으므로 완전히 가려진 미사용 벤치마크는 아니다. 사례당 3회라는 작은 범위이며 일반 저장소의 정확도를 추정할 표본이 아니다.

초기 파일럿은 일부 선택 파일에 누락된 줄을 요청할 수 없던 입력 문제, 문자열 route 출력, scope 오류를 드러냈다. `hive-pilot`, `hive-pilot-v15`, `hive-pilot-v16`, `hive-v2-pilot` 기록은 로컬 artifacts에 남겼다. 보관 v1.3 및 중간 v1.6의 실패 실행도 평가 JSON에서 삭제하지 않았다. 실제 모델 오류와 사람이 구성한 검증기 공격 사례는 구분했다.

## 제공된 두 저장소 및 회귀 검증

검토한 원본 파일 digest를 유지한 `tested_code/Temp_log`, `tested_code/portpolio-production`을 재평가했다.

| 저장소 | 정적 재평가 | 최종 Hive 재평가 | 검토한 주요 기준 |
| --- | ---: | ---: | --- |
| Temp_log | 2/2 실행 통과 | 1/1 실행 통과 | 22/22 기준, 내부 HTTP 경로 21/21 |
| portpolio-production | 2/2 실행 통과 | 1/1 실행 통과 | 15/15 기준, 기대 내부 API 경로 0개 |

이 점수는 정적 관측 보존과 정해진 필드의 정확도이며 모델의 신규 발견 점수가 아니다. 이번 재평가에서 소스를 빌드하거나 배포하지 않았다.

- Python 3.11·3.13: 각각 **677 passed**, 비용 없는 native OpenCode 검사 포함.
- 분기 포함 coverage **86.90%**, Ruff, Python 포맷 검사, diff 검사, wheel 빌드 통과.
- 독립 검토에서 발견한 JS/Python escape 해석 차이를 제한된 JS decoder로 수정했다. 미지원 escape는 보류하며 회귀 12개를 추가했다. 이는 모델이 찾은 하자가 아니다.
- 최종 live 호출 후 escape 검증과 전체 요청 비용 예약을 강화했다. 평가의 단순 literal fixture 의미에는 변화가 없으며 전체 테스트를 다시 실행했다. [최종 구현 digest](ai-judgment-implementation-digests.json)에 변경 파일을 기록했다.

## 비용·재현·연결 범위

사용자가 승인한 **누적 상한은 USD 20**이다. 과거 호출과 미정산 예약을 보존한 ledger의 계상액은 **USD 0.96030093**, 작업 시작의 USD 0.91283259 대비 증가분은 USD 0.04746834다. 최종 45회 평가의 사용량 기반 추정 비용은 USD 0.01421885이며 두 실제 저장소와 파일럿·비교 호출은 별도다. **실제 청구액은 제공되지 않아 null**이다. 기본 제품 예산은 변경하지 않았다.

```sh
uv run python scripts/evaluate_ai_judgment.py --live --env-file ../.env \
  --provider hive-ai --repetitions 3 --max-cost-usd 20 \
  --max-output-tokens 8192 --max-total-tokens 1500000 --max-model-calls 150 \
  --opencode-executable /path/to/opencode --out artifacts/ai-judgment/new-run
```

별도 사례는 `--corpus evaluations/ai-judgment-holdout.json`, 이전 방식은 `--prompt-file evaluations/prompts/deployment_v1.3.txt`를 추가한다. 출력 폴더는 새 이름을 사용한다. raw wire·canonical 응답·context·verification·스코어를 함께 보존하며, 비용 ledger는 공유한다.

WAS에 넘기는 분석 결과 v1과 verification sidecar는 준비됐다. Dockerfile 없는 빌드는 서비스 담당의 Railpack으로 넘긴다. 실제 로그 기반 개선 agent dispatcher·자동 수정/재실행·배포 승인은 [인계 계약 초안](../docs/remediation-handoff.md)의 후속 범위다. 이 평가가 그 연결이나 운영 배포의 완료를 증명하지 않는다.

## 동료에게 전달할 답변

> 말씀 주신 네 항목을 기준 문서뿐 아니라 운영 프롬프트·검증기·실제 평가까지 반영했습니다. 정적 결과와 실행 메타데이터를 AI에 주고, AI는 추가 제안만 내도록 분리했습니다. 근거 ID가 있어도 원본의 관계·서비스·scope가 결론을 지지하지 않으면 거절하거나 보류합니다. 항목별 기준은 요청하신 다섯 열 형식으로 정리했습니다. 같은 Hive 모델로 정상·충돌·정보 부족 등 15사례를 각 3회 평가했고, 정적 분석이 놓친 서로 다른 경로 2개를 각 3회, 문서 불일치 1사례를 3회 검증했습니다. 모델이 잘못 제안한 경로 1건도 확인했으며 최종 결과에서는 차단됐습니다. 미지원 규칙 때문에 유효한 의견을 보류한 경우와 파일 확장 한도로 남는 unknown도 보고서에 공개했습니다. Railpack 빌드는 서비스 담당 경계로 유지했고, 자동 수리 agent 연결은 별도 후속 작업입니다.
