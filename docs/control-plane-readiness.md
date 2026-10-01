# Iris 백엔드 연결 준비 · 2026-10-01

GitHub 조직의 팀 저장소와 원격 브랜치, PR 및 fork를 확인했다. 조회 시점의 `iris-web`은 README만 있었고 별도 개발 브랜치는 발견하지 못했다. 백엔드 담당자가 개발 중임을 확인한 뒤 분석 모듈을 독립적으로 시연할 테스트 프론트를 추가했다.

| 저장소 | 확인한 브랜치 / 커밋 | 구현 상태 |
| --- | --- | --- |
| [iris-was](https://github.com/2026-softbank-1/iris-was) | main · `2e1cc108e58a9755a545242efe5a43fecd767355` | FastAPI control plane skeleton. PR #1 병합. |
| [iris-web](https://github.com/2026-softbank-1/iris-web) | main · `51725f93621dc2fd7a2c9bd7acb8ea40cd336539` | README |
| [iris-infra](https://github.com/2026-softbank-1/iris-infra) | main · `82ab7e8ff1f2f4c2c403945c274691454a5b973d` | gitignore |
| [iris-error-check-agent](https://github.com/2026-softbank-1/iris-error-check-agent) | main · `2e17c2f75d546cdf9e47b33c83bdf929e234b34c` | README |
| [iris-code-analyzer-agent](https://github.com/2026-softbank-1/iris-code-analyzer-agent/tree/test/feature) | test/feature · 기준 `cc9937a` | 전처리·분석·검증·평가 구현. main은 초기 상태. |

## 실제 WAS와 맞춘 경계

WAS는 Python 3.13, Pydantic 2, async SQLAlchemy/PostgreSQL을 사용한다. `/healthz`와 `/readyz`만 구현되어 있고 business router, 실제 worker 처리, migration은 개발 대상이다. 문서의 배포 흐름은 CodeBuild → ArgoCD GitOps다. 현재 JobKind는 BUILD/DEPLOY/RECONCILE/ROLLBACK이며 ANALYZE job이나 공개 분석 endpoint는 정해지지 않았다.

`iris_analyzer.integrations.LocalAnalysisClient`는 worker가 소유한 로컬 소스 디렉터리를 입력받는다. 동기 분석과 OpenCode 수명은 worker thread에서 관리하고, 진행 콜백은 WAS의 async event loop로 전달한다. 기본 동시성은 1이다. 취소 시 runner에 취소 이벤트를 전달하고 종료를 기다린 뒤 슬롯을 반환한다. 진행 기록 저장 오류도 호출자에게 전달한다.

```python
from pathlib import Path
from iris_analyzer.integrations import LocalAnalysisClient, create_live_runner_factory
from iris_analyzer.opencode import ModelConfig

client = LocalAnalysisClient(
    runner_factory=create_live_runner_factory(
        ModelConfig.from_env(Path('.env')),
        budget_ledger='/worker/shared/model-budget-ledger.json',
        executable='/path/to/opencode',
    ),
)
# worker의 async 함수 내부에서:
outcome = await client.analyze_repository(
    source_root, out=artifact_directory, on_progress=persist_progress,
)
response_data = outcome.to_response_data()
```

`persist_progress`는 `AnalysisProgress`를 받는 async 함수다. 모든 progress의 job_status_hint는 RUNNING이다. pipeline의 마지막 이벤트 이후에도 산출물 기록이 실패할 수 있어 최종 상태는 반환 결과 또는 오류를 받은 큐 담당자가 저장해야 한다. BUILD job에서 분석을 호출하면 이 반환이 BUILD 전체의 성공을 의미하지 않는다.

정상 분석의 `analysisExecutionStatus=SUCCEEDED`와 `analysisStatus=complete/needs_input/unsupported`를 분리한다. `needs_input`과 `unsupported`는 실행 실패나 WAS의 MANUAL_INTERVENTION을 자동으로 의미하지 않는다. `reviewRequired`는 complete가 아닌 경우 true이며 `deploymentAuthorized`는 항상 false다. detected/suggested/unknown 구분과 unknown의 명시적 `value:null`을 보존한다.

WAS `ApiResponse[dict[str, Any]]`의 data에 결과를 넣으면 envelope의 `exclude_none=True` 직렬화에도 nested JSON의 unknown null이 유지된다. snake_case→camelCase는 WAS envelope 규칙을 따른다. Request ID는 HTTP header다. 제안 타입은 `contracts/control-plane-analysis.ts`에 있으며 계약 버전은 `iris.control-plane.analysis.v1-draft`다.

## 계약 검증

```sh
# 실제 WAS의 의존성과 Python 3.13 환경에서 analyzer를 editable 설치한 후:
python scripts/verify_control_plane_contract.py \
  --was-root /path/to/iris-was --out artifacts/control-plane-contract
```

실제 WAS response/enums를 읽어 상태 코드, camelCase envelope, unknown null 보존, 배포 승인 분리를 검증했다. 모델 요청·DB 쓰기·배포 동작은 각각 0회다. WAS 자체 테스트 14개와 ruff/mypy도 통과했다. 해당 저장소는 수정하지 않았다.

## 팀과 확정할 부분

- source 전달 방식, 고정 커밋 및 provenance 저장, 소스/근거 보관과 접근 권한
- 분석을 BUILD의 단계로 실행할지 별도 job으로 접수할지, endpoint와 요청/응답 계약
- Job 진행·최종 결과의 PostgreSQL 트랜잭션과 취소·재개 정책
- 환경변수 실제 값 등록, 공용 모델 예산 기록, frontend의 제안 확인 흐름

테스트 프론트의 `/api/reviews`는 개발용 경계다. WAS 접속, PostgreSQL 연동, 실제 빌드·배포 E2E는 팀 구현 후 검증해야 한다.
