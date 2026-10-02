# 1002 — Organization 입력으로 시스템 분석·계획·배포

작성일: 2026-10-02. 구현 저장소는 `iris-code-analyzer-agent`다. WAS·프론트·인프라 저장소는 이번 기능에서 수정하지 않는다.

## 동작과 현재 범위

`Organization → 접근 가능한 레포 조사 → 레포별 고정 커밋/원문 분석 → 시스템 그래프 → 부족 정보 확인 → 전체 시스템 계획 → 이미지/배포 설정 → 별도 executor`를 제공한다.

- Organization slug 또는 GitHub URL을 받는다. 인증은 `GITHUB_TOKEN`/`GH_TOKEN` 또는 기존 `gh auth token`이다. 설치 token 전용 목록은 라이브러리의 `installation_mode=True`로 사용한다.
- 레포 목록을 페이지별로 조사하고 선택한 모든 ref를 고정 SHA로 해석한 다음 다운로드한다. Organization 전체를 동시에 고정하는 트랜잭션은 아니며, 각 레포의 정확한 커밋을 release에 묶는다.
- 기본 최대 50개, 요청 최대 100개다. 권한 범위·알 수 없는 비공개 레포·목록 제한·조회 실패를 coverage에 남긴다. 알려진 목록 누락은 전체 조사 완료로 처리하지 않고, 명시적인 제한 범위 선택이나 재조회가 필요하다.
- 레포별 분석은 기존 Node.js workspace/Vite/Express/Dockerfile/Compose 분석 범위를 재사용한다. Organization 기능이 새로운 언어 분석기를 추가한 것은 아니다. 미지원 앱은 질문으로 남는다.
- 앱·라이브러리·문서·기존 Terraform 설정과 외부 DB/큐/스토리지를 구분한다. 선언된 클라이언트 패키지나 Compose DB를 실제 생성된 인프라로 취급하지 않는다.
- 서비스 ID는 레포 ID와 원본 serviceId를 묶어 구분한다. 연결 근거는 레포·커밋·파일·줄로 구분하고, 같은 변수명/포트/localhost만으로 서비스 관계를 확정하지 않는다.
- 정적 모드는 확인한 앱 전체를 기본 대상으로 한다. `--ai`는 목적을 읽어 배포 가능한 앱 범위를 제안한다. 명시적인 `selectedServiceIds`가 우선이며, AI 선택은 `scopeSelection.basis=ai_proposal`과 원래 요청/제안 digest로 기록한다. 모델은 임의 IaC나 실행 명령을 만들지 않는다.
- 첫 실행 adapter는 **기존 Kubernetes**다. 여러 source 앱의 Dockerfile/Railpack 빌드·registry push·digest/platform 검사·Service/Deployment/Ingress·rollout 및 승인한 HTTP 확인을 연결한다.
- 신규 VPC·클러스터·DB·큐 생성, migration, 데이터 rollback, 기존 외부 Terraform 코드의 실행은 포함하지 않는다. 신규 AWS EKS 요청에는 원본 per-repository dossier와 필요한 질문을 반환하며 실제 system executor는 진행하지 않는다.

## Organization만 전달하기

```sh
uv sync --extra dev
uv run iris-organization https://github.com/2026-softbank-1 \
  --purpose "이 Organization의 사용자 서비스를 하나의 시스템으로 구성" \
  --out artifacts/organization-1002
```

첫 인자를 Organization으로 주면 `analyze`의 별칭이다. 위 실행은 정적 분석·계획이며 배포하지 않는다. `--offline`도 정적 모드를 뜻한다. 네트워크까지 사용하지 않으려면 아래 `--sources`를 함께 제공한다.

AI 분석과 목적에 맞는 시스템 구성 제안:

```sh
uv run iris-organization analyze https://github.com/ORG \
  --purpose "프론트·API·worker를 연결한 서비스 구성" \
  --ai --env-file .env --max-cost-usd 1 \
  --budget-ledger artifacts/model-budget-ledger.json \
  --out artifacts/organization-ai
```

기존 고정 OpenCode 1.18.33·모델 설정·공유 ledger를 사용한다. 레포별 분석과 Organization 제안이 같은 예산을 쓴다. 모델 호출 오류는 표시하고 다른 모델이나 정적 AI 응답으로 자동 대체하지 않는다. 실제 AI 호출은 `--ai`를 명시한 사용자가 실행한다.

범위와 목표 환경은 `--include 'shop-*'`, `--exclude 'legacy-*'`, `--max-repositories 30`, `--context my-cluster`, `--namespace shop-preview` 또는 request JSON으로 지정한다. 기본 archived/fork/template는 조사 목록에 남기고 제외한다. 명시적인 include로 포함할 수 있다.

## 입력·출력 계약

