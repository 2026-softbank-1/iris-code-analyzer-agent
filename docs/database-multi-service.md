# DB와 멀티 서비스 분석 (0.1.1)

요청 `iris.analysis-gate-request.v1`과 응답 `iris.analysis-gate.v1`의 필드를 그대로 유지한다. DB가 있는 단일 앱도 이제 `analyze`로 보내 기존 WAS 스택 적용 경로에서 검토한다. DB 없는 단일 앱은 기존 `skip` 경로를 유지한다.

| 근거 | 처리 |
|---|---|
| DB 라이브러리 선언만 있음 | 생성하지 않고 `database_usage_unconfirmed` 질문 |
| 지원하는 클라이언트 생성/연결 호출과 환경변수 | DB 후보와 기존 `env.binding` 추출; URL의 DB 이름·연결 옵션 유지 |
| Compose의 명시된 DB 또는 연결 URL | 설정에 근거한 후보/호스트 연결 |
| 외부 URL/호스트 | 내부 DB로 바꾸지 않고 binding=null, 외부 설정 확인; 해당 연결의 사용자·비밀번호 키도 자동 교체하지 않음 |
| 코드에 직접 적힌 내부 DB URL | 환경변수로 전환할 때까지 적용 거절; 비밀값을 출력하지 않음 |
| 같은 엔진의 DB 여러 개 | 개별 ID 유지, 정확한 호스트로 선택; 모호하면 질문 |
| 파일 SQLite 연결 | 별도 DB 컨테이너 없음; 영속 경로·볼륨·쓰기 정책 확인 질문 (`:memory:`는 제외) |
| 여러 Dockerfile/Compose build | 각각의 context, target, command와 의존 관계 유지 |
| 순환 의존 관계 | `dependency_cycle` 질문; WAS는 순서 배포를 거절 |

JavaScript/TypeScript에서는 pg, postgres, mongoose, mongodb, redis, ioredis, mysql/mysql2, SQLite의 가져온 생성자/연결 함수를 확인한다. Python에서는 psycopg, psycopg2, asyncpg, pymongo, motor, redis.asyncio, pymysql, sqlite3를 확인한다. 별칭을 처리하고 재할당·매개변수로 가려진 식별자는 제외한다. 디렉터리 깊이·파일 수·파일 크기 한도를 지키며 다른 배포 단위의 코드는 소유권에서 제외한다. 래퍼, ORM의 동적 구성, 재수출, Go/Java 등의 연결 추적은 일반화하지 않는다. 소스 호출은 실제 연결 성공을 뜻하지 않는다.

Railpack은 앱 이미지를 빌드한다. DB 생성·비밀값·네트워크·초기화 스크립트·순서 배포는 WAS와 배포 차트의 책임이다. LLM 프롬프트 `deployment_v2.2`도 같은 경계를 적용하지만, Gate 자체는 정적 분석으로 실행한다. LLM 제안만으로 리소스를 생성하지 않는다.

현재 API/차트는 앱의 SQLite 파일 볼륨과 단일 쓰기 배포 정책을 표현하지 않는다. WAS는 해당 unit의 apply를 기존 `INVALID_INPUT` 응답과 `sqlite_persistence_required` 상세 사유로 거절한다. 지원하지 않는 build.args와 없는 Dockerfile/stage도 조용히 누락시키지 않는다. DB 대상/구성을 추적할 수 없거나 엔진이 충돌하는 경우 먼저 해소해야 한다. 커스텀 DB 이미지는 공식 이미지로 조용히 대체하지 않고, 기존 `dependencies[].provision=true`를 명시한 경우에만 관리형 공식 이미지 사용을 허용한다(커스텀 Dockerfile 설정은 적용하지 않는다). HTTP 포트 없는 순수 워커의 상태 검사 정책은 추가 플랫폼 지원이 필요하다. 공식 DB 초기화 스크립트는 빈 DB의 첫 기동에만 실행하며 기존 DB의 업그레이드 마이그레이션으로 재실행하지 않는다.

실제 컨테이너 검증은 직접 작성한 fixture만 실행한다. 분석 중 사용자 코드를 실행하는 기능이 아니다.

```sh
PYTHONPATH=src python scripts/verify_db_containers.py --out /tmp/iris-container-validation
```

이 검증은 Dockerfile 두 개, web→api 통신, PostgreSQL/MongoDB 쓰기·읽기, 첫 SQL 초기화, DB 재시작 후 데이터 유지를 확인하고 자체 테스트 컨테이너·볼륨을 정리한다. AWS/Kubernetes 배포나 Railpack 빌드 검증을 대신하지 않는다.
