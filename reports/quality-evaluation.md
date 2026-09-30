# Iris 분석 모듈 품질 평가 · 2026-10-01

전처리·OpenCode 어댑터·결과 검증·worker용 pipeline과 CLI를 구현했습니다. 최종 설정으로 사용자 제공 두 저장소를 각각 두 번 분석했으며 4회 모두 구조화 응답, 근거 검사와 결과 품질 기준을 통과했습니다. 최종 상태는 모두 complete입니다.

## 평가 대상과 결과

| 대상 | 실행 구성 | 실행 런타임 | 정적 기준 | 최종 실제 모델 |
|---|---|---|---|---|
| Temp_log | client/server를 합친 Web/API 앱 1개, MongoDB와 볼륨은 의존성 | node / container | 22개 검사 통과, HTTP 경로 21/21 | 2/2 성공, complete |
| portpolio-production | 정적 앱 1개, Vite 빌드와 nginx 실행 분리 | nginx / container | 15개 검사 통과, 가공한 API 없음 | 2/2 성공, complete |

두 입력의 canonical bundle와 contextHash를 반복 생성해 동일함을 확인했습니다. Temp_log 입력은 137,102 bytes, 선정 파일 68개, 근거 162개입니다. 포트폴리오는 28,888 bytes, 선정 파일 16개, 근거 18개입니다. 둘 다 180,000 bytes 기본 한도 안에 들어갑니다.

Temp_log의 컨테이너 4000, Vite 개발 5173, 호스트 기본 매핑 8080을 구분했습니다. 런타임 작업 디렉터리 /app/server와 node dist/index.js, 세 헬스 경로, 라우터 mount를 조합한 로그인·내 정보 경로, MongoDB·업로드 볼륨, 프론트 /api 연결을 확인했습니다. 포트폴리오의 빌드 산출물 dist와 컨테이너 8080/호스트 4387도 확인했습니다.

HTTP 경로 precision/recall/F1은 Temp_log에서 모두 1.0입니다. 포트폴리오는 실제 예상 API가 없는 경우이므로 빈 집합 일치로 처리했습니다. 최종 결과의 서비스 필드와 bundle 관측값을 각각 검사해, bundle은 맞고 결과의 작업 디렉터리만 잘못된 경우에도 평가가 실패하도록 했습니다.

## 실제 모델 설정과 의미

- OpenCode 1.18.33, 실제 /doc 명세와 effective 모델 설정 검증.
- Hive hive-ai / zai-org/glm-5.3-flash, temperature 0, effort low.
- 네이티브 response_format=json_object와 애플리케이션의 제안 스키마·근거 검증.
- 출력 상한 8,192, 호출 상한 8, 평가용 누적 토큰 상한 750,000, 원격 모델 재시도 0.
- 최종 네 호출 모두 formatRecovery=false이며, 원시 JSON을 보정하지 않고 검증했습니다.

모델은 이미 감지한 관측값을 다시 생성하지 않는 보완 방식입니다. 원시 응답은 변경하지 않은 서비스·경로·환경변수·의존성을 생략했고, 검증기가 정적 관측값을 복원했습니다. 따라서 이 정확도는 최종 분석 결과의 자동 검사 기준이며 LLM이 모든 사실을 독립적으로 재발견했다는 수치가 아닙니다. 근거 ID가 유효하다는 사실만으로 추천의 의미가 모두 옳다고 보장하지 않습니다.

## 실패 이력과 개선

| 설정 단계 | 성공/실패 run | 확인한 원인 |
|---|---|---|
| 초기 전체 재진술 | 0/4 | 출력 예산 소진 및 잘못된 detected 해석 거절 |
| steps=1 | 2/2 | OpenCode가 첫 요청에 MAX_STEPS 요약 지시 삽입 |
| steps=2 | 3/1 | 한 호출이 추론에 출력 예산을 모두 사용 |
| low effort + temperature 0 | 2/2 | Temp_log 응답의 추가 설명·관측값 재진술 |
| 네이티브 JSON + 제안 전용 스키마 | 4/0 | 모든 검사 통과 |
| 최종 런타임/작업 디렉터리 투영 | 4/0 | 현재 코드의 모든 검사 통과 |

실패 run과 실제 model 호출 수는 다릅니다. 입력 확장으로 한 run이 두 모델 호출을 수행한 경우가 있습니다. 모든 기록의 세션·메시지 ID, 사용량·지연·오류를 [model-iterations.json](model-iterations.json)에 보존했습니다. 잘린 응답과 여러 JSON 객체는 거절합니다. 형식 보정이 필요한 provider에서는 한 개의 명확한 완성 JSON만 추출하고 보정 여부와 원문을 기록하며, 동일한 스키마·근거 검증을 유지합니다.

