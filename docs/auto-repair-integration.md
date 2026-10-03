# 수정본 S3 저장 · 자동 푸시 · 재귀 재배포

2026-10-03 구현. 사용자의 레포 푸시 및 재배포 허가가 주어진 상태를 전제로 한다.

분석기는 원문 분석과 빌드 준비 역할을 유지한다. 실제 수정 후보를 생성하는 `iris_code_fix_agent`에 별도 `iris-auto-repair` coordinator를 구현하고, `iris-was-upstream/repair-agent-integration`의 원본 diagnosis-result.v3 API와 연결한다. 이전 `iris-was-wt/ai-analysis-integration`의 v2 진단 API와는 별도 통합이다.

흐름은 실제 실패 배포 진단 → 해당 SHA의 소스 스냅샷 → 코드 수정 후보 → artifact/manifest 무결성 확인 → 수정 아카이브와 diff/changes/manifest S3 저장 → 동일 base SHA를 부모로 커밋 → 서비스 브랜치에 강제 옵션 없이 자동 push → 해당 커밋 재배포 → 성공 종료 또는 새 진단·수정이다.

- 수정 에이전트: `src/iris_code_fix_agent/auto_repair.py`, `storage.py`, `publication.py`, `source.py` 및 `docs/auto-repair.md`.
- WAS: `DiagnosisService.get_repair_context`와 소유자 인증을 사용하는 `GET /api/v1/services/{serviceId}/deployments/{deploymentId}/repair-context`. 원본 진단을 필드 손실 없이 전달하며 유효한 빌드 스냅샷이 없으면 거절한다.
- S3에는 수정 전체 `source.tar.gz`, `patch.diff`, `changes.json`, `manifest.json`을 checksum과 content 기반 key로 저장한다. 업로드 성공 전에 브랜치를 갱신하지 않는다.
- 자동 배포 서비스는 push webhook 배포를 source SHA로 추적한다. 자동 배포가 꺼진 서비스는 고정 SHA와 멱등 키를 포함해 MANUAL 배포를 접수한다.
- 기본 최대 3회·전체 30분이며 수정 모델 비용 한도는 회당 USD 1이다. WAS 진단 비용은 별도 설정이다. 브랜치 변경·같은 후보 반복·코드 외 수정안·시간 만료·횟수 소진에서 중단한다.
- 후보와 단계별 상태를 로컬 영속 journal에 저장한다. S3 재시도는 같은 후보를 사용하고, 푸시 응답 유실은 저장한 commit SHA와 branch head를 비교해 복구한다. 중단된 작업의 자식 커밋을 새 incident로 시작하지 않는다.

운영에는 업데이트한 WAS·수정 API와 coordinator 실행, private S3 bucket, AWS 자격증명, 레포 contents 쓰기 GitHub token, 서비스 소유자 WAS token 및 소스 S3 host allowlist가 필요하다. 설정과 명령은 수정 에이전트의 `docs/auto-repair.md`를 따른다. `--watch --service-id ... --allowed-path ...`로 이미 허가된 서비스의 새로운 실패를 자동 관찰할 수 있다.

대역 테스트에서 실패→수정→push→재배포 실패→재수정→재push→재배포 성공, webhook/manual dispatch, 업로드 복구, 최대 회차 중단 및 GitHub 원문 검사를 확인한다. 실제 클라우드 업로드·GitHub push·유료 모델·운영 재배포는 이번 구현 검증에 포함하지 않았다. coordinator를 운영 프로세스로 실행하기 전에는 자동 동작이 활성화되지 않는다.
