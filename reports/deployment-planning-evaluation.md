# 배포 계획·실행 설정 검증 · 2026-10-01

기존 analysis-result v1을 유지하고 source-readiness, 구조화된 배포 계획, 제한된 AI 운영 제안, 고정 실행 템플릿과 backend dossier를 추가했다. 임의 모델 생성 IaC를 실행하지 않는다.

두 제공 저장소에서 실제 Hive GLM으로 초기 운영 조건을 제안받았다. 원본 분석·요청 digest를 보존한 응답을 최종 계획기에서도 검증했다. 클라우드·리전·architecture·traffic·availability·resource envelope는 추천으로 표시했고, 실행 바인딩이 없는 경우 유용한 계획과 질문을 반환하면서 manifests 생성은 보류했다. 실제 모델 응답의 Terraform/HCL/YAML과 임의 필드는 스키마에서 허용하지 않는다.

검증 범위:

- 버전 선언의 build/runtime 분리, SHA로 확인한 source의 구문·JSON·import·명령·컨테이너·rate-limit 초기 검사
- source/user/measurement/policy 출처, request/analysis/plan digest, unknown·부분 가격·명시적 사용자 요청 보존
- 다른 stack/cloud 요청은 보존한 채 unsupported 반환. AWS EKS와 기존 Kubernetes만 고정 compiler 제공
- 유효한 부하 관측, stale/mismatched/test/build/failed 자료 제외, 제공 RPS와 검증한 capacity 분리
- images/platform/ports/resources/replicas/probes/Secret/PVC/network/private execution/runtime compatibility 확인
- 가격과 지역·수량 계산 일치, EKS standard/extended support tier 및 stale lifecycle 검증
- 반복 실행과 변조된 실행 artifact 검출, worker 취소·동시성·외부 모델 예산 보호
- 프론트 조건 변경·source 재사용 replan·새로고침 및 서버 재시작 기록 복원

고정 template 검증은 Terraform 1.13.3 fmt/init(-backend=false)/validate와 signed hashicorp/aws 6.14.1 lockfile, Helm 3.19.0 lint/template, kubeconform 0.8.0 strict Kubernetes 1.33 schema로 수행했다. Deployment, Service, TLS Ingress, PDB, PVC **5/5**가 유효하고 Helm/native 리소스가 일치했다. 이 검증은 실제 계정·클러스터 접속이나 cloud apply를 포함하지 않는다.

최근 전체 Python 테스트는 Python 3.11·3.13 각각 **426개 통과**했으며 고정 OpenCode의 비용 없는 native 검사도 포함했다. Ruff·Python formatting·JavaScript syntax·wheel 및 static/template/hidden provider lockfile 포함을 확인했다. 정확한 coverage와 모델 집계는 [deployment-planning-evaluation.json](deployment-planning-evaluation.json)에 기록한다.

실제 제공 코드의 Docker 빌드·unit 검사·부하 측정과 보정 값은 [런타임 평가](runtime-capacity-evaluation.md)에 분리했다. source complete, compiler ready, 실제 deploy success와 production capacity는 각각 다른 상태다. 공용 전역 예산, 인증·Job·DB, 실제 infrastructure executor 연결은 팀 플랫폼에서 이어서 검증해야 한다.
