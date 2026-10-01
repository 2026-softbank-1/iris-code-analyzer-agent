# GitHub 리뷰 테스트 프론트 검증 · 2026-10-01

독립 로컬 프론트에서 GitHub 링크를 받아 커밋을 고정하고 소스를 내려받아 분석한 뒤 화면에 결과를 표시했다. 실제 Hive GLM 리뷰는 두 샘플 모두 complete였고, 모델 응답 보정 없이 근거·정적 관측값 검증을 통과했다. 기존 분석 품질 평가의 기준을 변경하지 않았다.

| 대상 | 고정 커밋 | 실제 AI 분석 | 확인한 실행 구조 |
| --- | --- | --- | --- |
| monitor5/Temp_log | `54fa8072d9fbf817e3352f8eeee2344f37c8a813` | 서비스 1 / HTTP 21 / 환경 키 20 / 질문 0 | Node Web/API, `/app/server`, container 4000 / development 5173 / host 8080 |
| monitor5/portpolio-production | `f54230346fbf` (전체 SHA는 JSON 보고서) | 서비스 1 / HTTP 0 / 환경 키 0 / 질문 0 | Vite build → nginx runtime, container 8080 / development·host 4387 |

포트폴리오의 workingDirectory는 소스 관측이 없어 unknown/null로 유지했다. complete는 해당 프로필의 필수 항목 충족을 의미하며 모든 선택 항목의 값이나 실제 실행 성공을 의미하지 않는다.

Hive `zai-org/glm-5.3-flash`, OpenCode 1.18.33, JSON text + native json_object, temperature 0, reasoning low를 사용했다. 두 실행의 pipeline 소요 시간은 6.2036초 / 5.5452초였다. 사용량은 각각 52,388 / 10,906 tokens이며 추정 비용은 USD 0.00264052 / 0.00056450, 합계 USD 0.00320502다. 공급자 실제 청구액은 null이다. API 상태 조회 횟수는 모델 추론 횟수가 아니며 paid 분석은 각 1회였다.

검증 결과:

- Python 3.13: 264 passed / 선택적 native runtime 2 skipped; native OpenCode 검사 별도 2 passed
- Python 3.11: 264 passed / 선택적 native runtime 2 skipped
- 최종 다운로드 endpoint 변경 후 관련 테스트 14개를 양쪽 Python에서 재검증
- branch 포함 합산 coverage는 JSON 보고서에 기록 (85.64%)
- 두 tested_code의 정적 품질 평가: 4/4 성공, 실패 0
- 실제 WAS envelope/enums 계약 검증 성공; WAS 자체 테스트 14개와 ruff/mypy 성공
- ruff, Python formatting, JavaScript syntax, wheel 빌드와 HTML/CSS/JS 포함 확인

브라우저에서 정적/AI 리뷰, 진행 표시, 파일 근거 모달, 새로고침 후 현재 리뷰 복원을 확인했다. 320/390/1280px에서 가로 넘침을 확인하고 모바일 긴 값의 레이아웃을 수정했다. JSON은 서버 attachment로 다운로드하여 파일 내용이 서버 결과와 일치함을 확인했다. 화면 캡처와 로컬 산출물은 `artifacts/review-demo/`에 남겼다.

자동 HTTP 테스트는 고정 fixture 다운로드를 대체하고 실제 offline pipeline을 실행한다. 실제 GitHub 다운로드와 Hive 모델 연결은 위 수동 브라우저 검증으로 보완했다. 이 보고서는 프론트와 분석 모듈의 검증이다. WAS의 PostgreSQL 저장, 배포 큐, CodeBuild/ArgoCD 실행은 검증 범위에 포함되지 않았다.

측정값: [review-demo-validation.json](review-demo-validation.json). 실행법: [README](../README.md). 연결 준비: [control-plane-readiness.md](../docs/control-plane-readiness.md).
