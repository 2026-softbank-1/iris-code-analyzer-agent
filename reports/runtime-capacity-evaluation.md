# 실제 컨테이너 빌드·부하 평가 · 2026-10-01

사용자 확인 후 복구된 Docker 데몬에서 두 프로젝트의 격리된 복사본을 빌드·검사하고, 준비한 로컬 컨테이너에 세 번의 60초 부하를 가했다. 기존 사용자 컨테이너·프로젝트 소스·DB는 변경하지 않았다. source snapshot과 service label, immutable image ID, 실제 published port 및 자원 제한을 확인한 측정만 저장했다.

## 빌드와 실행 검사

- Temp_log: 제공 Dockerfile의 빌드 성공, 별도 테스트 이미지에서 원본 테스트 **17/17 통과**. 실행 Node 버전 **24.21.0** 확인. 앱 1 vCPU / 512 MiB, 별도 MongoDB 1 vCPU / 1 GiB. 합성 게시글 100개와 임시 credentials·볼륨을 사용했다.
- portpolio-production: npm 테스트 **9/9 통과**, Vite 빌드 성공. 제공 Nginx 설정과 산출물을 묶은 테스트 이미지에서 **nginx 1.30.5** 실행 확인. 0.5 vCPU / 64 MiB.
- 실행 컨테이너는 non-root, read-only root, cap-drop ALL, no-new-privileges, PID·CPU·메모리 제한을 사용했다. MongoDB는 독립 내부 네트워크로 연결하고 HTTP는 localhost에만 게시했다.
- 동일한 빌드 입력의 Docker cache를 재사용했다. 이 빌드 시간이나 cache 사용량으로 build-worker 사양을 산정하지 않았다.

## 관측 결과

Docker Desktop의 native ARM64 환경이며 VM에 10 vCPU / 약 7.65 GiB가 설정되어 있었다. CPU는 sample peak, 메모리는 Docker container working-set peak, 지연은 해당 GET 요청의 p95다.

| 대상 / 시나리오 | 실제 RPS | p95 | CPU peak | 메모리 peak | HTTP 결과 | 사양 근거 |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| Temp_log · 단일 클라이언트 GET /api/posts · 요청 20 RPS | 19.64 | 17.95 ms | 172.3m | 56.04 MiB | 200: 100 / 429: 1,100 | 오류율 91.67%로 제외 |
| Temp_log · 동일 경로 · 요청 1 RPS | 0.985 | 26.89 ms | 62.4m | 53.61 MiB | 200: 60, 오류 0% | 이 GET 시나리오의 footprint |
| 포트폴리오 · GET / · 요청 50 RPS | 49.06 | 14.78 ms | 37.2m | 3.477 MiB | 200: 3,000, 오류 0% | 이 static GET 시나리오의 footprint |

관측 종료·Docker stats 수집 시간이 포함되어 목표 RPS와 실제 RPS는 약간 다르다. 실패 응답의 낮은 CPU 사용량을 충분한 서버 용량의 근거로 사용하지 않았다.

Temp_log에는 `express-rate-limit`의 apiLimiter가 max=100 / windowMs=60,000으로 설정되어 있었다. 단일 client key의 요청 20 RPS는 이 설정을 초과한다. 이는 측정 시나리오와 운영 설정의 불일치이며 CPU 부족으로 확정할 수 없다. 여러 실제 클라이언트·trusted proxy·요청 mix를 반영한 별도 시험이 필요하다. 빠른 source 검사도 literal rate-limit 설정을 검토 항목과 줄 근거로 표시하도록 확장했다.

## 사양 보정 및 검증 개선

| 대상 | 정책 초기 requests | 측정 기반 초기 requests | limits | 적용 범위 |
| --- | --- | --- | --- | --- |
| Temp_log | 250m / 512 MiB | 100m / 128 MiB | 500m / 256 MiB | 합성 데이터의 1 RPS 공개 목록 GET |
| 포트폴리오 | 100m / 128 MiB | 100m / 64 MiB | 500m / 128 MiB | 측정한 static GET 부하 |

50% 자원 여유와 CPU 50m / memory 64MiB 단위 반올림을 적용했다. 이 값은 production 전체 workload의 적정 사양이 아니다. uploads/auth/writes, 실제 DB 규모, 다중 사용자·네트워크, target cloud hardware에서 재검증해야 한다.

고정 제공 부하의 achievedRps를 최대 처리 용량으로 해석하면 낮은 부하 시험에서 불필요하게 replicas를 늘릴 수 있음을 확인했다. Collector는 capacityValidated=false를 명시하며, 계획기는 이 자료로 footprint만 보정한다. 최대 용량·SLO를 별도로 검토한 capacityValidated=true 자료에만 처리량 기반 replica 계산을 허용한다. 검증한 시나리오보다 높은 명시적 요구에는 새 부하 시험 질문을 남긴다.

ARM64 대상과 같은 snapshot의 측정만 적용했다. failed 20 RPS 자료는 측정 기록으로 보존하되 사양 보정에서 제외했다. 배포 계획 상태는 필요한 이미지·Secret·네트워크·스토리지 등의 바인딩이 없어 needs_input이며 실행 설정은 blocked다. Terraform apply·Helm 설치·클라우드 배포는 수행하지 않았다.

정확한 snapshot·Git commit·컨테이너 및 image ID, 요청 조건, 원시 집계, 출처가 붙은 사양 비교는 [runtime-capacity-evaluation.json](runtime-capacity-evaluation.json)에 있다. 전체 산출물은 Git에서 제외한 `artifacts/runtime-bench/`에 보관했다. 기존 일반 분석 품질 보고서와 별도 평가다.
