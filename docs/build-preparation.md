# 소스 분석 뒤 Dockerfile 준비

빌드 담당자와의 연결 범위는 **고정 소스 분석 → 기존 Dockerfile 보존 또는 제한된 템플릿 생성 → 검증 가능한 빌드 아카이브 반환**이다. 실제 이미지 빌드·ECR push·CodeBuild Job은 팀 WAS/인프라 빌더가 수행한다. 이 모듈을 분석 API 호출만으로 실행·배포하는 경로는 없다.

## 입력과 출력

`python -m iris_analyzer.build.cli --request-stdin`은 stdin JSON 하나를 받고 stdout JSON 하나를 반환한다. Worker가 이미 고정 커밋으로 준비한 소스를 넘긴다.

```json
{
  "schemaVersion": "iris.build-preparation-request.v1",
  "sourceRoot": "/worker/fetched-source",
  "outputDirectory": "/worker/jobs/new-job",
  "sourceSha": "0123456789012345678901234567890123456789",
  "rootDirectory": ".",
  "dockerfilePath": null,
  "platform": "linux/amd64",
  "builder": "dockerfile",
  "allowGeneration": true
}
```

`sourceSha`는 소스 수집 Worker가 검증해 전달하는 Git 커밋이다. 로컬 디렉터리에 SHA 문자열을 붙인다고 Git 원본 인증이 되는 것은 아니다. 준비 모듈은 실제 포함 파일의 별도 `sourceManifestSha256`을 계산하고, 분석 snapshot/context hash, Dockerfile SHA, 최종 압축 SHA를 기록한다.

출력 `iris.build-preparation.v1`에는 `status`, `sourceSha`, `rootDirectory`, `platform`, `dockerfilePath`, `dockerfileOrigin`, `dockerfileSha256`, `templateId`, `sourceManifestSha256`, `analysisSourceSnapshotId`, `analysisContextHash`, `analysisMode`, `analysisResult`, `sourceReadiness`, `evidence`, `sourceArchive`, `planDigest`, `preparationDigest`, `unresolvedInputs`, `executionAuthorized=false`가 있다. 준비 성공은 `ready`, 결정할 설정이 남으면 `needs_input`이다. 이는 이미지 빌드 성공이나 배포 승인이 아니다.

`sourceArchive`는 `path/sha256/format`이며 ready일 때만 생성한다. tar 항목은 `source/<원본 경로>`와 선택적 생성 Dockerfile 하나다. 인프라의 기존 `tar --strip-components=1` 입력에 맞췄다. Dockerfile 경로는 서비스 `rootDirectory` 안의 상대 경로다. 원본 파일·실행 비트와 binary 자산은 보존한다. `.git`, 호스트 의존성·가상환경, `.env*`, 명시적 인증 파일과 `secrets/.secrets/credentials` 디렉터리는 포함하지 않는다. 제외 항목은 manifest에 남고, 여기에 의존하는 프로젝트는 별도 빌드 설정이 필요하다.

마스킹된 분석 context는 이미지 소스로 쓰지 않는다. 원본을 별도 staging한 뒤 분석하고, 생성 Dockerfile은 아카이브에만 추가한다. 사용자 저장소와 staging 원본을 덮어쓰지 않는다. 심볼릭 링크·경로 이탈·변경된 manifest·기존 출력 디렉터리는 거절한다.

## 생성 정책

- 기존 Dockerfile이 있으면 바이트를 그대로 보존한다. 잘못된 명시 경로를 다른 파일로 자동 대체하지 않는다.
- Dockerfile이 없을 때 npm lockfile v2/v3 기반의 단순 Node 시작 앱 또는 기본 Vite 정적 앱만 고정 템플릿으로 생성한다. 분석 결과와 생성 정책의 출처는 분리한다.
- Node 22/24의 지원하는 선언을 확인한다. `.nvmrc` 등의 정확한 버전은 보존하고, 불명확하거나 충돌한 제약은 질문으로 남긴다. 선언이 없을 때의 Node 22는 템플릿 정책이다. base image tag는 digest 고정이 아니므로 바이너리 재현성을 주장하지 않는다.
- Vite는 `npm run build` 전에 기존 dist를 제거한다. custom `vite.config.*`, SSR/라이브러리/모노레포, 미설정 빌드 환경변수는 검증된 전용 프로파일 또는 Dockerfile이 필요하다. 일반 Node 빌드/TypeScript, Python, Go는 이 생성기의 지원 범위가 아니다. 팀이 명시적으로 선택한 Railpack 경로는 WAS에서 유지한다.
- 생성 정적 이미지는 nginx 비특권 사용자와 8080 포트를 사용하며 `/healthz`를 제공한다. read-only root 및 `/tmp` tmpfs 조건으로 실제 실행을 확인했다.

현재 subprocess bridge는 정적 분석 모드를 명시한다. 라이브러리 `prepare_source_build(request, runner=...)`는 기존 검증된 모델 runner를 받을 수 있지만, Dockerfile 텍스트는 검토한 템플릿으로 생성한다. 이를 자유 형식 LLM 코드 생성이나 새로운 모델 정확도 평가로 표현하지 않는다.

## 처리 구조 보완

`sourceReadiness.buildTargets`는 Docker 빌드 context·Dockerfile·stage·빌드 경로·명령과 패키지 매니저·lockfile·설치 정책을 제공한다. Temp_log의 빌드 `/app`와 실행 `/app/server`를 분리한다. `resolvedCommand`/`resolvedCommandBasis`는 `npm run build` 같은 실행 래퍼가 정책인지 원문 명령인지 구분한다.

`environmentVariables`는 key·component·serviceName·phase·required·origin·condition·evidenceIds를 제공한다. 앱용 Secret과 DB 초기화·Compose 보간·QA 변수를 구분한다. `serviceConnections`는 공급된 근거로 확인한 앱→DB 등의 연결 정보를 추가한다. 선언되지 않은 동적 연결 전체를 복원한다고 주장하지 않는다.

배포 요청의 `bindings.runtimeEnv`와 `bindings.configMapRefs`를 통해 서비스별 일반 설정을 전달한다. Secret과 키가 중복되거나 평문 자격증명·알 수 없는 서비스가 포함되면 거절한다. 자세한 형식은 [배포 계약](deployment-planning.md)에 있다.

## 팀 WAS 연결

`iris-was` 최신 `develop`의 Worker-side `BuildPreparationService`가 위 subprocess 계약을 사용한다. WAS는 응답 SHA·플랫폼·경로, 아카이브의 모든 원본 파일 및 실행 비트, manifest와 evidence를 독립적으로 대조한다. 유일하게 허용되는 추가 파일은 선택한 생성 Dockerfile이다. 기존 Dockerfile 변경, 파일 누락·추가·변경, 중복 tar 경로, 타임아웃 뒤 남은 자식 프로세스에 대한 회귀를 포함한다.

WAS의 DB 큐/소스 수집→CodeBuild 실제 호출은 기존 빌드 담당 구현에서 이 준비 단계를 호출해야 한다. 이번 검증은 WAS 준비 CLI→실제 분석기→아카이브→로컬 Docker 빌드 경로까지 수행했다. [검증 결과](../reports/build-preparation-validation.md)를 참조한다.
