# 전처리·빌드 준비 연동 검증 · 2026-10-01

최신 `iris-was/develop` **344426f**를 별도 clone/worktree로 준비하고, 소스 분석 뒤 Dockerfile을 준비하는 Worker 단계를 연결했다. 분석기 개선은 `feat/preprocess-deployment-contracts`, WAS 연결은 `feat/ai-dockerfile-preparation` 브랜치다.

## 확인한 처리 경로

```text
WAS 준비 CLI → BuildPreparationService → bounded subprocess Client
  → 원본 bytes staging / 별도 build manifest
  → 전처리·정적 분석·source readiness
  → 기존 Dockerfile 보존 또는 controlled template 생성
  → source/ prefix tar.gz
  → WAS가 원본 모든 파일·실행 비트·해시·경로를 독립 검증
  → 반환 아카이브로 실제 로컬 Docker 빌드
```

| 샘플 | 소스 커밋 | 준비 결과 | 실제 검증 |
| --- | --- | --- | --- |
| Temp_log | `54fa8072d9fb` | 기존 Dockerfile 바이트 보존, ready | ARM64 Docker 빌드 성공, 컨테이너 Node `v24.21.0` |
| portpolio-production | `f54230346fbf` | Dockerfile 없음 → `vite-static-npm.v1`, ready | ARM64 빌드 성공. nginx UID101, read-only root, cap-drop, 64MiB/0.5 CPU에서 `/`, `/healthz`, JS 파일 HTTP200 |

빌드 context의 binary 파일과 실행 권한을 보존했다. 분석용 마스킹 자료를 이미지 소스로 사용하지 않았다. 사용자 소스는 수정하지 않았고, 생성 Dockerfile은 최종 아카이브에만 추가했다. 임시 smoke 컨테이너는 정리했다. 기존 프로젝트의 DB·컨테이너는 변경하지 않았다. 검증용 이미지와 로그·아카이브는 로컬 artifact로 남겼다.

## 구조 검토에서 수정한 내용

- Temp_log 빌드 경로 `/app`와 런타임 `/app/server`를 분리했다. package script 호출은 `npm run build`로 감싸고 원문/정책 출처를 구분했다. build context·Dockerfile·stage·설치/lockfile 정보는 `sourceReadiness.buildTargets`에 담았다.
- `environmentVariables`에 소유 서비스·component·build/runtime 단계·필수 여부·근거를 남겼다. 앱의 필수 Secret은 `MONGO_URI`, `SESSION_SECRET`이며 MongoDB 초기화 암호는 앱에 요구하지 않는다. QA·Compose 보간 변수는 별도로 표시한다.
- 일반 환경 값과 ConfigMap 참조를 배포 요청부터 manifests까지 연결했다. 중복·평문 자격증명·알 수 없는 서비스·변조된 값은 거절한다.
- `serviceConnections`에 소스 근거로 확인한 앱→MongoDB 연결, 포트·프로토콜·설정 키를 제공한다. 모든 동적 연결의 완전한 복원을 주장하지 않는다.
- unsafe/dynamic/remote Compose context, 모노레포 패키지 매니저 충돌, 중첩 Dockerfile의 잘못된 component 귀속을 검증했다. 불명확한 구성은 질문으로 남긴다.

## 생성기·경계 회귀

상위 symlink로 소스 밖 파일을 선택하거나 기존 출력 symlink로 외부 파일을 쓰는 경우를 거절한다. WAS는 `source/Dockerfile`과 `source//Dockerfile` 같은 정규화 충돌, 원본 파일의 추가·누락·변경, 허위 manifest/evidence를 거절한다. 부모가 먼저 종료해도 timeout/cancel 시 남은 자식 프로세스를 정리한다.

정확한 Node 버전을 major로 축약하지 않는다. Vite 빌드 전 dist를 제거하고, 검증되지 않은 custom Vite 설정과 미설정 빌드 환경변수는 needs_input으로 처리한다. `secrets/.secrets/credentials`가 COPY로 이미지에 포함되는 경로도 차단했다. 원본 Dockerfile과 명시적 Railpack 선택은 보존한다.

## 결과와 한계

- 분석기: Python3.11·3.13 각각 **497 passed**, 비용 없는 native OpenCode 검사 포함. branch-aware coverage **86.62%**. Ruff·format 및 wheel의 신규 모듈 포함 확인 통과.
- WAS: **236 passed, 8 skipped**. 로컬 생략은 PostgreSQL 통합 테스트이며 PR CI에서 별도로 실행한다. 전체 Ruff·format·strict mypy 통과.
- 새 산출물의 품질 정답 재평가: Temp_log **22/22**, portfolio **15/15**. 기존 지원 프로필의 정답 기준이며 일반 코드 이해 정확도나 성능 보장은 아니다.
- 이번 준비 CLI는 `analysisMode=static`, 생성은 `controlled_template`이다. 새 유료 모델 호출은 없었다. 기본 Node/npm·Vite 프로파일을 지원하며 일반 Python/Go/SSR·모노레포 생성은 별도 프로파일이나 기존 Railpack 경로가 필요하다.
- 실제 AWS CodeBuild·ECR push·WAS DB 큐 E2E는 수행하지 않았다. 최신 Worker의 Job 선점은 기존 TODO 상태다. 팀 빌드 담당자가 제공된 준비 Service를 S3/CodeBuild 호출 앞에 연결해야 한다.
- 로컬 Docker image ID와 registry manifest digest는 다르다. ARM64에서의 빌드/실행 결과를 amd64 호환성이나 운영 용량 증거로 사용하지 않는다. Temp_log 전체 DB 앱 E2E는 이전 평가 범위를 유지하며 이번에는 재수행하지 않았다.

가공 가능한 상세값은 [검증 JSON](build-preparation-validation.json), 계약과 명령은 [빌드 준비 문서](../docs/build-preparation.md)에 있다.