Hive의 필수 StructuredOutput 도구 요청과 네이티브 json_schema는 실제 HTTP 400을 받았습니다. JSON object 모드는 실제 API probe와 OpenCode의 네이티브 요청 검사에서 확인했습니다. low 파라미터 수락과 정확한 내부 추론 강도의 보장은 구분합니다.

## 테스트와 패키지 검증

Python 3.11.4와 3.13.9에서 각각 **242개 테스트 전부 통과**했습니다. 실제 고정 OpenCode 서버 테스트도 포함했고 skip/failure/error는 0입니다. 분기 포함 종합 커버리지는 **86.52%**, 문장 커버리지는 89.07%, 분기 커버리지는 81.36%입니다. Ruff lint/format, uv.lock 검사와 wheel 빌드도 통과했습니다. [verification.json](verification.json)에 상세값을 저장했습니다.

검사는 불변 스냅샷과 동시 참조 수, symlink/경로 이탈, 키 파일·실제 .env 제외, Docker/Compose/명령 인자/URL 비밀값 마스킹, 추가 파일의 전체 줄 제공과 예산 거절, TS .js→.ts/alias, Express mount·배열 경로, 동일 빌드 context의 별도 실행 단위, 포트 충돌과 미지원 저장소 상태를 포함합니다. 테스트 fixture의 manifest가 배포 서비스로 오인되거나 유효한 소스를 망가뜨리지 않도록 구분했습니다.

모델 transport mock과 실제 고정 서버의 /doc·권한·설정 검사, localhost 합성 SSE 공급자로 네이티브 요청을 검사했습니다. 이 네이티브 테스트는 외부 모델 비용이 발생하지 않습니다. 실제 Hive 호출은 별도 평가 기록입니다.

## 사용량과 비용

최종 평가의 보고 사용량은 input 63,000, output 697, cache read 62,976, total 126,673 tokens입니다. 최종 네 호출 추정 비용은 USD0.00389825입니다. 개발 중 기록한 사용량 기준 누적 추정은 USD0.04907649, 미확인 사용량 예약은 USD0.680300입니다. 합계 USD0.729376는 실제 청구액이 아닙니다. reasoning=0은 OpenCode가 반환한 구분값이며, Hive의 내부 추론이 없었다는 뜻으로 해석하지 않습니다.

[Hive 공식 가격](https://thehive.ai/models/zai-org/glm-5.3-flash)을 2026-10-01 확인했습니다. 100만 토큰당 입력 USD0.05, 출력 USD0.17, 캐시 읽기 USD0.01을 사용한 추정입니다. 원격 provider의 실제 청구액은 제공받지 못했으므로 null입니다. OpenCode가 알 수 없는 모델 가격을 0으로 채운 값을 무료 호출로 보고하지 않았습니다.

사용량을 보존하지 못한 초기 capability probe는 보수적인 예약액을 남겼습니다. 제출되지 않은 요청과 중복된 과거 기록의 예약은 감사 기록을 유지하며 정정했습니다. 상세값은 [budget-summary.json](budget-summary.json)에 있습니다. 로컬 공용 ledger는 프로세스 간 예약을 막지만, 별도 worker/서버/저장소의 전역 원화 예산은 플랫폼의 공용 예산 저장소에 연결해야 합니다.

## 인계 범위

공개 함수·스키마·CLI와 고정 fixture 정답은 저장소에 포함했습니다. 원본 평가 프로젝트와 .env, 모델의 전체 소스 입력은 Git에 추가하지 않았습니다. 로컬 artifacts에는 실제 model-input/model-request/model-response, revision별 근거와 run-report를 보관했습니다.

complete는 deployment_v1 필수 정보가 채워졌다는 뜻입니다. 원본 프로젝트의 빌드·실행·배포를 수행한 결과가 아닙니다. FastAPI HTTP 계약, PostgreSQL 저장, 소스 전달·환경변수 등록·worker 통신은 팀의 플랫폼과 연결할 부분입니다. 공개 경로와 인증은 이 결과만으로 확정하지 않습니다. 초기 지원은 Node workspace/Vite/Express/Docker/Compose이며 다른 언어는 unsupported 또는 명시적 확인 항목을 반환합니다.

이 평가는 두 저장소의 검토한 기준에 대한 작은 반복 평가입니다. 일반적인 모든 저장소·추천 의미의 정확도나 운영 SLA를 주장하지 않습니다. 재검증 명령과 계약은 [README](../README.md), 정답과 파일 digest는 [ground-truth.json](../evaluations/ground-truth.json)을 참고합니다.
