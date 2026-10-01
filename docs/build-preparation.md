# 소스 분석 뒤 서비스 빌더로 인계

담당 경계는 **고정 소스 분석 → 기존 Dockerfile 확인 → 원본 빌드 아카이브·분석 결과 인계**다. Dockerfile이 없으면 백엔드/서비스 담당 김현겸의 Railpack 빌드 경로를 추천한다. Dockerfile 생성·Railpack 실행·이미지 빌드·ECR push·CodeBuild Job은 서비스 빌드 Worker의 책임이다. 분석기는 소스 코드나 설치 스크립트를 실행하지 않으며 Dockerfile을 생성하지 않는다.

Dockerfile 부재는 정상적인 빌더 선택 조건이다. 그 자체를 코드 하자나 재귀 수정 에이전트의 수정 대상으로 보내지 않는다. 실제 빌드/실행 하자는 빌더가 돌려주는 고정 소스 식별자·종료 코드·정제된 로그·재현 정보를 진단 계약으로 검증한 뒤 별도로 인계한다.

## v2 입력과 출력

`python -m iris_analyzer.build.cli --request-stdin`은 stdin JSON 하나를 받아 stdout JSON 하나를 반환한다. Worker가 이미 고정 커밋으로 준비한 소스를 넘긴다.

```json
{
  "schemaVersion": "iris.build-preparation-request.v2",
  "sourceRoot": "/worker/fetched-source",
  "outputDirectory": "/worker/jobs/new-job",
  "sourceSha": "0123456789012345678901234567890123456789",
  "rootDirectory": ".",
  "dockerfilePath": null,
  "platform": "linux/amd64",
  "builder": "auto"
}
```

`builder`는 `auto`(생략 시 기본값), `dockerfile`, `railpack`이다. `rootDirectory`는 저장소 기준 서비스 루트이고 `dockerfilePath`는 그 루트 안의 상대 경로다. 잘못된 명시 경로는 다른 파일이나 Railpack으로 자동 대체하지 않는다. `sourceSha`는 소스 수집 Worker가 검증해 전달하는 커밋이며, 이 문자열만으로 로컬 디렉터리의 Git 원본이 인증되지는 않는다. 준비 모듈은 포함 파일의 별도 `sourceManifestSha256`과 분석 snapshot/context hash, 기존 Dockerfile SHA, 압축 SHA를 기록한다.

출력 `iris.build-preparation.v2`는 기존 분석·무결성 필드와 다음의 서비스 인계를 포함한다.

```json
{
  "schemaVersion": "iris.build-preparation.v2",
  "status": "ready",
  "builder": "railpack",
  "buildHandoff": {
    "owner": "service",
    "recommendedBuilder": "railpack",
    "requestedBuilder": null,
    "decisionRequired": true,
    "reasonCode": "dockerfile_absent"
  },
  "dockerfilePath": null,
  "dockerfileOrigin": null,
  "dockerfileSha256": null,
  "templateId": null,
  "executionAuthorized": false
}
```

위는 인계 필드 발췌이며 전체 응답은 `sourceSha`, `rootDirectory`, `platform`, `sourceManifestSha256`, `analysisSourceSnapshotId`, `analysisContextHash`, `analysisMode`, `analysisResult`, `sourceReadiness`, `evidence`, `sourceArchive`, `planDigest`, `preparationDigest`, `unresolvedInputs`도 포함한다.

- `builder`와 `recommendedBuilder`는 준비 모듈의 추천이다. DB의 `service.builder`를 설정하거나 덮어쓰는 명령이 아니다.
- `requestedBuilder`는 명시적 호출자 선택이며 `auto`/생략은 `null`이다.
- `decisionRequired`는 자동 추천이거나 `needs_input`이면 `true`다. 검증된 명시적 빌더 선택일 때만 `false`다. Worker는 실제 실행 전 서비스에 저장된 빌더 선택을 확인한다.
- `ready`는 분석 및 원본 소스 아카이브 준비 완료다. Railpack의 언어 감지·호환성·빌드 성공이나 환경변수의 완전함을 주장하지 않는다. 그런 검증은 빌더 담당이다.
- `executionAuthorized`는 항상 `false`다. 명시적인 빌더를 전달했어도 분석 호출이 빌드를 실행하지 않는다.

## 선택 규칙

