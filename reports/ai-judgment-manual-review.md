# AI 판단 수동 검토 — Hive v2.1

검토일: 2026-10-02. 독립 코딩 에이전트가 완료된 core 30회와 holdout 15회의 **모델 wire 원문·제공 context·fixture 소스·정적 baseline·검증 결과·최종 결과**를 대조했다. 외부 사람이 실시한 블라인드 감사는 아니다. 상세 판정 66건과 원본 SHA-256은 [검토 JSON](ai-judgment-manual-review.json)에 기록했다. 앱·npm·Docker·클라우드 실행은 이번 검토에서 수행하지 않았다.

## 실제 기여와 잘못된 제안

| 모델이 작성한 값 제안 | 수 | 검증 supported | rejected | deferred | 최종 반영 |
|---|---:|---:|---:|---:|---:|
| 신규 경로, 근거가 충분함 | 6 | 6 | 0 | 0 | 6 |
| 신규 경로, 근거로 단정할 수 없음 | 1 | 0 | 0 | 1 | 0 |
| 기존 포트 3000의 반복 | 3 | 2 | 0 | 1 | 기존 관측 유지, AI 기여 0 |

이외 `unknown/null` 변경 2건은 정보 부족에 대한 보류다. 자동 점수의 `unsupportedProposalCount=0`은 **오답이 없다는 뜻이 아니다.** `deferred`로 차단된 잘못된 신규 제안이 1건 있다. 반복 실행을 포함한 작은 표본이므로 일반 저장소에 대한 정확도 비율로 확대하지 않는다.

- **새 정답:** `/health/ready`와 `/ops/alive`의 상수 문자열 결합 → 직접 Express 등록 관계를 각각 3회 찾아냈다. 추가 파일 요청 후 source 범위의 제안으로 제출했고 검증기가 수용했다. 런타임 성공이나 readiness를 주장하지 않았다.
- **실제 잘못된 제안:** shadowed-route 2회차는 함수 매개변수가 같은 이름의 상수를 가리고, 호출자가 `process.env.PROBE_ROUTE`를 넘기는데도 `/ops/alive`를 고정 경로로 제안했다. “환경변수가 그 상수와 같을 때”라는 설명으로 무조건적인 결과 필드를 정당화할 수 없다. 검증기는 `LEXICAL_RELATION_UNRESOLVED`로 보류하고 경로를 제거했다. 해당 경로가 런타임에서 절대 존재할 수 없다는 판단은 아니다. [원문](../artifacts/ai-judgment/hive-v21-holdout/holdout-shadowed-route/run-02/captured-wire-replies.json), [검증](../artifacts/ai-judgment/hive-v21-holdout/holdout-shadowed-route/run-02/verification-report.json)
- **새 발견이 아닌 반복:** 포트 3000은 이미 정적 관측이었다. 1회는 receiver 근거 부족으로 보류됐지만 baseline의 3000·4000 관측과 충돌 질문은 그대로 유지됐다. 새로운 정답을 잃은 사례로 세지 않는다.

## 자연어 검토 결과

원문 finding 21건 중 19건은 좁게 해석하면 소스로 뒷받침된다. 여기에는 이미 정적으로 발견한 포트 충돌 3건과 줄 위치 설명이 부정확한 1건이 포함된다. 나머지 2건은 **실제 관찰은 유용하지만 `runtime_stage_mismatch` 분류가 과도**하다. web 산출물을 쓰는 연결이 없다는 사실만으로, 루트 Node 서비스만 실행하도록 정한 이미지가 잘못됐다고 단정할 수 없다. 두 분류는 검증기에서 거절됐다.

README와 선택한 Docker 실행 경로의 불일치는 3회 모두 구체적으로 설명됐다. 반면 개발용 README와 운영 Docker 설정이 다른 holdout에서는 잘못된 충돌을 만들지 않았다. 의존성·import만 있는 pg를 실행 중인 DB나 필수 endpoint로 확대하지 않은 판단도 적절했다.

질문 10건에는 다음 보완점이 있다.

- 명시적인 `CMD node index.js`가 있는데 “어느 실행 컴포넌트인지 결정할 수 없다”는 질문은 너무 넓다. 실제 미확정 항목은 web 산출물의 배포 포함 여부다. 해당 질문은 최종 blocker로 추가되지 않았다.
- 소스에 설정 연결이 있는데 필요한 파일을 덜 요청한 뒤 사용자에게 포트를 확인하게 한 경우가 있다. 영구적인 사용자 설정 문제로 분류하기 전에 context를 보완하는 편이 맞다.
- 빈 `.env.example`을 보고 “실제 값이 가려졌다”고 표현한 부분은 과도하다. 확인된 것은 예제 키와 미제공 외부 설정뿐이다.

## 남아 있는 검증 범위

**옳은 경고도 아직 모두 자동 수용되지는 않는다.** nginx fixture 3회차는 `RUN npm ci`와 빈 `{}` lockfile, Vite 의존성 선언의 부조화를 지적했다. 이는 실행 실패를 단정하지 않은 타당한 정적 위험 제보다. npm은 `ci`에 기존 lockfile과 일치하는 의존성 정보를 요구한다고 설명한다. 현재 `source_conflict` 규칙은 이 관계를 지원하지 않아 거절했다. [원문·근거](../artifacts/ai-judgment/hive-v21-final/node-build-nginx-runtime/run-03/captured-wire-replies.json), [검증 결과](../artifacts/ai-judgment/hive-v21-final/node-build-nginx-runtime/run-03/verification-report.json), [npm 공식 문서](https://docs.npmjs.com/cli/v11/commands/npm-ci/)

이 fixture의 nginx/container는 **v2.1 실행 전에 이미 정적 baseline에 포함**됐다. 모델이 nginx 누락을 새 이슈로 보고하지 않은 것은 정상이며, 과거 단계의 누락 기준으로 오답 처리하지 않았다. [당시 baseline](../artifacts/ai-judgment/hive-v21-final/node-build-nginx-runtime/baseline.json)

설정 파일 사례 3회는 초기 평가 기준에 맞게 `release-settings.json`을 요청했지만, `index.js:4`의 초기화 구문을 함께 요청하지 않아 한 번의 확장 후에도 포트 연결이 미확정으로 남았다. 보류 자체는 제공된 정보에 비춰 정확하다. 다만 완결된 분석을 위해서는 **부분 제공된 소비 코드와 설정 파일을 한 배치로 요청하는 정책**과 최종 연결까지 확인하는 평가가 필요하다. [실제 제공 범위](../artifacts/ai-judgment/hive-v21-final/available-config-needs-files/run-01/captured-contexts.json)

파일 요청 23건은 모두 허용된 available/expandable 경로에 한정됐다. gold에 이름이 없는 경로를 요청했다는 이유만으로 오답 처리하지 않았다. import·변수 가림·실제 소비 관계를 확인하는 추가 읽기는 합리적일 수 있다.

자동 평가의 성공·gate 통과는 제한된 계약 준수 결과다. 이 검토는 **6개 신규 정답을 검증해서 수용하고, 1개 잘못된 신규 제안을 차단했으며, 규칙 밖의 유용한 경고와 context 요청 개선점이 남아 있다**는 범위까지 뒷받침한다. 원본 artifact 상대 링크는 로컬 보관 파일이며 GitHub에 원문 전체를 게시했다는 뜻은 아니다.