| 계약 | 의미 |
|---|---|
| `iris.organization-request.v1` | 목적·레포 범위·환경·서비스/연결 바인딩 |
| `iris.organization-sources.v1` | 오프라인 고정 소스 목록. SHA는 호출자 확인 값이며 byte manifest를 별도로 검증 |
| `iris.organization-snapshot.v1` | 모든 조사 레포의 커밋·분석·readiness·근거·build manifest와 coverage |
| `iris.system-graph.v1` | 전체 구성 요소·연결·질문·근거 위치 |
| `iris.system-advice.v1` | 제한된 AI 구성 제안. 확정된 source 관측값과 구분 |
| `iris.system-plan.v1` | source lock·이미지·환경·작업 그래프·실행 조건 |
| `iris.system-execution.v1` | 고정 compiler의 Helm/native 출력과 artifact hash |
| `iris.system-release.v1` | 실제 확인한 image digest·manifest·source lock |

요청 타입은 [organization-system.ts](../contracts/organization-system.ts), Python JSON Schema는 `organization/contracts.py`의 `REQUEST_SCHEMA`다. `iris-organization schema --out organization-request.schema.json`으로 내보낼 수 있다.

주요 출력:

```text
organization-snapshot.json
organization-result.json
system-graph.json
system-plan.json
architecture.md
local-workspace.json                  # source root 경로, 로컬 전용·0600
sources/{repositoryId}/               # 비밀 파일 제외 후 원본 build bytes
repository-artifacts/{repositoryId}/  # 분석·검증·마스킹된 근거
system-bundle/
  system-plan.json
  execution.json
  manifests.yaml                     # image-ready일 때만
  helm/                              # image-ready일 때만
```

`needs_input`은 필수 입력/소스 문제 때문에 차단된 계획이다. `build_required`는 build recipe가 준비됐고 아직 이미지 digest가 없는 상태다. 이 경우 실행 가능한 manifest를 먼저 생성하지 않는다. `ready`는 고정 이미지/바인딩으로 설정을 컴파일할 수 있다는 뜻이며 배포 성공이 아니다. 계획의 `executionAuthorized`는 항상 false다.

## 질문에 답하고 같은 소스로 재계획

`request.json`에 `system-graph.json`의 정확한 서비스 ID를 사용한다. 예시는 필드 모양이며 실제 ID/registry/context는 결과에 맞춘다.

```json
{
  "schemaVersion": "iris.organization-request.v1",
  "organization": "ORG",
  "purpose": "웹과 API 시스템 구성",
  "environment": "preview",
  "target": {
    "kind": "existing_kubernetes",
    "context": "reviewed-cluster",
    "namespace": "iris-preview",
    "architecture": "amd64"
  },
  "selectedServiceIds": ["repo-101--api", "repo-102--web"],
  "serviceBindings": [
    {
      "serviceId": "repo-101--api",
      "imageRepository": "ghcr.io/ORG/catalog-api",
      "port": 3000,
      "secretRefs": [{"key":"DATABASE_URL","name":"catalog-db","secretKey":"url"}]
    },
    {
      "serviceId": "repo-102--web",
      "imageRepository": "ghcr.io/ORG/shop-web",
      "port": 3000,
      "buildEnv": [{"key":"VITE_API_URL","value":"https://api.example.com"}]
    }
  ]
}
```

`imageReference`에 `registry/path@sha256:<digest>`를 공급하면 해당 이미지를 재빌드하지 않는다. 없으면 source manifest로 Dockerfile 또는 Railpack 빌드를 계획하고 `imageRepository`를 요구한다. Dockerfile 경로는 **레포 root 기준**, buildContext 안의 경로다. build context가 여러 앱에 걸친 기존 monorepo 레이아웃이면 명시적으로 지정한다.

`connectionBindings`는 `{fromServiceId,toServiceId,kind,environmentKey,phase}`다. 확인한 HTTP 관계의 runtime 값은 대상 Service의 내부 DNS로, 프론트/빌드 시점 값은 확인한 public endpoint로 연결한다. browser는 cluster 내부 DNS에 접속할 수 없다. 공개 ingress는 `deploymentRequest.bindings.ingress`의 정확한 serviceId/host/className/tlsSecretName과 인프라 확인 값이 필요하다. executor는 실제 namespace Secret/키 존재를 다시 확인한다.

`runtimeEnv`와 `buildEnv`는 공개 값이다. 비밀 키/자격증명/암묵적 변수 확장은 거절한다. `secretRefs`는 기존 namespace Secret을 참조한다. runtime Secret은 build secret 전달을 대신하지 않는다. generic 필수 환경값·소스 오류도 계획에서 확인한다. 명령 문자열을 host shell에 실행하지 않으며, runtime override는 직접 실행 배열 또는 source 이미지 entrypoint를 사용한다. 별도 shell override가 필요하면 source 설정으로 기록한다.

```sh
uv run iris-organization plan \
  --snapshot artifacts/organization-1002/organization-snapshot.json \
  --request confirmed-request.json \
  --out artifacts/organization-confirmed
```

레포를 다시 내려받지 않는다. 레포 include/exclude·서비스 범위를 바꿀 수 있지만 고정 ref를 다른 것으로 바꿀 수는 없다. 새 커밋을 원하면 새 Organization snapshot을 만든다. 원래 snapshot은 수정하지 않는다.

## 실행 경계

