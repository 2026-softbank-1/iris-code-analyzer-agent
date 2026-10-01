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
  pollTimer = null;
const history = new Map();
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
  $("progress-repo").textContent = job.repository || "저장소 분석 중";
  const stage = ["queued"].includes(job.stage)
    ? 0
    : ["succeeded", "needs_input", "unsupported", "finished"].includes(
          job.stage,
        )
      ? 4
      : Math.max(
          0,
          STAGES.findIndex(([key]) => key === job.stage),
        );
  $("progress-stages").innerHTML = STAGES.map(
    ([key, label], index) =>
      `<div class="${index === stage ? "current" : index < stage ? "done" : ""}">${index + 1}. ${!job.useAi && key === "analyzing" ? "정적 리뷰" : label}</div>`,
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
function renderResult(job) {
  $("progress-panel").hidden = true;
  $("empty").hidden = true;
  $("results").hidden = false;
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
    `<div class="result-top"><div><div class="eyebrow">REPOSITORY REVIEW</div><h2>${escape(job.repository)}</h2><div class="commit">${escape(job.ref)} &nbsp; / &nbsp; ${escape(job.sourceSha?.slice(0, 12))} &nbsp; / &nbsp; ${job.useAi ? "AI 보완" : "정적 분석"}</div></div><div><span class="status-badge ${escape(result.status)}">${STATUS[result.status]}</span></div></div><div class="metrics"><div class="metric"><small>APP SERVICES</small><strong>${result.services.length}</strong></div><div class="metric"><small>HTTP ROUTES</small><strong>${result.apiRoutes.length}</strong></div><div class="metric"><small>ENVIRONMENT KEYS</small><strong>${env.length}</strong></div><div class="metric"><small>REVIEW ITEMS</small><strong>${result.questions.length}</strong></div></div><div class="section-title">SERVICE STRUCTURE</div><div class="service-grid">${services || '<div class="detail-card detail-muted">이 저장소의 실행 단위는 초기 지원 범위에서 확정하지 못했습니다. Node/Vite/Express/Docker/Compose를 지원합니다.</div>'}</div><div class="result-columns"><div class="detail-card"><h3>HTTP routes <span class="field-status">코드 내부 경로</span></h3>${routes || '<p class="detail-muted">관측한 HTTP 경로가 없습니다.</p>'}</div><div><div class="detail-card"><h3>Environment keys</h3><div class="tags">${env.map((key) => `<span class="tag">${escape(key)}</span>`).join("") || '<p class="detail-muted">관측한 환경변수가 없습니다.</p>'}</div></div><div class="detail-card question-card"><h3>Dependencies & connections</h3>${[...result.dependencies, ...result.connections].map((item) => `<div class="question">${field(item, true)}</div>`).join("") || '<p class="detail-muted">추가 의존성 관측이 없습니다.</p>'}</div></div></div><div class="detail-card question-card"><h3>확인할 항목</h3>${questions || '<p class="detail-muted">현재 분석 프로필의 필수 정보가 채워졌습니다.</p>'}</div><div class="result-top result-note"><span>근거 버튼 ↗으로 원문을 확인하세요. 분석 결과는 실제 빌드·실행 성공을 의미하지 않습니다.</span><a class="download" id="download-result" href="/api/reviews/${escape(job.id)}/result" download="iris-review.json">JSON 다운로드 ↓</a></div>`;
  $("results")
    .querySelectorAll("[data-evidence]")
    .forEach((button) =>
      button.addEventListener("click", () =>
        showEvidence(button.dataset.evidence),
      ),
    );
}
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
    } else if (job.state === "failed") {
      $("progress-panel").hidden = true;
      $("submit").disabled = false;
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
      }),
    });
    activeJob = job.id;
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
    $("model-label").textContent = config.modelAvailable
      ? "Hive · " + config.model.split("/").pop()
      : "정적 분석 · AI 키 설정 필요";
    if (!config.modelAvailable) {
      document.querySelector('[data-mode="ai"]').disabled = true;
      setMode("static");
    }
  })
  .catch((error) => alertMessage(error.message));

const restoredReview = location.hash.match(/^#review=([a-f0-9]{24})$/);
if (restoredReview) {
  activeJob = restoredReview[1];
  poll();
}
