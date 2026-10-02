# Organization source fixture

세 개의 별도 레포를 나타내는 로컬 시연 입력입니다.

- catalog-api: Express API와 source Dockerfile
- shop-web: Vite frontend와 build-time API URL 필요
- platform-infra: 기존 Terraform 설정. 자동 apply 대상 아님

`sources.json`의 commit SHA는 synthetic caller-attested 값입니다. GitHub에서 검증한 실제 commit이 아닙니다. `request.json`은 목적과 기존 Kubernetes context를 담지만 이미지 registry/public URL/실제 Secret 등의 운영 입력을 채우지 않아 질문이 남는 것을 기대합니다. `index.html` 파일을 직접 열어 전체 시스템이 배포됐다고 판단하지 않습니다.

저장소 root에서:

```sh
uv run iris-organization demo-org \
  --sources fixtures/organization-demo/sources.json \
  --request fixtures/organization-demo/request.json \
  --out artifacts/organization-demo
```

실행 결과의 정확한 component IDs로 registry/port/connection/public endpoint를 입력한 request를 만들어 `iris-organization plan`으로 재계획합니다. 실제 source 모델·빌드·클러스터 실행은 이 fixture를 추가하는 작업에서 수행하지 않았습니다.
