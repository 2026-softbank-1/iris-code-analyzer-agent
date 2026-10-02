# 1002 — Organization 기능 로컬 모델 검증

2026-10-02. 사용자 제공 로컬 서버의 **로드된 `qwen3.8-27b-uncensored-mlx`**를 사용했다. 다른 GGUF 모델은 목록에 있었지만 로드하지 않았다. 실제 Organization 다운로드·유료 제공자·이미지 빌드·클러스터 배포·WAS 변경은 없다.

## 결과

| 확인 | 결과 |
|---|---|
| Organization 관련 테스트 | 85 passed |
| 전체 회귀 테스트 | **760 passed, 2 skipped**, 18.06초. 위 85개 포함 |
| 변경 범위 Ruff | passed |
| JSON Schema 출력 | 4개 요청 모두 HTTP 400. 서버 오류: `Unimplemented keys: ["uniqueItems"]` |
| 로컬 모델로 레포부터 전체 분석 | 첫 요청 180.009초 후 ReadTimeout. 이후 요청은 중단. 완료 여부 불명확 |
| 최소 JSON smoke | 정상 `{ "ok": true }`, finish_reason=stop, 67 input/25 completion tokens |
| native API reasoning off | 모델이 reasoning 설정을 노출하지 않아 해당 요청 거절 |
| 정적 레포 분석 + 로컬 시스템 제안만 | 레포 3개 정적 분석 완료. 시스템 제안은 120.013초 후 ReadTimeout |

**코드 회귀 테스트와 작은 JSON 요청은 통과했지만, 이 로컬 모델로 Organization 전체 AI 분석/아키텍처 제안을 완료했다고 주장할 수 없다.** 모델 속도·서버 부하·reasoning/template 설정 중 무엇이 지연을 만들었는지는 이번 결과로 확정하지 않았다. 로컬 호출 실패는 정적 AI 성공으로 대체하지 않고 필요한 질문·실패 상태로 유지했다.

정적 분석 결과는 `catalog-api`, `shop-web`의 서비스 두 개와 `platform-infra`의 비배포 인프라 구성 요소를 구분했다. 이미지 registry, 프론트 container port, build-time `VITE_CATALOG_URL` 및 AI 제안 실패 질문이 남았다. 실제 배포 성공이나 서비스 연결 성공을 뜻하지 않는다.

2개 skipped는 고정 OpenCode 실행파일을 사용하는 실제 서버/네이티브 추론 회귀 테스트다. 이번 로컬 모델 요청은 동일 production 프롬프트·스키마·검증기를 **직접 Chat Completions 테스트 transport**로 호출했다. OpenCode 프로토콜 자체의 호환성 검증과 구분한다.

## 재현

```sh
uv run --extra dev pytest -q

# 전체 Organization AI 경로. 새 출력 경로를 사용한다.
uv run --extra dev python scripts/test_organization_local_model.py \
  --base-url http://192.168.0.67:1234/v1 \
  --model qwen3.8-27b-uncensored-mlx \
  --output-mode plain --timeout 180 \
  --out artifacts/organization-local-new

# 레포는 정적으로 분석하고 시스템 제안 호출만 분리한다.
uv run --extra dev python scripts/test_organization_local_model.py \
  --base-url http://192.168.0.67:1234/v1 \
  --model qwen3.8-27b-uncensored-mlx \
  --static-repositories --output-mode plain \
  --max-output-tokens 2048 --max-calls 1 --timeout 120 \
  --out artifacts/organization-advisor-new
```

호출마다 input digest·입력 크기·usage·실제 reported model·finish reason·경과시간·검증 결과를 저장한다. 응답 중단으로 완료 여부가 불명확하면 같은 실행에서 다음 호출을 막도록 테스트 runner를 보완했다. 원문 요청·응답은 로컬 `artifacts/.../local-calls`에 있고, Git에는 요약 보고서만 저장한다. 기존 사용자 `scripts/evaluate_local_model.py`는 수정하지 않았다.

상세 측정: [organization-local-model-1002.json](organization-local-model-1002.json).

서버 스키마 지원 관련 기준 문서: [LM Studio Structured Output](https://lmstudio.ai/docs/developer/openai-compat/structured-output). 해당 서버는 문서의 일반 지원 설명과 달리 이번 production schema의 uniqueItems를 거절했다. [native chat](https://lmstudio.ai/docs/developer/rest/chat)의 reasoning 옵션 역시 모델별 지원 여부를 확인해야 한다.
