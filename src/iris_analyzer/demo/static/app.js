"use strict";
const $ = (id) => document.getElementById(id);
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const STAGES = [
  ["resolving", "소스 확인"],
  ["downloading", "다운로드"],
  ["preprocessing", "전처리"],
  ["analyzing", "AI 리뷰"],
  ["validating", "결과 검증"],
  ["checking", "초기 하자 검사"],
  ["planning", "배포 계획"],
];
const STATUS = {
  complete: "분석 완료",
  needs_input: "확인 필요",
  unsupported: "지원 확장 필요",
};
const ROLE = {
  web_api: "WEB + API",
  api: "API",
  server: "SERVER",
  static: "STATIC WEB",
  web: "WEB",
  frontend: "FRONTEND",
};
let useAi = true,
  activeJob = null,
  pollTimer = null,
  formJobId = null;
const history = new Map();
let providerModels = {};
function updateProviderLabel() {
  const selected = providerModels[$("ai-provider").value];
  $("model-label").textContent = selected?.available
    ? `${selected.id === "openai" ? "OpenAI" : "Hive"} · ${selected.model.split("/").pop()}`
    : "정적 분석 · 선택한 AI 키 설정 필요";
  document.querySelector('[data-mode="ai"]').disabled = !selected?.available;
  if (!selected?.available) setMode("static");
}
$("ai-provider").addEventListener("change", updateProviderLabel);
async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "요청을 처리하지 못했습니다.",
    );
  return data;
}
function alertMessage(message) {
  $("alert").textContent = message;
  $("alert").hidden = !message;
}
function setMode(mode) {
  useAi = mode === "ai";
  document.querySelectorAll("[data-mode]").forEach((button) => {
    button.classList.toggle("selected", button.dataset.mode === mode);
    button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
  });
}
document
  .querySelectorAll("[data-mode]")
  .forEach((button) =>
    button.addEventListener("click", () => setMode(button.dataset.mode)),
  );