먼저 번들만 확인한다. dry-run은 Docker·kubectl·모델·HTTP 확인을 실행하지 않는다.

```sh
uv run iris-organization apply --bundle artifacts/organization-confirmed/system-bundle
```

실제 실행에는 review한 `execution.json`/plan의 digest, context, namespace를 담은 로컬 authorization JSON이 필요하다.

```json
{
  "planDigest": "<검토한 planDigest>",
  "bundleDigest": "<검토한 bundleDigest>",
  "context": "reviewed-cluster",
  "namespace": "iris-preview",
  "allowDeploy": true,
  "allowBuilds": true,
  "allowPushes": true,
  "approvedHttpUrls": []
}
```

```sh
uv run iris-organization apply \
  --bundle artifacts/organization-confirmed/system-bundle \
  --workspace artifacts/organization-1002/local-workspace.json \
  --authorization reviewed-authorization.json --execute
```

Docker/Railpack·BuildKit·registry 로그인·kubectl context는 실행자가 미리 구성한다. executor는 툴을 설치하거나 privileged build daemon을 띄우지 않는다. registry push 후 RepoDigest와 Linux architecture를 확인한 이미지로만 Kubernetes 설정을 생성한다. 일반 source Dockerfile/Railpack 빌드는 승인된 source 코드를 실행하며 외부 패키지 버전이 바뀌면 이미지도 달라질 수 있다.

각 mutation 전에 journal에 작업 ID/실행 의도를 저장한다. 종료·실패로 결과가 불확실하면 반복 실행하지 않고 확인을 요구한다. `reconcile --bundle ... --authorization ... --operation-id ... --resolution retry|complete`는 실행자가 확인한 결과만 기록한다. 완료한 이미지 작업에는 정확한 `--image-reference`를 추가한다. 죽은 프로세스의 lock은 실제 실행 상태를 확인한 운영자가 정리한다.

rollout은 Kubernetes readiness 확인이며 전체 업무 기능 검증은 아니다. `httpChecks`와 동일한 `approvedHttpUrls`를 제공하면 해당 정확한 URL의 status를 확인한다. HTTP redirect와 응답 body 수집은 하지 않는다. 실제 HTTP 확인이 없으면 connectivity 완료로 표시하지 않는다. credential 값은 journal에 저장하지 않는다. 컨테이너 보안 설정과 이미지의 호환성, 기존 이미지/DB/schema rollback은 별도 운영 검증 대상이다.

## 오프라인 시연 입력과 검증 인계

작은 frontend/API/Terraform source fixture를 추가했다. synthetic SHA는 caller-attested fixture 식별자이며 GitHub의 실제 commit 검증 결과가 아니다.

```sh
uv run iris-organization analyze demo-org \
  --sources fixtures/organization-demo/sources.json \
  --request fixtures/organization-demo/request.json \
  --out artifacts/organization-demo
```

CLI는 manifest의 상대 sourceRoot를 manifest 위치 기준으로 해석한다. Python 라이브러리는 절대 경로를 받는다. 실제 네트워크를 사용하지 않는 fixture지만 source 모델도 호출하지 않도록 기본 static 모드다. 새 출력 경로를 사용한다.

이번 기능은 테스트 코드를 추가했으며 사용자 요청에 따라 테스트·검증 명령·유료 모델·실제 Organization 분석·클라우드 배포를 실행하지 않았다. 검증을 맡을 때의 범위는 source pagination/권한/고정 SHA/압축 안전성, cross-repo 근거·모호한 관계, 질문 재개·source seal, 다중 image 계획·artifact 변경 차단·실행 journal·HTTP 확인이다. 기존 단독 repo 계약과 WAS vendored wheel은 변경하지 않았다. 새 기능을 WAS에서 쓰려면 WAS 담당자가 추후 별도 연결/패키지 갱신을 수행한다.

## 모듈

| 모듈 | 역할 |
|---|---|
| `organization/github.py`, `sources.py` | GitHub 조사·고정 SHA·byte-preserving 다운로드 |
| `organization/graph.py` | 구성 요소·관계·근거·질문 |
| `organization/advisor.py` | 고정 모델과 예산의 제한된 시스템 제안 |
| `organization/pipeline.py` | 기존 repo 분석 재사용·snapshot·재계획 |
| `organization/planner.py` | 다중 앱 바인딩·source/image lock·실행 DAG·기존 dossier |
| `organization/execution.py` | 고정 compiler·별도 실행·결과 확인·재개 |
| `organization/contracts.py`, `cli.py` | JSON 요청 계약·독립 CLI |

## 후속 로컬 모델 검증 — 2026-10-02

이후 사용자의 요청으로 로컬 서버 테스트를 실행했다. 전체 테스트 760개 통과/2개 skipped이고, 최소 JSON 응답은 받았지만 MLX JSON Schema의 uniqueItems 거절 및 레포 분석/시스템 제안 timeout으로 전체 AI Organization 성공은 확인하지 못했다. 상세 결과와 재현 명령은 [로컬 모델 1002 보고서](../reports/organization-local-model-1002.md)를 참고한다. 실제 배포는 실행하지 않았다.