| 입력/관측 | 추천 · reasonCode | 결과 |
|---|---|---|
| auto + 서비스 루트의 Dockerfile 존재 | dockerfile · `source_dockerfile` | ready, 원본 바이트 보존, 서비스 결정 필요 |
| auto + Dockerfile 후보 없음 | railpack · `dockerfile_absent` | ready, 원본만 인계, 서비스 결정 필요 |
| auto + 기본 파일 없이 다른 Dockerfile 후보 존재 | dockerfile · `dockerfile_selection_required` | needs_input, 파일 또는 Railpack 명시 선택 필요 |
| 명시적 dockerfile + 유효한 파일 | dockerfile · `source_dockerfile` | ready, 기존 파일 보존 |
| 명시적 dockerfile + 기본 파일 없음 | dockerfile · `explicit_dockerfile_missing` | needs_input, Railpack으로 몰래 변경하지 않음 |
| 명시적 railpack | railpack · `explicit_railpack` | ready, 기존 Dockerfile 유무와 무관하게 선택 보존 |
| 명시한 dockerfilePath가 없음/잘못됨 | 오류 `BUILD_DOCKERFILE_INVALID` | 다른 파일/빌더로 대체하지 않음 |

Dockerfile 후보는 선택한 서비스 루트 내부에서 찾는다. 다른 서비스의 Dockerfile은 이 서비스의 Railpack 추천을 막지 않는다. Railpack 선택 응답의 Dockerfile path/origin/hash는 모두 null이며, 발견된 기존 파일 자체는 아카이브에 그대로 남는다. 선택한 기존 Dockerfile의 origin은 `source`다. `templateId`는 v2에서 항상 null이다.

## 바이트와 분석의 분리

`sourceArchive`는 `path/sha256/format`이며 ready일 때만 생성한다. tar 항목은 `source/<원본 경로>`뿐이다. 생성 파일 또는 덮어쓰기 overlay를 허용하지 않는다. 원본 파일·실행 비트·binary 자산을 보존한다. `.git`, 호스트 의존성·가상환경, `.env`·`.env.*`, 명시적 인증 파일과 `secrets/.secrets/credentials` 디렉터리는 제외한다. 제외 항목은 manifest에 남고, 이에 의존하는 프로젝트는 빌더에서 별도 설정이 필요하다.

마스킹된 분석 context를 이미지 소스로 사용하지 않는다. 원본을 별도 staging하고 분석하며 원본 또는 staging 파일을 수정하지 않는다. 심볼릭 링크·경로 이탈·변경된 manifest·기존 출력 디렉터리를 거절한다. WAS는 응답 SHA·플랫폼·경로, 아카이브 전체 원본 파일·실행 비트, manifest와 evidence를 독립적으로 대조한다. 추가/누락/변경 파일 및 중복 tar 경로는 실패다.

현재 subprocess bridge는 정적 분석 모드를 명시한다. 라이브러리 `prepare_source_build(request, runner=...)`는 기존 검증·예산 파이프라인의 모델 runner를 받을 수 있다. 이 선택은 빌더 소유권이나 소스 실행 권한을 바꾸지 않는다.

## 빌더에 전달하는 분석 정보

`sourceReadiness.buildTargets`는 빌드 context·Dockerfile·stage·빌드 경로·명령, 패키지 매니저·lockfile·설치 정책을 제공한다. 빌드 경로와 실행 경로를 분리하고 `resolvedCommandBasis`로 원문/정책을 구별한다. `environmentVariables`의 key·component·serviceName·phase·required·origin·condition·evidenceIds와 `serviceConnections`의 근거 기반 연결 관계도 보존한다.

이 결과에 빌드 환경변수나 동적 설정의 미확정 항목이 있을 수 있다. 아카이브 준비 `ready`가 이 항목을 해결하지 않는다. Railpack 감지 결과·설정 override·버전·실제 빌드 로그·이미지 digest는 서비스 빌더가 별도 실행 결과에 기록한다.

## v1에서 v2로 이전

이번 계약은 기존 Draft PR의 생성 경로를 대체한다. v1 입력은 버전 오류로 거절한다. `allowGeneration` 필드는 제거했으며 v2에 넣어도 오류다. Worker는 입력/출력 버전을 함께 v2로 바꾸고 기본 빌더는 auto로 전달한다. 라이브러리 `BuildRequest.template`도 제거하고 `builder`를 사용한다. 원본 이외의 생성 Dockerfile 예외 허용 검사를 제거한다. 이전에 생성한 Dockerfile 아카이브를 v2 결과로 재사용하지 않는다.

서비스 담당은 이 준비 단계를 기존 소스 수집→Railpack/Dockerfile→CodeBuild/ECR 흐름에 연결한다. 분석기에는 Railpack 설치/실행이나 자동 fallback 빌더 실행이 없다. 과거 템플릿 빌드 실험은 [기존 검증 기록](../reports/build-preparation-validation.md)에 남지만, v2의 Railpack 실제 빌드 검증을 대신하지 않는다.