function renderHistory() {
  $("history").innerHTML = [...history.values()]
    .reverse()
    .map(
      (job) =>
        `<button data-job="${escape(job.id)}" title="${escape(job.repositoryUrl)}">${job.state === "succeeded" ? "✓" : job.state === "failed" ? "×" : "◌"} &nbsp; ${escape(job.repository || job.repositoryUrl.replace("https://github.com/", ""))}</button>`,
    )
    .join("");
  $("history")
    .querySelectorAll("button")
    .forEach((button) =>
      button.addEventListener("click", () => {
        activeJob = button.dataset.job;
        formJobId = null;
        location.hash = "review=" + activeJob;
        clearTimeout(pollTimer);
        poll();
      }),
    );
}
function progress(job) {
  $("empty").hidden = true;
  $("results").hidden = true;
  $("progress-panel").hidden = false;
  $("replan-current").disabled = true;
  $("progress-repo").textContent = job.repository || "저장소 분석 중";
  const stage = ["queued"].includes(job.stage)
    ? 0
    : ["succeeded", "needs_input", "unsupported", "finished"].includes(
          job.stage,
        )
      ? STAGES.length - 1
      : Math.max(
          0,
          STAGES.findIndex(([key]) => key === job.stage),
        );
  $("progress-stages").innerHTML = STAGES.map(
    ([key, label], index) =>
      `<div class="${index === stage ? "current" : index < stage ? "done" : ""}">${index + 1}. ${key === "analyzing" && (job.result?.analysisMode === "static" || (!job.result && !job.useAi)) ? "정적 리뷰" : label}</div>`,
  ).join("");
  $("progress-detail").textContent =
    job.state === "queued"
      ? "앞선 분석이 완료되면 시작합니다."
      : job.sourceSha
        ? `소스 커밋 ${job.sourceSha.slice(0, 12)} · ${job.ref || ""}`
        : "고정된 GitHub 소스 버전을 확인합니다.";
}
function field(field, mono = false) {
  if (!field) return '<span class="detail-muted">확인 필요</span>';
  const value =
    field.status === "unknown"
      ? "확인 필요"
      : typeof field.value === "object"
        ? JSON.stringify(field.value)
        : field.value;
  const evidence = field.evidenceIds?.[0];
  return `<span class="${mono ? "command" : ""}" title="${escape(field.reason)}">${escape(value)}</span><span class="field-status">${field.status === "detected" ? "감지" : field.status === "suggested" ? "제안" : "미확정"}</span>${evidence ? `<button class="evidence-button" data-evidence="${escape(evidence)}" aria-label="원문 근거 보기">↗</button>` : ""}`;
}
function row(label, value) {
  return `<div class="data-row"><span>${label}</span><div class="field-value">${value}</div></div>`;
}
function planningRequest(replanning = false) {
  function number(id, label) {
    const raw = $(id).value.trim();
    if (!raw) return null;
    const value = Number(raw);
    if (!Number.isFinite(value) || value <= 0)
      throw new Error(`${label}은 0보다 큰 숫자로 입력하세요.`);
    return value;
  }
  const previous = replanning
    ? history.get(activeJob)?.deploymentDossier?.deploymentPlan?.request
    : null;
  const preserved = previous ? JSON.parse(JSON.stringify(previous)) : {};
  return {
    ...preserved,
    schemaVersion: "iris.planning-request.v1",
    target: {
      ...preserved.target,
      stack: $("plan-stack").value.trim() || null,
      cloud: $("plan-cloud").value.trim() || null,
      region: $("plan-region").value.trim() || null,
      architecture: $("plan-arch").value || null,
      environment: $("plan-environment").value,
    },
    constraints: {
      ...preserved.constraints,
      expectedRps: number("plan-rps", "예상 트래픽"),
      monthlyBudgetUsd: number("plan-budget", "월 예산"),
      availability: $("plan-availability").value || null,
    },
  };
}
function restoreConditions(job) {
  if (formJobId === job.id) return;
  const request = job.deploymentDossier?.deploymentPlan?.request;
  const target = request?.target || {};
  const constraints = request?.constraints || {};
  $("repo-url").value = job.repositoryUrl || "";
  $("repo-ref").value = job.ref || "";
  if (job.provider && providerModels[job.provider]) {
    $("ai-provider").value = job.provider;
    updateProviderLabel();
  }
  for (const [id, value] of [
    ["plan-stack", target.stack],
    ["plan-cloud", target.cloud],
    ["plan-region", target.region],
    ["plan-arch", target.architecture],
    ["plan-environment", target.environment || "test"],
    ["plan-availability", constraints.availability],
    ["plan-rps", constraints.expectedRps],
    ["plan-budget", constraints.monthlyBudgetUsd],
  ])
    $(id).value = value ?? "";
  formJobId = job.id;
}
function evidenceButton(ids, label = "원문 근거 보기") {
  return ids?.[0]
    ? `<button class="evidence-button" data-evidence="${escape(ids[0])}" aria-label="${escape(label)}">↗</button>`
    : "";
}
function valueText(value, fallback = "미확정") {
  return escape(
    value == null
      ? fallback
      : typeof value === "object"
        ? JSON.stringify(value)
        : value,
  );
}
function money(value) {
  return typeof value === "number" && Number.isFinite(value)
    ? `$${value.toLocaleString("en-US", { maximumFractionDigits: 2 })}`
    : "미확정";
}
function provenance(item) {
  const labels = {
    source: "코드 근거",
    user: "사용자 조건",
    measurement: "측정 반영",
    policy: "운영 가정",
  };
  return (item?.provenance?.basis || [])
    .map(
      (basis) => `<span class="scope">${escape(labels[basis] || basis)}</span>`,
    )
    .join("");
}
function jsonDetail(label, value) {
  return `<details class="json-detail"><summary>${escape(label)}</summary><pre>${escape(JSON.stringify(value, null, 2))}</pre></details>`;
}
function renderReadiness(readiness) {
  if (!readiness) return "";
  const coverage = readiness.coverage;
  const scopes = {
    source: "코드 선언",
    build: "빌드 이미지",
    runtime: "실행 이미지",
    container_stage_unknown: "이미지 단계 미확정",
  };
  const versions = readiness.runtimeVersions
    .map(
      (item) =>
        `<div class="runtime-row"><div><b>${escape(item.runtime)}</b><span class="scope">${escape(scopes[item.scope] || item.scope)}</span><span class="field-status">${item.status === "conflict" ? "충돌" : item.status === "unknown" ? "미확정" : "선언 관측"}</span></div><div class="command">${valueText(item.constraint, "버전 미확정")}${evidenceButton(item.evidenceIds)}</div><small>${escape(item.component)}${item.stage ? " · " + escape(item.stage) : ""} · ${escape(item.path || "")}</small>${item.imageReference ? `<div class="image-reference">${escape(item.imageReference)}</div>` : ""}</div>`,
    )
    .join("");
  const findings = readiness.findings
    .map(
      (item) =>
        `<div class="finding ${item.severity === "error" ? "finding-error" : ""}"><div><span class="scope">${item.severity === "error" ? "오류 신호" : "검토 항목"}</span><b>${escape(item.ruleId)}</b>${evidenceButton(item.evidenceIds)}</div><small>${escape(item.path)} · ${item.startLine}–${item.endLine}행</small><p>${escape(item.reason)}</p></div>`,
    )
    .join("");
  return `<div class="section-title">SOURCE READINESS · 사실과 초기 검사</div><div class="detail-card"><div class="plan-section-head"><h3>언어 · 런타임 선언</h3><span class="scope">소스 실행 없음</span></div><div class="tags">${readiness.languages.map((item) => `<span class="tag">${escape(item.language)}${evidenceButton(item.evidenceIds)}</span>`).join("") || '<span class="detail-muted">제공된 근거에서 언어를 확정하지 못했습니다.</span>'}</div><div class="runtime-grid">${versions || '<p class="detail-muted">런타임 버전 선언이 제공되지 않았습니다.</p>'}</div><div class="coverage-note">구문 검사 <b>${coverage.syntaxCheckedFiles.length}</b>개 · JSON <b>${coverage.jsonCheckedFiles.length}</b>개 · 컨테이너 설정 <b>${coverage.containerCheckedFiles.length}</b>개 · 검사 보류 <b>${coverage.skippedFiles.length}</b>개 <span class="scope">${coverage.status === "partial" ? "부분 검사" : "선택 파일 검사"}</span></div><p class="detail-muted">전체 소스가 제공되고 해시로 확인된 파일만 검사했습니다. 일부 근거·마스킹·검사 범위 밖의 코드는 남아 있을 수 있습니다.</p>${findings || '<p class="detail-muted">검사한 범위에서 초기 하자 신호가 발견되지 않았습니다. 빌드·보안·운영 성공을 보장하지 않습니다.</p>'}${jsonDetail("검사 범위와 보류 사유", coverage)}</div>`;
}
function renderWorkload(workload) {
  const resources = workload.resources.value;
  const service = workload.service.value;
  const ingress = workload.ingress.value;
  const probes = workload.probes.value;
  const probeLabel = (probe) =>
    probe
      ? `${probe.kind === "http" ? "HTTP " + probe.path : "TCP"} :${probe.port}`
      : "미확정";
  const volumes = workload.volumes.value || [];
  const secrets = workload.secretRefs.value || [];
  return `<article class="service-card"><div class="service-head"><span class="service-name">${escape(workload.name)}</span><span class="role-badge">KUBERNETES</span></div>${row("Requests", `${resources.requests.cpuMillicores}m CPU / ${resources.requests.memoryMiB} MiB`)}${row("Limits", `${resources.limits.cpuMillicores}m CPU / ${resources.limits.memoryMiB} MiB`)}${row("Replicas", valueText(workload.replicas.value))}<div class="planning-reason">${provenance(workload.resources)}<p>${escape(workload.resources.provenance.reason)}</p></div>${row("Service", service ? `${escape(service.type)} · ${service.port} → ${service.targetPort}` : "미확정")}${row("Ingress", ingress?.enabled ? `${escape(ingress.host)} · TLS ${escape(ingress.tlsSecretName || "미확정")}` : "비공개 · ClusterIP")}${row("Readiness", escape(probeLabel(probes.readiness)))}${row("Liveness", escape(probeLabel(probes.liveness)))}${row("Startup", escape(probeLabel(probes.startup)))}${row("PVC / 볼륨", volumes.length ? volumes.map((item) => `${escape(item.mountPath)} → ${escape(item.claimName || "Claim 미확정")} (${escape(item.accessMode)})`).join("<br>") : "추가 볼륨 없음")}${row("Secret 참조", secrets.length ? secrets.map((item) => `${escape(item.environmentKey)} → ${escape(item.name)} / ${escape(item.key)}`).join("<br>") : "참조 미제공")}${row("Rollout", `${valueText(workload.rollout.value.strategy)} · surge ${valueText(workload.rollout.value.maxSurge)} / unavailable ${valueText(workload.rollout.value.maxUnavailable)}`)}${row("Rollback", valueText(workload.rollback.value.strategy))}${jsonDetail("워크로드 실행 설정 전체", workload)}</article>`;
}
function renderDossier(job) {
  const dossier = job.deploymentDossier;
  if (!dossier)
    return `<div class="detail-card planning-empty"><h3>배포 계획을 추가해 보세요.</h3><p class="detail-muted">이 리뷰에는 아직 배포 계획이 없습니다. 기존 소스 분석을 재사용해 추천 사양과 실행 설정을 준비할 수 있습니다.</p><button class="secondary-button" data-replan>현재 조건으로 계획 생성 ↻</button></div>`;
  const plan = dossier.deploymentPlan;
  const recommendations = plan.recommendations;
  const target = recommendations.target.value;
  const instance = recommendations.instance.value;
  const policy = recommendations.operatingPolicy?.value;
  const cost = recommendations.cost.value;
  const known = cost.lineItems.filter(
    (item) => typeof item.monthlyUsd === "number",
  );
  const subtotal = known.reduce((sum, item) => sum + item.monthlyUsd, 0);
  const costs = {
    control_plane: "EKS 컨트롤 플레인",
    workers: "워커 인스턴스",
    node_disk: "노드 디스크",
    network: "네트워크",
    data: "DB · 데이터 · 백업",
  };
  const constraints = plan.request.constraints;
  const questions = plan.questions
    .map(
      (item) =>
        `<div class="question"><b>${item.requiredForExecution ? "실행 설정에 필요" : "운영 검토"} · ${escape(item.field)}</b>${escape(item.reason)}</div>`,
    )
    .join("");
  const assumptions = plan.assumptions
    .map(
      (item) =>
        `<div class="question"><b>${escape(item.description)}</b>${escape(item.impact)}</div>`,
    )
    .join("");
  const execution = dossier.execution;
  return `${renderReadiness(dossier.sourceReadiness)}<div class="section-title">DEPLOYMENT PLAN · 추천과 운영 가정</div><div class="plan-banner"><div><h3>${plan.status === "ready" ? "실행 입력이 채워진 계획" : plan.status === "unsupported" ? "요청 스택의 어댑터가 필요합니다" : "초기 추천 계획이 준비되었습니다"}</h3><p>${plan.plannerMode === "ai" ? "AI 운영 제안" : "기본 정책 제안"} · 필요한 바인딩과 측정값을 채워 계획을 구체화하세요.</p></div><span class="status-badge ${plan.status === "ready" ? "complete" : plan.status === "unsupported" ? "unsupported" : "needs_input"}">${plan.status === "ready" ? "계획 입력 완료" : plan.status === "unsupported" ? "스택 확장 필요" : "실행 입력 확인 필요"}</span></div><div class="plan-summary-grid"><div class="detail-card"><h3>추천 배포 스택 ${provenance(recommendations.target)}</h3>${row("Stack", valueText(target.stack))}${row("Cloud / region", `${valueText(target.cloud)} / ${valueText(target.region)}`)}${row("Architecture", valueText(target.architecture))}${row("Environment", target.environment === "production" ? "프로덕션" : "테스트")}<p class="planning-reason">${escape(recommendations.target.provenance.reason)}</p><h3 class="subheading">인스턴스 ${provenance(recommendations.instance)}</h3>${row("Instance", instance ? valueText(instance.instanceType) : "기존 클러스터 / 미확정")}${instance ? row("Capacity", `${instance.cpuMillicores / 1000} vCPU / ${instance.memoryMiB} MiB · 노드 ${instance.minNodes}–${instance.maxNodes}`) : ""}<p class="planning-reason">${escape(recommendations.instance.provenance.reason)}</p></div><div class="detail-card"><h3>운영 시나리오 <span class="scope">측정 전 가정</span></h3>${policy ? `${row("Traffic", `${policy.expectedRps} RPS <span class="scope">${constraints.expectedRps == null ? "초기 가정" : "사용자 조건"}</span>`)}${row("Availability", `${policy.availability === "multi_az" ? "여러 AZ" : "단일 AZ"} <span class="scope">${constraints.availability == null ? "초기 가정" : "사용자 조건"}</span>`)}${row("사용자 월 상한", policy.userMonthlyBudgetUsd == null ? "미제공" : money(policy.userMonthlyBudgetUsd))}${row("제안 월 예산", `${money(policy.suggestedMonthlyBudgetUsd)} <span class="scope">임시 비용 여유분</span>`)}<p class="planning-reason">${escape(recommendations.operatingPolicy.provenance.reason)}</p>` : '<p class="detail-muted">운영 시나리오는 계획 JSON에서 확인하세요.</p>'}<div class="cost-divider"><h3>예상 월 비용 · USD</h3>${cost.lineItems.map((item) => `${row(escape(costs[item.key] || item.key), item.monthlyUsd == null ? '<span class="detail-muted">견적 필요</span>' : money(item.monthlyUsd))}<p class="cost-reason">${escape(item.reason)}</p>`).join("")}${row("확인된 항목 소계", known.length ? money(subtotal) : "미확정")}${row("전체 월 비용", cost.monthlyTotalUsd == null ? '<b class="cost-unknown">미확정 · 미산정 항목 포함</b>' : money(cost.monthlyTotalUsd))}<p class="detail-muted">소계는 전체 비용의 하한입니다. 트래픽·DB·백업 등 미산정 비용과 실제 청구 조건을 확인해야 합니다.</p></div></div></div><div class="section-title">WORKLOAD CONFIGURATION · 사양과 Kubernetes 설정</div><div class="service-grid">${plan.configuration.workloads.map(renderWorkload).join("") || '<p class="detail-muted">확정된 실행 단위가 없습니다.</p>'}</div><div class="detail-card question-card"><h3>가정과 확인할 입력</h3><details><summary>운영 가정 ${plan.assumptions.length}개</summary>${assumptions}</details><div class="plan-questions">${questions || '<p class="detail-muted">필수 실행 입력이 채워졌습니다. 측정 범위와 운영 요구는 계속 검증해야 합니다.</p>'}</div></div><div class="detail-card question-card execution-card"><div class="plan-section-head"><h3>실행용 설정 · 고정 템플릿</h3><span class="status-badge ${execution.executionEligible ? "complete" : "needs_input"}">${execution.executionEligible ? "템플릿 변환 가능" : "템플릿 변환 보류"}</span></div><p class="detail-muted">템플릿 변환 가능은 배포 승인을 의미하지 않습니다. Terraform·Helm·클러스터 실행은 별도 수행 단계입니다.</p>${execution.blockedReasons.map((item) => `<div class="question"><b>${escape(item.code)} · ${escape(item.path)}</b>${escape(item.message)}</div>`).join("")}${jsonDetail("네트워크 · DB · 디스크 · 실행 설정", { recommendations: { network: recommendations.network, databases: recommendations.databases, storage: recommendations.storage, observability: recommendations.observability }, execution })}${jsonDetail("측정 요청 · 빌드/테스트와 운영 부하 구분", dossier.benchmarkPlan)}<div class="plan-actions"><button class="secondary-button" data-edit-conditions>조건 변경 ↑</button><button class="secondary-button" data-replan>현재 조건으로 계획 다시 생성 ↻</button><a class="download" href="/api/reviews/${escape(job.id)}/plan" download="iris-deployment-dossier.json">배포 계획 JSON ↓</a></div></div>`;
}
function renderResult(job) {
  $("progress-panel").hidden = true;
  $("empty").hidden = true;
  $("results").hidden = false;
  restoreConditions(job);
  $("replan-controls").hidden = false;
  $("replan-current").disabled = false;
  const result = job.result.analysisResult;
  const env = [
    ...new Set(
      result.environmentKeys.map((item) => item.value).filter(Boolean),
    ),
  ];
  const services = result.services
    .map(
      (service) =>
        `<article class="service-card"><div class="service-head"><span class="service-name">${escape(service.root.value || service.serviceId)}</span><span class="role-badge">${escape(ROLE[service.role.value] || service.role.value)}</span></div>${row("Runtime", field(service.runtime))}${row("Build", field(service.buildCommand, true))}${row("Start", field(service.startCommand, true))}${row("Working directory", field(service.workingDirectory, true))}${row("Output", field(service.outputDirectory, true))}${row("Health checks", service.healthchecks.map((item) => field(item, true)).join("<br>") || '<span class="detail-muted">관측 없음</span>')}${row("Ports", service.ports.length ? service.ports.map((port) => field(port) + `<span class="scope">${escape(port.scope)}</span>`).join("<br>") : '<span class="detail-muted">관측 없음</span>')}<div class="result-note">모듈: ${escape(service.componentRoots.join(" · "))}</div></article>`,
    )
    .join("");
  const routes = result.apiRoutes
    .map(
      (route) =>
        `<div class="route-row"><span class="http-method">${escape(route.value?.method || "HTTP")}</span><span class="route-path">${escape(route.value?.path || "확인 필요")}</span>${route.evidenceIds?.[0] ? `<button class="evidence-button" data-evidence="${escape(route.evidenceIds[0])}" aria-label="API 원문 근거 보기">↗</button>` : ""}</div>`,
    )
    .join("");
  const questions = result.questions
    .map(
      (question) =>
        `<div class="question"><b>${question.kind === "user_configuration" ? "설정 확인" : "코드 확인"} · ${escape(question.key)}</b>${escape(question.reason)}</div>`,
    )
    .join("");
  $("results").innerHTML =
    `<div class="result-top"><div><div class="eyebrow">REPOSITORY REVIEW</div><h2>${escape(job.repository)}</h2><div class="commit">${escape(job.ref)} &nbsp; / &nbsp; ${escape(job.sourceSha?.slice(0, 12))} &nbsp; / &nbsp; ${job.result.analysisMode === "opencode" ? "AI 소스 보완" : "정적 소스 분석"}</div></div><div><span class="status-badge ${escape(result.status)}">${STATUS[result.status]}</span></div></div><div class="metrics"><div class="metric"><small>APP SERVICES</small><strong>${result.services.length}</strong></div><div class="metric"><small>HTTP ROUTES</small><strong>${result.apiRoutes.length}</strong></div><div class="metric"><small>ENVIRONMENT KEYS</small><strong>${env.length}</strong></div><div class="metric"><small>REVIEW ITEMS</small><strong>${result.questions.length}</strong></div></div><div class="section-title">SERVICE STRUCTURE</div><div class="service-grid">${services || '<div class="detail-card detail-muted">이 저장소의 실행 단위는 초기 지원 범위에서 확정하지 못했습니다. Node/Vite/Express/Docker/Compose를 지원합니다.</div>'}</div><div class="result-columns"><div class="detail-card"><h3>HTTP routes <span class="field-status">코드 내부 경로</span></h3>${routes || '<p class="detail-muted">관측한 HTTP 경로가 없습니다.</p>'}</div><div><div class="detail-card"><h3>Environment keys</h3><div class="tags">${env.map((key) => `<span class="tag">${escape(key)}</span>`).join("") || '<p class="detail-muted">관측한 환경변수가 없습니다.</p>'}</div></div><div class="detail-card question-card"><h3>Dependencies & connections</h3>${[...result.dependencies, ...result.connections].map((item) => `<div class="question">${field(item, true)}</div>`).join("") || '<p class="detail-muted">추가 의존성 관측이 없습니다.</p>'}</div></div></div><div class="detail-card question-card"><h3>확인할 항목</h3>${questions || '<p class="detail-muted">현재 분석 프로필의 필수 정보가 채워졌습니다.</p>'}</div><div class="result-top result-note"><span>근거 버튼 ↗으로 원문을 확인하세요. 분석 결과는 실제 빌드·실행 성공을 의미하지 않습니다.</span><a class="download" id="download-result" href="/api/reviews/${escape(job.id)}/result" download="iris-review.json">리뷰 JSON ↓</a></div>${renderDossier(job)}`;
  $("results")
    .querySelectorAll("[data-evidence]")
    .forEach((button) =>
      button.addEventListener("click", () =>
        showEvidence(button.dataset.evidence),
      ),
    );
  $("results")
    .querySelectorAll("[data-replan]")
    .forEach((button) => button.addEventListener("click", replan));
  $("results")
    .querySelectorAll("[data-edit-conditions]")
    .forEach((button) =>
      button.addEventListener("click", () => {
        $("deployment-conditions").open = true;
        $("deployment-conditions").scrollIntoView({
          behavior: "smooth",
          block: "center",
        });
        $("plan-stack").focus({ preventScroll: true });
      }),
    );
}
async function replan() {
  if (!activeJob) return;
  alertMessage("");
  const id = activeJob;
  try {
    const planning = planningRequest(true);
    clearTimeout(pollTimer);
    $("submit").disabled = true;
    $("replan-current").disabled = true;
    const job = await api(`/api/reviews/${id}/plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        planning_request: planning,
        use_ai: useAi,
        provider: $("ai-provider").value,
      }),
    });
    if (id !== activeJob) return;
    formJobId = id;
    history.set(id, job);
    renderHistory();
    progress(job);
    poll();
  } catch (error) {
    alertMessage(error.message);
    $("submit").disabled = false;
    $("replan-current").disabled = false;
  }
}
$("replan-current").addEventListener("click", replan);
async function showEvidence(id) {
  try {
    const item = await api(
      `/api/reviews/${activeJob}/evidence/${encodeURIComponent(id)}`,
    );
    $("evidence-title").textContent = item.path;
    $("evidence-meta").textContent =
      `Lines ${item.startLine}–${item.endLine}${item.redacted ? " · 일부 값 마스킹" : ""}`;
    $("evidence-code").textContent = item.text;
    $("evidence-dialog").showModal();
  } catch (error) {
    alertMessage(error.message);
  }
}
$("close-dialog").addEventListener("click", () => $("evidence-dialog").close());
async function poll() {
  const id = activeJob;
  try {
    const job = await api(`/api/reviews/${id}`);
    if (id !== activeJob) return;
    history.set(id, job);
    renderHistory();
    if (job.state === "succeeded") {
      renderResult(job);
      $("submit").disabled = false;
      $("replan-current").disabled = false;
    } else if (job.state === "failed") {
      $("progress-panel").hidden = true;
      $("submit").disabled = false;
      $("replan-current").disabled = false;
      if (job.result?.analysisResult) renderResult(job);
      alertMessage(
        `${job.error?.message || "분석 실패"} (${job.error?.code || "REVIEW_FAILED"})`,
      );
    } else {
      progress(job);
      pollTimer = setTimeout(poll, 1200);
    }
  } catch (error) {
    alertMessage(error.message);
    $("submit").disabled = false;
    $("replan-current").disabled = false;
  }
}
$("review-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  alertMessage("");
  clearTimeout(pollTimer);
  $("submit").disabled = true;
  try {
    const job = await api("/api/reviews", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        repository_url: $("repo-url").value.trim(),
        ref: $("repo-ref").value.trim() || null,
        use_ai: useAi,
        provider: $("ai-provider").value,
        planning_request: planningRequest(),
      }),
    });
    activeJob = job.id;
    formJobId = job.id;
    location.hash = "review=" + activeJob;
    history.set(job.id, job);
    renderHistory();
    progress(job);
    poll();
  } catch (error) {
    alertMessage(error.message);
    $("submit").disabled = false;
  }
});
api("/api/config")
  .then((config) => {
    providerModels = Object.fromEntries(
      (
        config.providers || [
          {
            id: config.provider,
            model: config.model,
            available: config.modelAvailable,
          },
        ]
      ).map((p) => [p.id, p]),
    );
    $("ai-provider").value =
      history.get(activeJob)?.provider || config.provider;
    updateProviderLabel();
  })
  .catch((error) => alertMessage(error.message));

const restoredReview = location.hash.match(/^#review=([a-f0-9]{24})$/);
if (restoredReview) {
  activeJob = restoredReview[1];
  poll();
}
