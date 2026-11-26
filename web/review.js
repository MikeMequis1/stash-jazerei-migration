"use strict";

const state = {
  data: null,
  selected: null,
  reviewTab: "match",
  matchFilter: "attention",
  likedFilter: "pending",
  confidence: "all",
  query: "",
  page: 1,
  pageSize: 25,
  selectedForBatch: new Set(),
  details: new Map(),
  modal: null,
};

const REVIEW_PREFS_KEY = "jz-review-prefs-v1";

function loadReviewPrefs() {
  try {
    const parsed = JSON.parse(localStorage.getItem(REVIEW_PREFS_KEY) || "null");
    if (!parsed) return;
    if (["match", "liked"].includes(parsed.reviewTab)) state.reviewTab = parsed.reviewTab;
    if (parsed.matchFilter) state.matchFilter = String(parsed.matchFilter);
    if (parsed.likedFilter) state.likedFilter = String(parsed.likedFilter);
    if (parsed.confidence) state.confidence = String(parsed.confidence);
    if (Number.isFinite(parsed.pageSize) && [25, 50].includes(Number(parsed.pageSize))) {
      state.pageSize = Number(parsed.pageSize);
    }
  } catch {
    /* preferencias invalidas nao bloqueiam a revisao */
  }
}

function saveReviewPrefs() {
  try {
    localStorage.setItem(
      REVIEW_PREFS_KEY,
      JSON.stringify({
        reviewTab: state.reviewTab,
        matchFilter: state.matchFilter,
        likedFilter: state.likedFilter,
        confidence: state.confidence,
        pageSize: state.pageSize,
      }),
    );
  } catch {
    /* localStorage indisponivel */
  }
}

loadReviewPrefs();

const wizard = {
  data: null,
  detail: null,
  detailError: null,
  stage: "prepare",
  job: null,
  jobTimer: null,
  runTimer: null,
  error: null,
  report: null,
  reportError: null,
  reportValid: null,
  selectedVerificationId: null,
  reportFilter: "all",
  selectedReportAction: null,
  advancedDetail: null,
  advancedError: null,
};

class ApiError extends Error {
  constructor(message, body = {}) {
    super(message);
    this.name = "ApiError";
    this.body = body;
  }
}

const MATCH_FILTERS = [
  ["attention", "Precisa de decisão"],
  ["all", "Todas"],
  ["candidate", "Candidatos"],
  ["manual", "Revisão manual"],
  ["no_candidate", "Sem candidato"],
  ["inconclusive", "Inconclusivas"],
  ["approved", "Aprovadas"],
  ["already_exists", "Já no destino"],
  ["special", "Edições especiais"],
  ["duplicate", "Títulos duplicados"],
  ["written_review", "Com texto"],
  ["rating_only", "Somente nota"],
  ["history_conflict", "Conflito histórico"],
  ["batch_ready", "Prontos para lote"],
];

const LIKED_FILTERS = [
  ["pending", "Não decididos"],
  ["decided", "Decididos"],
  ["reconfirm", "Reconfirmação"],
  ["awaiting_identity", "Aguardando identidade"],
  ["all", "Todos"],
];

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function toast(message, isError = false) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.className = "toast show" + (isError ? " error" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (node.className = "toast"), 5600);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(body.error || `HTTP ${response.status}`, body);
  }
  return body;
}

function badge(value, label = value) {
  return `<span class="badge ${esc(value)}">${esc(label)}</span>`;
}

function statusLabel(value) {
  return (
    {
      done: "concluído",
      current: "atual",
      needed: "necessário",
      running: "em andamento",
      waiting_user: "aguardando você",
      needs_confirmation: "requer confirmação",
      pending: "pendente",
      blocked: "bloqueado",
      failed: "falhou",
      skipped: "ignorado",
      prepared: "preparada",
      awaiting_confirmation: "aguardando confirmação",
      verified: "verificada",
      inconclusive: "inconclusiva",
      write_ambiguous: "ambígua",
      cancelled: "cancelada",
    }[value] || value
  );
}

function isActiveJob(job) {
  return Boolean(job && ["queued", "running", "waiting_user", "cancelling"].includes(job.status));
}

function fact(label, value) {
  return `<div class="fact"><span>${esc(label)}</span><strong>${value}</strong></div>`;
}

function stat(label, value) {
  return `<div class="stat"><b>${esc(value)}</b><span>${esc(label)}</span></div>`;
}

function currentGates() {
  return wizard.data?.config?.write_gates || {};
}

function gatesOn() {
  const gates = currentGates();
  return Boolean(gates.allow_writes && gates.enable_real_write_transport);
}

/* ---------- revisão ---------- */

async function loadState(selectSlug) {
  try {
    state.data = await api("/api/state");
    const wanted = selectSlug || state.selected;
    const exists = state.data.items.some((item) => item.slug === wanted);
    state.selected = exists ? wanted : state.data.items[0]?.slug || null;
    const visible = filteredItems();
    const index = visible.findIndex((item) => item.slug === state.selected);
    if (index >= 0) state.page = Math.floor(index / state.pageSize) + 1;
    renderReview();
  } catch (error) {
    state.data = null;
    const detail = document.getElementById("detail");
    if (detail) {
      detail.innerHTML = `<div class="danger"><strong>Não foi possível carregar a revisão.</strong>
        <div>${esc(error.message)}</div>
        <div class="history">Conclua a etapa 1 (Preparar) para gerar coleta, snapshot e correspondências.</div></div>`;
    }
    document.getElementById("game-list").innerHTML = "";
  }
}

function itemBySlug(slug) {
  return state.data?.items.find((item) => item.slug === slug) || null;
}

function filteredItems() {
  if (!state.data) return [];
  const query = state.query.trim().toLowerCase();
  const base = state.data.items.filter((item) => {
    if (
      state.reviewTab === "liked" &&
      item.evaluation_kind !== "rating_only" &&
      item.liked?.status !== "requires_reconfirmation"
    ) {
      return false;
    }
    if (query) {
      const haystack = `${item.title} ${item.slug} ${item.selected_candidate_id ?? ""}`.toLowerCase();
      if (!haystack.includes(query)) return false;
    }
    if (state.confidence !== "all") {
      const confidence = Number(item.candidates?.[0]?.confidence ?? -1);
      if (state.confidence === "high" && confidence < 0.9) return false;
      if (state.confidence === "medium" && (confidence < 0.8 || confidence >= 0.9)) return false;
      if (state.confidence === "low" && confidence >= 0.8) return false;
    }
    return true;
  });

  if (state.reviewTab === "liked") {
    return base.filter((item) => {
      const status = item.liked?.status || "unresolved";
      if (state.likedFilter === "all") return true;
      if (state.likedFilter === "decided") return status === "decided";
      if (state.likedFilter === "reconfirm") return status === "requires_reconfirmation";
      if (state.likedFilter === "awaiting_identity") {
        return status === "decided" && item.liked?.binding_status === "pending_identity";
      }
      return status === "unresolved" || status === "stale";
    });
  }

  const filter = state.matchFilter;
  return base.filter((item) => {
    if (filter === "all") return true;
    if (filter === "attention") return item.needs_review && !item.destination_present;
    if (filter === "candidate") return item.match_status === "candidate" || item.match_status === "manual_review";
    if (filter === "manual") return item.match_status === "manual_review" || item.initial_classification === "ambiguous";
    if (filter === "no_candidate") return item.match_status === "no_candidate";
    if (filter === "inconclusive") return item.match_status === "inconclusive" || item.match_status === "error" || item.match_status === "not_run";
    if (filter === "approved") return item.match_decision === "approved";
    if (filter === "already_exists") return item.destination_present;
    if (filter === "special") return item.special_edition;
    if (filter === "duplicate") return item.duplicate_title;
    if (filter === "written_review") return item.evaluation_kind === "written_review";
    if (filter === "rating_only") return item.evaluation_kind === "rating_only";
    if (filter === "history_conflict") return item.history_conflict;
    if (filter === "batch_ready") return item.batch_eligible;
    return item.review_status === filter;
  });
}

function pageItems() {
  const items = filteredItems();
  const pages = Math.max(1, Math.ceil(items.length / state.pageSize));
  if (state.page > pages) state.page = pages;
  return items.slice((state.page - 1) * state.pageSize, state.page * state.pageSize);
}

function currentFilter() {
  return state.reviewTab === "liked" ? state.likedFilter : state.matchFilter;
}

function setFilter(value) {
  if (state.reviewTab === "liked") state.likedFilter = value;
  else state.matchFilter = value;
  state.page = 1;
  state.selectedForBatch.clear();
  saveReviewPrefs();
  renderReview();
}

function renderReview() {
  renderReviewTabs();
  renderProgress();
  renderBatchBar();
  renderSummary();
  renderFilters();
  renderList();
  renderPagination();
  renderDetail();
}

function renderReviewTabs() {
  const node = document.getElementById("review-tabs");
  if (!node) return;
  const tabs = [
    ["match", "Correspondências"],
    ["liked", "Liked"],
  ];
  node.innerHTML = tabs
    .map(
      ([key, label]) =>
        `<button role="tab" aria-selected="${state.reviewTab === key}" data-review-tab="${key}" class="${state.reviewTab === key ? "active" : ""}">${label}</button>`,
    )
    .join("");
  node.querySelectorAll("[data-review-tab]").forEach((button) => {
    button.onclick = () => {
      state.reviewTab = button.dataset.reviewTab;
      state.selected = null;
      state.page = 1;
      state.selectedForBatch.clear();
      saveReviewPrefs();
      renderReview();
    };
  });
}

function renderProgress() {
  const node = document.getElementById("review-progress");
  const summary = state.data?.summary;
  if (!node || !summary) return;
  if (state.reviewTab === "liked") {
    const ratingItems = (state.data?.items || []).filter((item) => item.evaluation_kind === "rating_only");
    const decided = ratingItems.filter((item) => item.liked?.status === "decided").length;
    const reconfirm = (state.data?.items || []).filter((item) => item.liked?.status === "requires_reconfirmation").length;
    node.textContent = `${decided}/${ratingItems.length} liked decididos · ${reconfirm} para reconfirmar · ${summary.liked_pending_identity || 0} aguardando identidade`;
    return;
  }
  node.textContent = `${summary.eligible_now || 0} elegíveis agora · ${summary.batch_eligible || 0} prontos para lote · ${summary.edition_unconfirmed || 0} edições a confirmar · ${summary.history_conflicts || 0} conflitos históricos`;
}

function renderBatchBar() {
  const node = document.getElementById("review-batch");
  if (!node) return;
  if (state.reviewTab !== "match" || state.selectedForBatch.size === 0) {
    node.hidden = true;
    node.innerHTML = "";
    return;
  }
  node.hidden = false;
  node.innerHTML = `
    <strong>${state.selectedForBatch.size} selecionado(s)</strong>
    <button id="batch-confirm" class="ok">Aprovar selecionados</button>
    <button id="batch-clear" class="ghost">Limpar seleção</button>
    <span class="history">Lote somente nesta página; edições especiais e ambiguidades ficam fora.</span>
  `;
  document.getElementById("batch-clear").onclick = () => {
    state.selectedForBatch.clear();
    renderReview();
  };
  document.getElementById("batch-confirm").onclick = openBatchConfirm;
}

function renderPagination() {
  const node = document.getElementById("pagination");
  if (!node || !state.data) return;
  const items = filteredItems();
  const pages = Math.max(1, Math.ceil(items.length / state.pageSize));
  node.innerHTML = `
    <button id="page-prev" ${state.page <= 1 ? "disabled" : ""}>Anterior</button>
    <span>${state.page} de ${pages} · ${items.length} item(ns)</span>
    <button id="page-next" ${state.page >= pages ? "disabled" : ""}>Próxima</button>
    <select id="page-size" aria-label="Itens por página">
      <option value="25" ${state.pageSize === 25 ? "selected" : ""}>25</option>
      <option value="50" ${state.pageSize === 50 ? "selected" : ""}>50</option>
    </select>`;
  document.getElementById("page-prev").onclick = () => {
    state.page = Math.max(1, state.page - 1);
    state.selectedForBatch.clear();
    renderReview();
  };
  document.getElementById("page-next").onclick = () => {
    state.page = Math.min(pages, state.page + 1);
    state.selectedForBatch.clear();
    renderReview();
  };
  document.getElementById("page-size").onchange = (event) => {
    state.pageSize = Number(event.target.value);
    state.page = 1;
    state.selectedForBatch.clear();
    saveReviewPrefs();
    renderReview();
  };
}

function renderSummary() {
  const summary = state.data?.summary;
  if (!summary) return;
  const total = summary.total || 0;
  const ratingItems = (state.data?.items || []).filter((item) => item.evaluation_kind === "rating_only");
  const likedTotal = ratingItems.length;
  const likedDecided = ratingItems.filter((item) => item.liked?.status === "decided").length;
  const likedPending = ratingItems.filter((item) => ["unresolved", "stale"].includes(item.liked?.status)).length;
  const likedReconfirm = (state.data?.items || []).filter((item) => item.liked?.status === "requires_reconfirmation").length;
  const likedStale = ratingItems.filter((item) => item.liked?.status === "stale").length;
  document.getElementById("summary").innerHTML =
    state.reviewTab === "liked"
      ? `
    <div class="summary-head">
      <strong>${Math.max(0, likedDecided)} de ${likedTotal} liked decididos</strong>
      <span class="history">decisões independentes de correspondência</span>
    </div>
    <div class="stat-grid">
      ${stat("Não decididos", likedPending)}
      ${stat("Decididos", Math.max(0, likedDecided))}
      ${stat("Reconfirmar", likedReconfirm)}
      ${stat("Desatualizados", likedStale)}
      ${stat("Aguardando identidade", summary.liked_pending_identity || 0)}
      ${stat("Com texto", summary.written_review || 0)}
      ${stat("Somente nota", likedTotal)}
      ${stat("Já no destino", summary.destination_present || 0)}
    </div>
    <div class="history">A nota não é usada para sugerir liked.</div>
  `
      : `
    <div class="summary-head">
      <strong>${summary.eligible_now || 0} elegíveis agora</strong>
      <span class="history">${summary.batch_eligible || 0} prontos para lote</span>
    </div>
    <div class="stat-grid">
      ${stat("Pendentes", summary.pending)}
      ${stat("Elegíveis", summary.eligible_now || 0)}
      ${stat("Conflitos", summary.conflicts || 0)}
      ${stat("Histórico", summary.history_conflicts || 0)}
      ${stat("Sem correspondência", summary.no_match)}
      ${stat("Edições a confirmar", summary.edition_unconfirmed || 0)}
      ${stat("Edições especiais", summary.special_edition || 0)}
      ${stat("Já no destino", summary.destination_present || 0)}
      ${stat("Com texto", summary.written_review || 0)}
      ${stat("Somente nota", summary.rating_only || 0)}
      ${stat("Sem nota (fora do escopo)", summary.no_rating_excluded || 0)}
      ${stat("Lote disponível", summary.batch_eligible || 0)}
    </div>
    ${summary.stale_matches ? `<div class="danger">${summary.stale_matches} decisão(ões) desatualizada(s). Reabra o item e salve novamente.</div>` : ""}
    <div class="history">Confiança é evidência, não aprovação. Nenhum candidato é aprovado automaticamente.</div>
  `;
}

function renderFilters() {
  const filters = state.reviewTab === "liked" ? LIKED_FILTERS : MATCH_FILTERS;
  const active = state.reviewTab === "liked" ? state.likedFilter : state.matchFilter;
  document.getElementById("filters").innerHTML = filters
    .map(
      ([key, label]) =>
        `<button data-filter="${esc(key)}" class="${active === key ? "active" : ""}" aria-pressed="${active === key}">${esc(label)}</button>`,
    )
    .join("");
  document.querySelectorAll("#filters button").forEach((button) => {
    button.onclick = () => setFilter(button.dataset.filter);
  });
  const extra = document.getElementById("review-extra-filters");
  extra.innerHTML =
    state.reviewTab === "match"
      ? `<select id="confidence-filter" aria-label="Faixa de confiança">
          <option value="all" ${state.confidence === "all" ? "selected" : ""}>Toda confiança</option>
          <option value="high" ${state.confidence === "high" ? "selected" : ""}>Alta (>= 0,9)</option>
          <option value="medium" ${state.confidence === "medium" ? "selected" : ""}>Média (0,8–0,89)</option>
          <option value="low" ${state.confidence === "low" ? "selected" : ""}>Baixa (&lt; 0,8)</option>
        </select>
        <button id="select-visible" class="ghost">Selecionar visíveis para lote</button>`
      : "";
  const confidence = document.getElementById("confidence-filter");
  if (confidence) {
    confidence.onchange = (event) => {
      state.confidence = event.target.value;
      state.page = 1;
      saveReviewPrefs();
      renderReview();
    };
  }
  const selectVisible = document.getElementById("select-visible");
  if (selectVisible) {
    selectVisible.onclick = () => {
      for (const item of pageItems()) {
        if (item.batch_eligible) state.selectedForBatch.add(item.slug);
      }
      renderReview();
    };
  }
}

function renderList() {
  const list = document.getElementById("game-list");
  if (!state.data) {
    list.innerHTML = "";
    return;
  }
  const items = pageItems();
  if (!items.length) {
    list.innerHTML = `<li class="history">Nenhum jogo neste filtro.</li>`;
    return;
  }
  list.innerHTML = items
    .map((item) => {
      const liked = item.liked?.status || "unresolved";
      const selectable = state.reviewTab === "match" && item.batch_eligible;
      const checked = state.selectedForBatch.has(item.slug) ? "checked" : "";
      return `
      <li data-slug="${esc(item.slug)}" class="${item.slug === state.selected ? "selected" : ""}">
        <div class="game-title">
          ${selectable ? `<label class="history"><input class="match-row-select" type="checkbox" data-batch-slug="${esc(item.slug)}" ${checked}> lote</label>` : ""}
          <strong>${esc(item.title)}</strong>
          <span class="history">${esc(item.stable_id)}</span>
        </div>
        <div class="game-meta">
          <span>${esc(item.year ?? "ano?")}</span>
          <span>nota ${esc(item.rating ?? "?")}</span>
          ${badge(item.evaluation_kind === "rating_only" ? "manual" : "approved", item.evaluation_kind === "rating_only" ? "somente nota" : "com texto")}
          ${state.reviewTab === "liked" ? badge(liked === "decided" ? "approved" : liked === "requires_reconfirmation" ? "ambiguous" : "manual", liked === "decided" ? "liked" : liked === "requires_reconfirmation" ? "reconfirmar" : "pendente") : badge(item.match_status)}
          ${item.special_edition ? badge("ambiguous", item.category) : ""}
          ${item.destination_present ? badge("approved", "no destino") : ""}
          ${item.history_conflict ? badge("bad", "histórico") : ""}
        </div>
      </li>`;
    })
    .join("");
  list.querySelectorAll("li[data-slug]").forEach((node) => {
    node.onclick = (event) => {
      if (event.target.closest("[data-batch-slug]")) return;
      state.selected = node.dataset.slug;
      renderList();
      renderDetail();
    };
  });
  list.querySelectorAll("[data-batch-slug]").forEach((checkbox) => {
    checkbox.onclick = (event) => {
      event.stopPropagation();
      if (checkbox.checked) state.selectedForBatch.add(checkbox.dataset.batchSlug);
      else state.selectedForBatch.delete(checkbox.dataset.batchSlug);
      renderBatchBar();
    };
  });
}

function renderLikedDetail(item, previous, next, items, index) {
  const detail = document.getElementById("detail");
  const status = item.liked?.status || "unresolved";
  const identity = item.match_decision === "approved"
    ? `aprovada · game_id ${item.selected_candidate_id}`
    : item.match_status === "candidate"
      ? "candidato, ainda não aprovado"
      : item.match_status === "manual_review"
        ? "revisão manual"
        : "sem identidade";
  const history = item.liked_history || [];
  detail.innerHTML = `
    <div class="detail-head">
      <div>
        <h2>${esc(item.title)}</h2>
        <div class="history">${esc(item.stable_id)} · ${esc(item.slug)} · ${esc(item.year ?? "ano não informado")} · ${esc(item.category || "base")}</div>
      </div>
      <div class="detail-nav">
        <button id="prev" ${previous ? "" : "disabled"}>← Anterior</button>
        <span>${index + 1} de ${items.length}</span>
        <button id="next" ${next ? "" : "disabled"}>Próximo →</button>
      </div>
    </div>
    ${status === "requires_reconfirmation" ? `<div class="warning"><strong>Decisão automática histórica.</strong> Confirme ou substitua o valor; o evento anterior permanece no histórico.</div>` : ""}
    ${item.liked?.binding_status === "pending_identity" ? `<div class="warning"><strong>Decisão sem identidade.</strong> O valor vale para a avaliação de origem e será vinculado quando a correspondência for aprovada.</div>` : ""}
    ${item.destination_present ? `<div class="success"><strong>Já existe no destino.</strong> A decisão de liked fica preservada; a avaliação não será reenviada.</div>` : ""}
    <div class="columns">
      <div>
        <div class="card">
          <h3>Origem — Stash</h3>
          <dl class="kv">
            <dt>Tipo</dt><dd>${item.evaluation_kind === "rating_only" ? badge("manual", "somente nota") : badge("approved", "review escrita")}</dd>
            <dt>Nota</dt><dd>${esc(item.rating ?? "não informada")}</dd>
            <dt>Status</dt><dd>${esc(item.status || "não informado")}</dd>
            <dt>Categoria</dt><dd>${esc(item.category || "base")}</dd>
            <dt>Identidade</dt><dd>${esc(identity)}</dd>
            <dt>Já no destino</dt><dd>${item.destination_present ? `sim (${item.destination_evidence === "execution_verified" ? "verificada por execução" : "reconciliada por conteúdo"})` : "não"}</dd>
          </dl>
          ${item.evaluation_kind === "written_review" ? `<h3>Texto original</h3><div class="review-text">${esc(item.text)}</div>` : ""}
          <div class="liked-row">
            <button data-liked-choice="true" class="${status === "decided" && item.liked.value === true ? "active" : ""}">Recomendo</button>
            <button data-liked-choice="false" class="${status === "decided" && item.liked.value === false ? "active false" : ""}">Não recomendo</button>
            <button data-liked-choice="undecided" class="${status === "unresolved" ? "active undecided" : ""}">Ainda não decidi</button>
          </div>
          <p class="history">Nenhuma escolha é pré-selecionada e a nota não influencia a decisão.</p>
        </div>
        <div class="card">
          <h3>Histórico de liked</h3>
          <div class="history">${history.length ? history.map((entry) => `${esc(entry.at)} · ${esc(entry.event_type)} · ${entry.value === null ? "pendente" : esc(entry.value)} · ${esc(entry.operator_id || "operador não registrado")}`).join("<br>") : "Sem decisões registradas."}</div>
        </div>
      </div>
      <div>
        <div class="card">
          <h3>Identidade no catálogo</h3>
          <p>${esc(identity)}</p>
          <p class="history">Liked e correspondência são decisões independentes.</p>
        </div>
      </div>
    </div>`;
  if (previous) document.getElementById("prev").onclick = () => selectSlug(previous.slug);
  if (next) document.getElementById("next").onclick = () => selectSlug(next.slug);
  document.querySelectorAll("[data-liked-choice]").forEach((button) => {
    button.onclick = () => {
      const raw = button.dataset.likedChoice;
      submitLiked(item.slug, raw === "undecided" ? null : raw === "true");
    };
  });
}

function renderDetail() {
  const item = itemBySlug(state.selected);
  const detail = document.getElementById("detail");
  if (!item) {
    detail.innerHTML = `<div class="empty">Selecione um jogo à esquerda.</div>`;
    return;
  }
  const items = filteredItems();
  const index = items.findIndex((entry) => entry.slug === item.slug);
  const previous = index > 0 ? items[index - 1] : null;
  const next = index >= 0 && index < items.length - 1 ? items[index + 1] : null;
  if (state.reviewTab === "liked") {
    renderLikedDetail(item, previous, next, items, index);
    return;
  }
  const divergence =
    item.text_listing !== null &&
    item.text_detail !== null &&
    item.text_listing !== item.text_detail;
  const stale = item.stale
    ? `<div class="danger">A origem ou a decisão está desatualizada. Revise e salve novamente.</div>`
    : "";
  const existing = item.review_status === "already_exists";

  detail.innerHTML = `
    <div class="detail-head">
      <div>
        <h2>${esc(item.title)}</h2>
        <div class="history">${esc(item.stable_id)} · ${esc(item.slug)} · ${esc(item.year ?? "ano não informado")}</div>
      </div>
      <div class="detail-nav">
        <button id="prev" ${previous ? "" : "disabled"}>← Anterior</button>
        <span>${index + 1} de ${items.length}</span>
        <button id="next" ${next ? "" : "disabled"}>Próximo →</button>
      </div>
    </div>
    ${stale}
    ${existing ? `<div class="success"><strong>Review já existe no Já Zerei.</strong> Este item fica fora da fila de criação; nada será sobrescrito.</div>` : ""}
    ${item.evaluation_kind === "rating_only" ? `<div class="warning"><strong>Somente nota.</strong> Não há texto escrito na origem. O payload futuro usará texto vazio; nada será inventado.</div>` : ""}
    ${item.special_category ? `<div class="warning"><strong>Categoria especial: ${esc(item.category)}.</strong> ${item.suggestion === "no_match" ? "Provavelmente não existe no Já Zerei. Sugestão: marcar sem correspondência/não migrar." : "Confirme manualmente; não há autoaprovação para esta categoria."}</div>` : ""}
    ${item.identity_conflict ? `<div class="danger"><strong>Conflito de identidade de edição.</strong> A associação atual usa o jogo base com baixa confiança. Não será corrigida automaticamente.</div>` : ""}
    ${item.history_conflict ? `<div class="danger"><strong>Histórico inconsistente.</strong> ${esc(item.history_conflict_reason || "")} A elegibilidade fica bloqueada até revisão.
      <button id="reconcile-state" class="ghost">Reconciliar estado materializado a partir do histórico</button></div>` : ""}
    ${(item.eligibility_reasons || []).length ? `<div class="warning"><strong>Bloqueios para migração:</strong><ul class="eligibility-list">${item.eligibility_reasons.map((reason) => `<li>${esc(reason.label)}</li>`).join("")}</ul></div>` : ""}
    ${item.suggestion === "no_match" ? `<div class="danger">Sugestão de remoção da migração: categoria especial sem candidato. A decisão continua humana.</div>` : ""}
    <div class="columns">
      <div>
        <div class="card">
          <h3>Origem — Stash</h3>
          <dl class="kv">
            <dt>Tipo</dt><dd>${item.evaluation_kind === "rating_only" ? badge("manual", "somente nota") : badge("approved", "review escrita")}</dd>
            <dt>Nota original</dt><dd>${esc(item.original_rating ?? "não informada")}</dd>
            <dt>Nota usada</dt><dd>${esc(item.rating ?? "não informada")}</dd>
            <dt>Data</dt><dd>${esc(item.date)}</dd>
            <dt>Status</dt><dd>${esc(item.status || "não informado")}</dd>
            <dt>Progresso</dt><dd>${esc(item.progress ?? "não informado")}</dd>
            <dt>Plataforma Stash</dt><dd>${esc(item.platform ?? "não informada")}</dd>
            <dt>Categoria Stash</dt><dd>${esc(item.category ?? "não informada")} ${item.special_category ? badge("ambiguous", "categoria especial") : ""}</dd>
            <dt>Hash da origem</dt><dd class="history">${esc(item.source_hash)}</dd>
            <dt>Classificação inicial</dt><dd>${badge(item.initial_classification)}</dd>
            <dt>Estado do matching</dt><dd>${badge(item.match_status || "pending")}</dd>
            <dt>Já no destino</dt><dd>${item.destination_present ? `sim (${item.destination_evidence === "execution_verified" ? "verificada por execução" : "reconciliada por conteúdo"})` : "não"}</dd>
            <dt>Elegível agora</dt><dd>${item.eligibility ? "sim" : "não"}</dd>
            <dt>Lote</dt><dd>${item.batch_eligible ? "elegível com seleção explícita" : "individual"}</dd>
            <dt>Edição confirmada</dt><dd>${item.edition_confirmed ? `sim (candidato ${esc(item.edition_confirmed_id)})` : "não"}</dd>
            <dt>Estado da revisão</dt><dd>${badge(item.review_status)} ${item.edited ? badge("manual", "editado") : ""}</dd>
            <dt>Validação da correspondência</dt><dd>${badge(item.match_validation)}</dd>
            <dt>Origem da correspondência</dt><dd>${badge(item.match_origin)}</dd>
          </dl>
          <h3>Texto usado na migração</h3>
          ${item.evaluation_kind === "rating_only"
            ? `<div class="review-text history">Sem texto — avaliação somente com nota.</div>`
            : `<div class="review-text">${esc(item.text)}</div>`}
          <div class="text-source">Origem do texto: ${esc(item.text_source || "não informada")}.</div>
          ${item.evaluation_kind === "written_review" ? `<details><summary>Ver texto original do Stash</summary><div class="review-text">${esc(item.original_text)}</div></details>` : ""}
          ${divergence ? `<div class="warning">Divergência listagem × detalhe. O texto principal usa <code>text_detail</code>.</div>
            <details><summary>Ver texto da listagem</summary><div class="review-text">${esc(item.text_listing)}</div></details>
            <details><summary>Ver texto do detalhe</summary><div class="review-text">${esc(item.text_detail)}</div></details>` : ""}
          ${reviewEditor(item)}
        </div>
        <div class="card">
          <h3>Decisões da correspondência</h3>
          <div class="candidate-actions">
            <button id="no-match" class="bad">Sem correspondência adequada</button>
            <button id="pending" class="ghost">Adiar</button>
            <button id="reopen" class="ghost">Reabrir decisão anterior</button>
          </div>
          <p class="history">Rejeitar um candidato não rejeita os demais. A aprovação não autoriza migração.</p>
          <h3>Histórico recente</h3>
          <div class="history">${item.history.length ? item.history.map(historyLine).join("<br>") : "Sem decisões registradas."}</div>
        </div>
      </div>
      <div>
        <div class="card">
          <h3>Destino — candidatos</h3>
          ${item.candidates.length ? item.candidates.map((candidate) => candidateCard(item, candidate)).join("") : `<p class="history">Nenhum candidato encontrado.</p>`}
        </div>
        <div class="card">
          <h3>Pesquisar outro jogo no Já Zerei</h3>
          <div class="search-row">
            <input id="catalog-query" type="search" placeholder="Título do jogo" value="${esc(item.title)}">
            <button id="catalog-search">Pesquisar</button>
          </div>
          <div id="catalog-results" class="results"></div>
        </div>
        <div class="card">
          <h3>Recomendação do autor — liked</h3>
          <p class="history">Independente da correspondência. Não inferir por nota, texto ou status.</p>
          <dl class="kv">
            <dt>Estado</dt><dd>${badge(item.liked.status)}</dd>
            <dt>Valor</dt><dd>${item.liked.value === null ? "não definido" : esc(item.liked.value)}</dd>
            <dt>Origem</dt><dd>${esc(item.liked.source || "—")}</dd>
            <dt>Validação</dt><dd>${badge(item.liked.validation?.status || "pending")}</dd>
            <dt>Ratificação</dt><dd class="history">${esc(item.liked.ratification_id || "—")}</dd>
            <dt>game_id vinculado</dt><dd>${esc(item.liked.game_id ?? "—")}</dd>
          </dl>
          <div class="liked-row">
            <button data-liked="true" class="${item.liked.value === true ? "active" : ""}" ${item.liked.game_id && !existing ? "" : "disabled"}>true — recomendo</button>
            <button data-liked="false" class="${item.liked.value === false ? "active false" : ""}" ${item.liked.game_id && !existing ? "" : "disabled"}>false — não recomendo</button>
          </div>
          ${existing ? `<p class="history">Review já existente: liked não será enviado e não altera o plano.</p>` : item.liked.game_id ? `<p class="history">A decisão será vinculada ao game_id ${esc(item.liked.game_id)}. Se a correspondência mudar, o liked ficará stale.</p>` : `<div class="warning">Defina uma correspondência antes de decidir liked.</div>`}
        </div>
      </div>
    </div>
  `;

  if (previous) document.getElementById("prev").onclick = () => selectSlug(previous.slug);
  if (next) document.getElementById("next").onclick = () => selectSlug(next.slug);
  document.getElementById("no-match").onclick = () => submitMatch(item.slug, "no_match");
  document.getElementById("pending").onclick = () => submitMatch(item.slug, "pending");
  document.getElementById("reopen").onclick = () => {
    if (!window.confirm("Reabrir a decisão de correspondência deste item?")) return;
    submitMatch(item.slug, "reopen");
  };
  const reconcile = document.getElementById("reconcile-state");
  if (reconcile) {
    reconcile.onclick = async () => {
      if (!window.confirm("Reconstruir o estado materializado a partir do histórico? Um backup será preservado.")) return;
      try {
        const result = await api("/api/review/reconcile", {
          method: "POST",
          body: JSON.stringify({ confirm: "reconcile" }),
        });
        toast(`${result.changed} item(ns) reconciliado(s).`);
        await loadState(item.slug);
        await loadWizard();
      } catch (error) {
        toast(error.message, true);
      }
    };
  }
  document.querySelectorAll("[data-candidate-action]").forEach((button) => {
    button.onclick = () => {
      const candidateId = Number(button.dataset.candidateId);
      if (button.dataset.candidateAction === "approve") {
        const candidate = (item.candidates || []).find((entry) => entry.game_id === candidateId);
        if (!window.confirm(`Aprovar "${item.title}" → "${candidate?.name || candidateId}"?`)) return;
      }
      submitMatch(item.slug, button.dataset.candidateAction, {
        candidate_id: candidateId,
      });
    };
  });
  document.querySelectorAll("[data-edition-confirm]").forEach((button) => {
    button.onclick = () => {
      const candidateId = Number(button.dataset.editionConfirm);
      const input = document.querySelector(`[data-edition-reason="${CSS.escape(String(candidateId))}"]`);
      const reason = input?.value.trim() || "";
      if (!reason) {
        toast("Informe a justificativa da confirmação de edição.", true);
        return;
      }
      submitMatch(item.slug, "confirm_edition", {
        candidate_id: candidateId,
        reason,
        edition_confirmed: true,
      });
    };
  });
  document.querySelectorAll("[data-liked]").forEach((button) => {
    button.onclick = () => submitLiked(item.slug, button.dataset.liked === "true");
  });
  document.getElementById("catalog-search").onclick = () => searchCatalog(item);
  const catalogInput = document.getElementById("catalog-query");
  catalogInput.onkeydown = (event) => {
    if (event.key === "Enter") searchCatalog(item);
  };
  document.getElementById("save-review").onclick = () => {
    const fields = {
      rating: Number(document.getElementById("edit-rating").value),
      status: document.getElementById("edit-status").value || null,
      progress: document.getElementById("edit-progress").value || null,
    };
    if (item.evaluation_kind !== "rating_only") {
      fields.text = document.getElementById("edit-text").value;
    }
    submitReview(item, fields);
  };
  document.getElementById("reset-review").onclick = () => {
    const fields = {
      rating: item.original_rating,
      status: item.original_status,
      progress: item.original_progress,
    };
    if (item.evaluation_kind !== "rating_only") {
      fields.text = item.original_text;
    }
    submitReview(item, fields);
  };
  loadCandidateDetails(item);
}

function reviewEditor(item) {
  return `
    <details class="editor">
      <summary>Editar informações usadas na migração</summary>
      <div class="edit-grid">
        <label>Nota (0–10)
          <input id="edit-rating" type="number" min="0" max="10" step="0.5" value="${esc(item.rating ?? "")}">
        </label>
        <label>Status
          <input id="edit-status" type="text" value="${esc(item.status ?? "")}">
        </label>
        <label>Progresso
          <input id="edit-progress" type="text" value="${esc(item.progress ?? "")}">
        </label>
        ${item.evaluation_kind === "rating_only"
          ? `<div class="wide warning">Somente nota: o texto é vazio na origem e não será inventado.</div>`
          : `<label class="wide">Texto da review
          <textarea id="edit-text" rows="9">${esc(item.text)}</textarea>
        </label>`}
        <div class="edit-actions">
          <button id="save-review" class="primary">Salvar alterações</button>
          <button id="reset-review" class="ghost">Restaurar original</button>
          <span class="history">O original permanece preservado.</span>
        </div>
      </div>
    </details>`;
}

async function submitReview(item, fields) {
  try {
    await api("/api/review", {
      method: "POST",
      body: JSON.stringify({ slug: item.slug, fields }),
    });
    toast("Informações da review salvas.");
    await loadState(item.slug);
    await loadWizard();
  } catch (error) {
    toast(error.message, true);
  }
}

function historyLine(event) {
  const action = esc(event.action);
  const at = esc(event.at);
  const candidate = event.candidate_id ? ` · candidato ${esc(event.candidate_id)}` : "";
  const edition = event.edition_confirmed ? " · edição confirmada" : "";
  const operator = event.operator_id ? ` · ${esc(event.operator_id)}` : " · operador não registrado";
  return `${at} · ${action}${candidate}${edition}${operator}`;
}

function candidateWarnings(item, candidate) {
  const warnings = [];
  const sourceTitle = (item.title || "").trim().toLowerCase();
  const candidateTitle = (candidate.name || "").trim().toLowerCase();
  if (sourceTitle && candidateTitle && sourceTitle !== candidateTitle) warnings.push("título diferente");
  if (item.year && candidate.year && item.year !== candidate.year) warnings.push("ano divergente");
  if (!warnings.length) return "";
  return `<div class="warning">Atenção: ${esc(warnings.join(" · "))}. Confirme manualmente edição/versão.</div>`;
}

function candidateCard(item, candidate) {
  const rejected = item.rejected_candidate_ids.includes(candidate.game_id);
  const selected = item.selected_candidate_id === candidate.game_id;
  const editionConfirmed = item.edition_confirmed && item.edition_confirmed_id === candidate.game_id;
  const needsEdition = item.special_edition && selected && !editionConfirmed;
  const editionBlocked = item.special_edition && !editionConfirmed;
  return `
    <div class="candidate ${selected ? "selected" : ""} ${rejected ? "rejected" : ""}" data-candidate="${esc(candidate.game_id)}">
      <div class="candidate-head">
        <div>
          <h4>${esc(candidate.name)} ${selected ? "· selecionado" : ""}</h4>
          <div class="evidence">game_id ${esc(candidate.game_id)} · ${esc(candidate.slug)} · ${esc(candidate.year ?? "ano?")}</div>
        </div>
        <div>${selected ? badge("approved", "aprovado") : ""} ${rejected ? badge("rejected", "rejeitado") : ""} ${editionConfirmed ? badge("ok", "edição confirmada") : ""}</div>
      </div>
      <div class="evidence candidate-extra" data-detail="${esc(candidate.game_id)}">Plataforma/desenvolvedora: carregando…</div>
      <div class="evidence">Evidência: ${esc((candidate.reasons || []).join("; ") || "sem evidência individual registrada")} · confiança ${esc(candidate.confidence ?? "—")}</div>
      ${candidateWarnings(item, candidate)}
      ${needsEdition ? `<div class="warning"><strong>Confirmação de edição obrigatória.</strong>
        <p>Confirme que este candidato representa a edição correta. A confirmação não aprova a correspondência sozinha.</p>
        <input data-edition-reason="${esc(candidate.game_id)}" type="text" placeholder="Justificativa da confirmação de edição">
        <button data-edition-confirm="${esc(candidate.game_id)}" class="warning-button">Confirmar edição</button>
      </div>` : ""}
      <div class="candidate-actions">
        <button data-candidate-action="approve" data-candidate-id="${esc(candidate.game_id)}" class="ok" ${rejected || editionBlocked ? "disabled" : ""}>Aprovar este candidato</button>
        <button data-candidate-action="reject" data-candidate-id="${esc(candidate.game_id)}" class="bad" ${rejected ? "disabled" : ""}>Rejeitar este candidato</button>
      </div>
    </div>`;
}

async function loadCandidateDetails(item) {
  for (const candidate of item.candidates) {
    if (state.details.has(candidate.game_id)) {
      applyCandidateDetails(candidate.game_id, state.details.get(candidate.game_id));
      continue;
    }
    try {
      const body = await api(`/api/catalog?q=${encodeURIComponent(candidate.name || "")}`);
      const found = body.results.find((entry) => entry.game_id === candidate.game_id);
      if (found) {
        state.details.set(candidate.game_id, found);
        applyCandidateDetails(candidate.game_id, found);
      }
    } catch (error) {
      applyCandidateDetails(candidate.game_id, null);
    }
  }
}

function applyCandidateDetails(gameId, details) {
  const node = document.querySelector(`[data-detail="${CSS.escape(String(gameId))}"]`);
  if (!node) return;
  if (!details) {
    node.textContent = "Plataforma/desenvolvedora: não disponível no catálogo consultado.";
    return;
  }
  const platforms = (details.platforms || []).join(", ") || "não informada";
  node.textContent = `Plataforma: ${platforms} · Desenvolvedora: ${details.developer || "não informada"}`;
}

async function searchCatalog(item) {
  const query = document.getElementById("catalog-query").value.trim();
  const results = document.getElementById("catalog-results");
  results.innerHTML = `<div class="history">Pesquisando somente leitura…</div>`;
  try {
    const body = await api(`/api/catalog?q=${encodeURIComponent(query)}`);
    if (!body.results.length) {
      results.innerHTML = `<div class="warning">Nenhum resultado.</div>`;
      return;
    }
    results.innerHTML = body.results
      .map(
        (entry) => `
        <div class="result">
          <div>
            <strong>${esc(entry.name)}</strong>
            <div class="evidence">game_id ${esc(entry.game_id)} · ${esc(entry.slug)} · ${esc(entry.year ?? "ano?")} · ${esc((entry.platforms || []).join(", ") || "plataforma não informada")} · ${esc(entry.developer || "desenvolvedora não informada")}</div>
          </div>
          <button data-manual-id="${esc(entry.game_id)}" class="primary">Selecionar</button>
        </div>`,
      )
      .join("");
    results.querySelectorAll("[data-manual-id]").forEach((button) => {
      button.onclick = () => {
        if (!confirm(`Confirmar seleção manual do game_id ${button.dataset.manualId}?`)) return;
        submitMatch(item.slug, "manual", {
          candidate_id: Number(button.dataset.manualId),
          query,
        });
      };
    });
  } catch (error) {
    results.innerHTML = `<div class="danger">${esc(error.message)}</div>`;
  }
}

async function submitMatch(slug, action, extra = {}) {
  try {
    await api("/api/match", {
      method: "POST",
      body: JSON.stringify({ slug, action, ...extra }),
    });
    toast("Decisão salva.");
    await loadState(slug);
    await loadWizard();
  } catch (error) {
    toast(error.message, true);
  }
}

function closeModal() {
  document.querySelectorAll(".modal-backdrop").forEach((node) => node.remove());
}

function openBatchConfirm() {
  const selected = [...state.selectedForBatch];
  const items = state.data.items.filter((item) => selected.includes(item.slug));
  const invalid = items.filter((item) => !item.batch_eligible);
  if (invalid.length) {
    toast("A seleção contém itens que não são elegíveis para lote.", true);
    return;
  }
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";
  backdrop.innerHTML = `
    <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="batch-title">
      <h2 id="batch-title">Confirmar aprovação em lote</h2>
      <p>${items.length} correspondência(s) serão aprovadas individualmente. Revise a lista:</p>
      <ul class="batch-list">
        ${items
          .map((item) => {
            const candidate = (item.candidates || []).find(
              (entry) => entry.game_id === item.selected_candidate_id,
            );
            return `<li><strong>${esc(item.title)}</strong> → ${esc(candidate?.name || "—")} (ID ${esc(candidate?.game_id ?? "—")}, confiança ${esc(candidate?.confidence ?? "—")})</li>`;
          })
          .join("")}
      </ul>
      <p class="history">Cada item gera um evento próprio. Se parte falhar, as demais decisões são preservadas.</p>
      <div class="candidate-actions">
        <button id="batch-run" class="ok">Confirmar ${items.length} aprovação(ões)</button>
        <button id="batch-cancel" class="ghost">Cancelar</button>
      </div>
    </div>`;
  document.body.appendChild(backdrop);
  document.getElementById("batch-cancel").onclick = closeModal;
  document.getElementById("batch-run").onclick = async () => {
    const button = document.getElementById("batch-run");
    button.disabled = true;
    try {
      const result = await api("/api/match/batch", {
        method: "POST",
        body: JSON.stringify({
          items: items.map((item) => ({
            slug: item.slug,
            candidate_id: item.selected_candidate_id,
            reason: "aprovacao em lote explicita",
          })),
        }),
      });
      closeModal();
      state.selectedForBatch.clear();
      toast(
        `${result.approved} aprovada(s), ${result.failed} falha(s).`,
        result.failed > 0,
      );
      await loadState();
      await loadWizard();
    } catch (error) {
      button.disabled = false;
      toast(error.message, true);
    }
  };
}

async function submitLiked(slug, value) {
  try {
    await api("/api/liked", {
      method: "POST",
      body: JSON.stringify({ slug, value }),
    });
    toast(value === null ? "Decisão adiada; item voltou para pendente." : `liked = ${value} salvo.`);
    await loadState(slug);
    await loadWizard();
  } catch (error) {
    toast(error.message, true);
  }
}

function selectSlug(slug) {
  state.selected = slug;
  const items = filteredItems();
  const index = items.findIndex((item) => item.slug === slug);
  if (index >= 0) {
    const nextPage = Math.floor(index / state.pageSize) + 1;
    if (nextPage !== state.page) state.selectedForBatch.clear();
    state.page = nextPage;
  }
  renderList();
  renderPagination();
  renderDetail();
  document.getElementById("detail").scrollTop = 0;
}

function navigateReview(delta) {
  const items = filteredItems();
  if (!items.length) return;
  const current = items.findIndex((item) => item.slug === state.selected);
  const next = Math.max(0, Math.min(items.length - 1, (current < 0 ? 0 : current) + delta));
  selectSlug(items[next].slug);
}

document.addEventListener("keydown", (event) => {
  const tag = (event.target?.tagName || "").toLowerCase();
  if (["input", "textarea", "select"].includes(tag) || event.target?.isContentEditable) return;
  if (event.key === "/") {
    event.preventDefault();
    document.getElementById("search")?.focus();
    return;
  }
  if (["j", "ArrowDown"].includes(event.key)) {
    event.preventDefault();
    navigateReview(1);
    return;
  }
  if (["k", "ArrowUp"].includes(event.key)) {
    event.preventDefault();
    navigateReview(-1);
    return;
  }
  const item = itemBySlug(state.selected);
  if (!item || !wizard.data) return;
  if (state.reviewTab === "liked") {
    if (event.key === "1") submitLiked(item.slug, true);
    if (event.key === "2") submitLiked(item.slug, false);
    if (event.key === "0") submitLiked(item.slug, null);
    return;
  }
  if (event.key === "p") {
    submitMatch(item.slug, "pending");
    return;
  }
  if (event.key === "n") {
    if (window.confirm(`Marcar "${item.title}" como sem correspondência?`)) {
      submitMatch(item.slug, "no_match");
    }
    return;
  }
  if (event.key === "r") {
    if (item.selected_candidate_id && window.confirm(`Rejeitar o candidato de "${item.title}"?`)) {
      submitMatch(item.slug, "reject", { candidate_id: item.selected_candidate_id });
    }
    return;
  }
  if (event.key === "a") {
    if (!item.selected_candidate_id) return;
    const candidate = (item.candidates || []).find(
      (entry) => entry.game_id === item.selected_candidate_id,
    );
    if (item.special_edition) {
      toast("Edição especial: confirme a edição antes de aprovar.", true);
      return;
    }
    if (window.confirm(`Aprovar "${item.title}" → "${candidate?.name || item.selected_candidate_id}"?`)) {
      submitMatch(item.slug, "approve", { candidate_id: item.selected_candidate_id });
    }
  }
});

/* ---------- estado global ---------- */

async function loadWizard() {
  try {
    const data = await api("/api/wizard");
    wizard.data = data;
    wizard.error = null;
    wizard.job = data.job;
    wizard.detail = null;
    wizard.detailError = null;
    if (data.active_execution_id) {
      try {
        const detail = await api(`/api/executions/${encodeURIComponent(data.active_execution_id)}`);
        if (wizard.data?.active_execution_id === detail.execution_id) {
          wizard.detail = detail;
        }
      } catch (error) {
        wizard.detailError = error.body || { error: error.message };
      }
    }
    const verifications = data.active_verifications || [];
    if (verifications.length) {
      const wanted =
        wizard.selectedVerificationId &&
        verifications.some((item) => item.verification_id === wizard.selectedVerificationId)
          ? wizard.selectedVerificationId
          : data.last_verification_id || verifications[0].verification_id;
      await loadVerificationReport(wanted);
    } else {
      wizard.report = null;
      wizard.reportError = null;
      wizard.selectedVerificationId = null;
    }
    renderAll();
    if (isActiveJob(wizard.job)) startJobPolling();
    if (wizard.data.plan?.run?.running) startRunPolling();
  } catch (error) {
    wizard.error = error.body || { error: error.message };
    renderAll();
  }
}

async function loadVerificationReport(verificationId) {
  try {
    const body = await api(`/api/report?verification_id=${encodeURIComponent(verificationId)}`);
    wizard.report = body.report;
    wizard.reportValid = body.valid;
    wizard.selectedVerificationId = verificationId;
  } catch (error) {
    wizard.report = null;
    wizard.reportError = error.body || { error: error.message };
  }
}

async function refreshAll(selectSlug) {
  await loadState(selectSlug);
  await loadWizard();
}

function renderAll() {
  renderTop();
  renderPrepare();
  renderPlanPanel();
  renderResult();
  renderAdvanced();
  renderJob();
}

function renderTop() {
  const data = wizard.data;
  const gates = currentGates();
  const node = document.getElementById("write-state");
  if (!data) {
    node.textContent = "Carregando…";
    node.className = "gate off";
    return;
  }
  const on = Boolean(gates.allow_writes && gates.enable_real_write_transport);
  node.textContent = on ? "Escrita autorizada" : "Escrita bloqueada";
  node.className = `gate ${on ? "on" : "off"}`;
  const identity = data.identity || {};
  const jz = identity.jazerei || {};
  const stash = identity.stash || {};
  const jzLabel = jz.masked_user_id ? `Já Zerei ${jz.masked_user_id}` : "Já Zerei não verificado";
  const stashLabel = stash.user ? `Stash @${stash.user}` : "Stash não configurado";
  const plan = data.active_plan_id || "sem plano ativo";
  const execution = data.active_execution_id || "sem execução ativa";
  document.getElementById("status-line").textContent =
    `${jzLabel} · ${stashLabel} · plano ${plan} · execução ${execution}`;
}

/* ---------- 1. preparar ---------- */

function renderPrepare() {
  const node = document.getElementById("stage-prepare");
  const data = wizard.data;
  if (!data) {
    node.innerHTML = `<div class="danger">${esc(wizard.error?.error || "Não foi possível carregar o estado.")}</div>`;
    return;
  }
  const prepare = data.prepare || {};
  if (prepare.error) {
    node.innerHTML = `<h2>1. Preparar</h2><div class="danger">${esc(prepare.error)}</div>`;
    return;
  }
  const steps = prepare.steps || [];
  const preview = prepare.policy_preview;
  const safety = prepare.safety || {};
  const activeJob = isActiveJob(wizard.job) && wizard.job.kind === "prepare_all";
  let cta = "";

  if (safety.blocked) {
    cta = `<div class="danger"><strong>Há uma escrita ambígua de execução anterior.</strong>
      <p>${esc(safety.detail || "")}</p>
      <p class="history">Ela precisa ser reconciliada com leitura autenticada antes de preparar nova execução. Nenhum POST será repetido.</p>
      <button id="go-advanced" class="primary">Abrir seção avançada</button></div>`;
  } else if (prepare.blocked) {
    const blockedStep = steps.find((step) => step.status === "blocked");
    cta = `<div class="danger"><strong>Preparação bloqueada.</strong>
      <p>${esc(blockedStep?.detail || "Resolva a pendência indicada.")}</p>
      <button id="go-review-blocked" class="primary">Ir para revisão</button></div>`;
  } else if (prepare.needs_policy_confirmation) {
    cta = `<div class="warning">
      <strong>Política automática aguardando confirmação explícita.</strong>
      <p>${esc(preview ? `${preview.auto_approved} correspondência(s) seriam aprovadas, ${preview.auto_liked} liked seriam definidos.` : "")}</p>
      <p class="history">Decisões humanas não são sobrescritas. Nada foi aplicado ainda.</p>
      <div class="cta-row">
        <button id="apply-policy-continue" class="primary" ${activeJob ? "disabled" : ""}>Aplicar política e continuar</button>
        <button id="review-before-policy" class="ghost">Revisar antes</button>
      </div>
    </div>`;
  } else if (prepare.ready) {
    cta = `<div class="success"><strong>Preparação concluída.</strong>
      <p>O plano está validado. Nenhuma escrita no Já Zerei foi feita.</p>
      <button id="go-review" class="primary">Ir para revisão e execução</button></div>`;
  } else {
    cta = `<button id="prepare-all" class="primary big" ${activeJob || data.config?.operation_busy ? "disabled" : ""}>Preparar tudo</button>
      <p class="history">Executa apenas o que falta: coleta, snapshot, correspondências e plano. A política automática só roda após sua confirmação. Nenhuma escrita no destino.</p>`;
  }

  node.innerHTML = `
    <div class="stage-head">
      <div>
        <h2>1. Preparar</h2>
        <p class="history">Reúne os insumos necessários. Operações válidas são reutilizadas; nada é recoletado só porque a tela abriu.</p>
      </div>
      ${fact("Coleta", esc(prepare.source || "—"))}
      ${fact("Destino", esc(prepare.destination || "—"))}
    </div>
    ${prepare.interrupted ? `<div class="warning">A última preparação foi interrompida por reinício. O que já estava válido continua disponível; clique em preparar para continuar do ponto seguro.</div>` : ""}
    <ol class="activity-list">
      ${steps
        .map(
          (step) => `<li class="activity ${esc(step.status)}">
            <span class="activity-dot"></span>
            <div><strong>${esc(step.label)}</strong><div class="history">${esc(step.detail)}</div></div>
            <span class="activity-state">${esc(statusLabel(step.status))}</span>
          </li>`,
        )
        .join("")}
    </ol>
    ${cta}
    ${renderJobInline("prepare_all")}
    ${wizard.error ? `<div class="danger">${esc(wizard.error.error || "Falha")}${wizard.error.hint ? ` — ${esc(wizard.error.hint)}` : ""}</div>` : ""}
  `;
  const bind = (id, fn) => {
    const el = document.getElementById(id);
    if (el) el.onclick = fn;
  };
  bind("prepare-all", () => startPrepare(false));
  bind("apply-policy-continue", () => startPrepare(true));
  bind("review-before-policy", () => showStage("review"));
  bind("go-review", () => showStage("review"));
  bind("go-advanced", () => showStage("advanced"));
  bind("go-review-blocked", () => showStage("review"));
}

function startPrepare(applyPolicy) {
  return startJob("prepare_all", { apply_policy: applyPolicy });
}

/* ---------- 2. revisar e executar ---------- */

function renderPlanPanel() {
  const node = document.getElementById("plan-panel");
  const data = wizard.data;
  if (!data) {
    node.innerHTML = "";
    return;
  }
  const plan = data.plan || {};
  const prepare = data.prepare || {};
  const safety = data.safety || {};
  const execution = data.active_execution;
  const gates = currentGates();
  const on = gates.allow_writes && gates.enable_real_write_transport;
  const approved = Boolean(plan.approved);
  const planStale = Boolean((plan.stale || []).length || (plan.invalid || []).length);
  const actions = Number(plan.actions || 0);
  const pending = data.review?.summary?.needs_review ?? 0;
  const autoMatches = data.review?.summary?.match_validation?.automatic_unreviewed || 0;
  const autoLiked = data.review?.summary?.liked_validation?.automatic_unreviewed || 0;
  const automaticCount = autoMatches + autoLiked;
  const blockedLiked = data.review?.summary?.blocked_liked || 0;
  const alreadyExists = data.review?.summary?.already_exists || 0;
  const approvalReasons = plan.approval?.reasons || [];
  const blockedLikedItems = (state.data?.items || []).filter(
    (item) =>
      ["approved", "manual"].includes(item.review_status) &&
      item.liked?.status !== "decided",
  );
  const activeFinalize = isActiveJob(wizard.job) && wizard.job.kind === "approve_and_prepare";
  let cta = "";

  if (!plan.exists) {
    cta = `<div class="warning">Nenhum plano ativo. Conclua a etapa <strong>Preparar</strong>.</div>
      <button id="back-prepare" class="primary">Voltar para preparação</button>`;
  } else if (safety.blocked) {
    cta = `<div class="danger"><strong>${esc(safety.detail || "Ambiguidade pendente.")}</strong>
      <p class="history">A preparação de execução fica bloqueada até a reconciliação.</p>
      <button id="go-advanced-safety" class="primary">Abrir seção avançada</button></div>`;
  } else if (planStale) {
    cta = `<div class="warning"><strong>Plano desatualizado.</strong>
      <p>Uma decisão de correspondência ou liked mudou depois da geração deste plano. Ele não pode ser aprovado com hashes antigos.</p>
      <p class="history">Regenerar usa os dados atuais; não aplica política, não decide liked e não escreve no destino.</p>
      <button id="refresh-plan" class="primary big" ${data.config?.operation_busy ? "disabled" : ""}>Regenerar plano com decisões atuais</button></div>`;
  } else if (execution && ["prepared", "awaiting_confirmation"].includes(execution.status)) {
    cta = on
      ? `<div class="danger"><strong>Execução preparada com ${actions} ação(ões).</strong>
          <p>Esta é a única ação que altera o Já Zerei. A confirmação exigirá digitar o plan_id.</p>
          <button id="execute-migration" class="bad big">Executar ${actions} ações no Já Zerei</button></div>`
      : `<div class="warning"><strong>Execução preparada, mas a escrita está bloqueada.</strong>
          <p>Os dois gates externos precisam estar ligados: <code>MIGRATION_ALLOW_WRITES=true</code> e <code>MIGRATION_ENABLE_REAL_WRITE_TRANSPORT=true</code>. A UI não liga gates.</p>
          <button class="primary" disabled>Executar no Já Zerei</button></div>`;
  } else if (execution && ["inconclusive", "cancelled", "failed"].includes(execution.status)) {
    cta = `<div class="warning"><strong>Execução ${esc(statusLabel(execution.status))}.</strong>
      <p>Retomar não reenvia ações verificadas ou ambíguas. Revise o histórico antes.</p>
      <div class="cta-row">
        <button id="resume-execution" class="primary" ${on ? "" : "disabled"}>Retomar execução</button>
        <button id="go-result" class="ghost">Conferir resultado</button>
      </div>
      ${on ? "" : `<p class="history">Retomada exige os dois gates de escrita.</p>`}</div>`;
  } else if (!approved && automaticCount > 0) {
    cta = `<div class="warning"><strong>Decisões automáticas aguardam ratificação humana.</strong>
      <p>${autoMatches} correspondência(s) e ${autoLiked} liked automáticos ainda não estão cobertos por ratificação. O plano atual não os inclui.</p>
      <p class="history">Ratificar registra um evento humano append-only vinculado aos hashes atuais e regenera o plano. Não altera correspondências, liked nem o destino.</p>
      <div class="cta-row">
        <button id="ratify-automatic" class="primary big">Revisar e ratificar ${automaticCount} decisão(ões)</button>
        <button id="filter-automatic" class="ghost">Ver itens automáticos</button>
      </div></div>`;
  } else if (!approved && blockedLiked > 0) {
    const names = blockedLikedItems
      .slice(0, 8)
      .map((item) => esc(item.title || item.slug))
      .join(", ");
    cta = `<div class="danger"><strong>${blockedLiked} ação(ões) com liked pendente.</strong>
      <p>${names}${blockedLikedItems.length > 8 ? "…" : ""}</p>
      <p class="history">A aprovação exige decisão explícita de liked em todas as ações do plano. Nota 6.0 permanece unresolved por política; o backend bloqueia a aprovação até você decidir.</p>
      <div class="cta-row">
        <button id="filter-liked-pending" class="primary big">Decidir liked pendente</button>
      </div></div>`;
  } else if (!approved) {
    cta = `<div class="warning"><strong>Revisão humana antes da preparação da execução.</strong>
      <p>${pending} item(ns) ainda precisam de conferência. Itens fora do plano não bloqueiam, mas nenhuma decisão é tomada por você.</p>
      <p class="history">O próximo passo aprova o plano vinculado aos hashes atuais, executa o preflight autenticado e cria a execução. Nenhuma escrita no destino.</p>
      <button id="approve-and-prepare" class="primary big" ${activeFinalize || data.config?.operation_busy ? "disabled" : ""}>Aprovar plano e preparar execução</button></div>`;
  } else if (!execution) {
    cta = `<div class="warning"><strong>Plano aprovado, execução ainda não preparada.</strong>
      <p>Aprovação já registrada. O preflight relê o destino autenticado antes de criar a execução.</p>
      <button id="approve-and-prepare" class="primary" ${activeFinalize || data.config?.operation_busy ? "disabled" : ""}>Executar preflight e preparar execução</button></div>`;
  } else {
    cta = `<div class="success"><strong>Plano aprovado e execução disponível.</strong>
      <button id="go-result" class="primary">Conferir resultado</button></div>`;
  }

  node.innerHTML = `
    <div class="plan-head">
      <div>
        <h2>2. Revisar e executar</h2>
        <p class="history">Decida as correspondências e o liked. Depois aprove o plano exato; só então a execução pode ser preparada.</p>
      </div>
      <div class="plan-facts">
        ${fact("Plano", plan.plan_id ? `<code>${esc(plan.plan_id)}</code>` : "—")}
        ${fact("Ações", esc(actions))}
        ${fact("Sem correspondência", esc(plan.unmatched ?? "—"))}
        ${fact("Aprovação", esc(plan.approval?.status || "pendente"))}
        ${fact("Plano", planStale ? "desatualizado" : "válido")}
        ${fact("Preflight", esc(plan.compatible_preflight?.preflight_id || "pendente"))}
        ${fact("A ratificar", esc(automaticCount))}
        ${fact("Liked bloqueando", esc(blockedLiked))}
        ${fact("Já existentes", esc(alreadyExists))}
        ${fact("Gates", on ? "ligados" : "desligados")}
      </div>
    </div>
    ${alreadyExists ? `<p class="history">${alreadyExists} review(s) já existem no destino e ficam fora da fila de criação; não serão sobrescritas.</p>` : ""}
    ${cta}
    ${!approved && approvalReasons.length ? `<details class="execution-card"><summary>Por que a aprovação está bloqueada (${approvalReasons.length})</summary><ul class="compact-list">${approvalReasons.map((reason) => `<li>${esc(reason)}</li>`).join("")}</ul></details>` : ""}
    ${renderJobInline("approve_and_prepare")}
    ${renderExecutionSummary(execution)}
  `;
  const bind = (id, fn) => {
    const el = document.getElementById(id);
    if (el) el.onclick = fn;
  };
  bind("back-prepare", () => showStage("prepare"));
  bind("go-advanced-safety", () => showStage("advanced"));
  bind("approve-and-prepare", approveAndPrepare);
  bind("refresh-plan", generatePlan);
  bind("ratify-automatic", ratifyAutomatic);
  bind("filter-automatic", () => {
    state.matchFilter = "attention";
    renderReview();
  });
  bind("filter-liked-pending", () => {
    state.reviewTab = "liked";
    state.likedFilter = "pending";
    state.page = 1;
    saveReviewPrefs();
    renderReview();
    document.getElementById("review-workspace").scrollIntoView({ behavior: "smooth" });
  });
  bind("execute-migration", executeMigration);
  bind("resume-execution", resumeMigration);
  bind("go-result", () => showStage("result"));
  bindLiveCancel();
}

function renderExecutionSummary(execution) {
  if (!execution) return "";
  const counts = execution.summary || {};
  return `<div class="execution-card">
    <div class="execution-head">
      <div>
        <h3>Execução ${esc(execution.execution_id)}</h3>
        <div class="history">status ${esc(statusLabel(execution.status))} · criada ${esc(execution.created_at || "—")}</div>
      </div>
      ${badge(execution.status, statusLabel(execution.status))}
    </div>
    <div class="summary-strip">
      ${stat("Verificadas", counts.verified || 0)}
      ${stat("Prontas", counts.ready || 0)}
      ${stat("Falhas", counts.failed || 0)}
      ${stat("Ambíguas", counts.ambiguous || 0)}
      ${stat("Conflitos", counts.conflicts || 0)}
    </div>
    ${renderLiveRun(wizard.data.plan?.run)}
    ${(execution.blocking || []).length ? `<div class="warning"><strong>Bloqueios:</strong> ${execution.blocking.map((item) => `${esc(item.source_ref)} · ${esc(item.status)}`).join("; ")}</div>` : ""}
  </div>`;
}

function renderLiveRun(run) {
  if (!run || (!run.running && !["starting", "cancelling"].includes(run.status))) return "";
  const total = Number(run.total || run.summary?.total_actions || 0);
  const completed = Number(run.completed ?? 0);
  const percent = total ? Math.min(100, Math.round((completed / total) * 100)) : 0;
  const current = run.current_action;
  return `<div class="run-progress">
    <div class="run-head">
      <span class="spinner"></span>
      <strong>${run.status === "cancelling" ? "Cancelamento solicitado" : "Execução em andamento"}</strong>
      <span class="run-count">${completed}/${total || "?"}</span>
    </div>
    <div class="run-bar"><span style="width:${percent}%"></span></div>
    <div class="run-current">${
      current
        ? `Ação ${esc(current.index || run.current_index || "?")} de ${esc(total || "?")}: <code>${esc(current.source_ref)}</code> · ${esc(current.status)}`
        : "Preparando navegador e autenticação..."
    }</div>
    <div class="cta-row"><button id="cancel-execution" class="bad">Cancelar execução</button></div>
  </div>`;
}

function bindLiveCancel() {
  const button = document.getElementById("cancel-execution");
  if (button) button.onclick = cancelMigration;
}

async function generatePlan() {
  const planId = wizard.data?.active_plan_id;
  if (!planId) return;
  if (
    !confirm(
      "Regenerar o plano com as decisões atuais? Nenhuma escrita será feita no Já Zerei.",
    )
  ) {
    return;
  }
  try {
    const body = await api("/api/plan", { method: "POST", body: "{}" });
    toast(`Plano regenerado: ${body.plan_id} · ${body.actions} ações.`);
    await refreshAll(state.selected);
    renderPlanPanel();
  } catch (error) {
    toast(error.message, true);
  }
}

async function ratifyAutomatic() {
  const data = wizard.data;
  const planId = data?.active_plan_id;
  if (!planId) return;
  const matches = data.review?.summary?.match_validation?.automatic_unreviewed || 0;
  const liked = data.review?.summary?.liked_validation?.automatic_unreviewed || 0;
  const message =
    `Ratificar em lote ${matches} correspondência(s) e ${liked} liked automáticos?\n\n` +
    `Isso registra uma decisão humana append-only vinculada aos hashes atuais e ` +
    `regenera o plano para incluí-los. Nenhuma correspondência ou liked é alterado ` +
    `e nenhuma escrita ocorre no Já Zerei.`;
  if (!confirm(message)) return;
  try {
    const result = await api("/api/migration/ratify", {
      method: "POST",
      body: JSON.stringify({ plan_id: planId, confirm: planId }),
    });
    toast(
      `Ratificação registrada: ${result.ratified_matches} correspondência(s), ${result.ratified_liked} liked.`,
    );
    if (result.plan_id) data.active_plan_id = result.plan_id;
    await refreshAll(state.selected);
    renderPlanPanel();
  } catch (error) {
    toast(error.message, true);
  }
}

async function approveAndPrepare() {
  const data = wizard.data;
  const planId = data?.active_plan_id;
  if (!planId) return;
  const actions = Number(data.plan?.actions || 0);
  const message =
    `Aprovar o plano ${planId} e executar o preflight autenticado?\n\n` +
    `${actions} ação(ões) serão preparadas. Nenhuma review será criada agora. ` +
    `A execução só acontece depois de uma confirmação separada.`;
  if (!confirm(message)) return;
  try {
    const body = await api("/api/migration/approve-and-prepare", {
      method: "POST",
      body: JSON.stringify({ plan_id: planId, confirm: planId }),
    });
    wizard.job = body.job;
    renderJob();
    renderPlanPanel();
    startJobPolling();
  } catch (error) {
    toast(error.message, true);
  }
}

async function executeMigration() {
  const data = wizard.data;
  const execution = data?.active_execution;
  const planId = data?.active_plan_id;
  const actions = Number(data.plan?.actions || 0);
  if (!execution || !planId) return;
  const typed = prompt(
    `Esta ação cria ${actions} review(s) no Já Zerei.\n\nDigite o plan_id para confirmar:\n${planId}`,
  );
  if (typed !== planId) {
    if (typed !== null) toast("plan_id não confere; execução não iniciada.", true);
    return;
  }
  try {
    const result = await api("/api/migration/execute", {
      method: "POST",
      body: JSON.stringify({
        plan_id: planId,
        execution_id: execution.execution_id,
        confirm: planId,
      }),
    });
    toast(`Migração iniciada (${result.total} ações).`);
    await loadWizard();
    startRunPolling();
  } catch (error) {
    toast(error.message, true);
  }
}

async function resumeMigration() {
  const data = wizard.data;
  const execution = data?.active_execution;
  const planId = data?.active_plan_id;
  if (!execution || !planId) return;
  const typed = prompt(
    `Retomar a execução ${execution.execution_id}?\nAções verificadas não serão reenviadas.\n\nDigite o plan_id para confirmar:\n${planId}`,
  );
  if (typed !== planId) return;
  try {
    const result = await api("/api/migration/resume", {
      method: "POST",
      body: JSON.stringify({
        plan_id: planId,
        execution_id: execution.execution_id,
        confirm: planId,
      }),
    });
    toast(`Execução retomada (${result.total} ações).`);
    await loadWizard();
    startRunPolling();
  } catch (error) {
    toast(error.message, true);
  }
}

async function cancelMigration() {
  const data = wizard.data;
  const execution = data?.active_execution;
  const planId = data?.active_plan_id;
  if (!execution || !planId) return;
  if (!confirm("Cancelar a execução? A ação atual será concluída; ações já verificadas permanecem.")) return;
  try {
    const result = await api("/api/migration/cancel", {
      method: "POST",
      body: JSON.stringify({
        plan_id: planId,
        execution_id: execution.execution_id,
        confirm: planId,
      }),
    });
    toast(result.message || "Cancelamento solicitado.");
    await loadWizard();
    if (result.status === "cancelling") startRunPolling();
  } catch (error) {
    toast(error.message, true);
  }
}

/* ---------- 3. conferir ---------- */

function renderResult() {
  const node = document.getElementById("stage-result");
  const data = wizard.data;
  if (!data) {
    node.innerHTML = "";
    return;
  }
  const execution = data.active_execution;
  const report = wizard.report;
  if (!execution && !report) {
    node.innerHTML = `<h2>3. Conferir resultado</h2>
      <div class="future-box">Nenhuma execução selecionada. Conclua a etapa 2 para preparar uma execução.</div>`;
    return;
  }
  const canVerify =
    Boolean(data.active_plan_id && execution) &&
    !data.config?.operation_busy &&
    !isActiveJob(wizard.job);
  let body = "";
  if (!report) {
    body = `<div class="future-box">
      <strong>Nenhuma verificação persistida para esta execução.</strong>
      <p>A conferência relê o destino autenticado e gera um relatório versionado. É somente leitura: não corrige, não cria e não reenvia reviews.</p>
      ${wizard.reportError ? `<div class="danger">${esc(wizard.reportError.error || "Falha ao carregar relatório")}</div>` : ""}
      <button id="verify-destination" class="primary" ${canVerify ? "" : "disabled"}>Conferir resultado no destino</button>
    </div>`;
  } else {
    const actions = report.actions || [];
    const filtered = actions.filter((action) => reportFilterMatches(action, wizard.reportFilter));
    const selected = actions.find((action) => action.action_id === wizard.selectedReportAction);
    body = `
      <div class="execution-card ${report.status === "complete" ? "current" : "blocked"}">
        <div class="execution-head">
          <div>
            <h3>Resultado da execução</h3>
            <div class="history">Plano <code>${esc(report.plan_id)}</code> · execução <code>${esc(report.execution_id)}</code> · ${esc(report.created_at)}</div>
          </div>
          <span class="badge-row">${reportDimensionBadge("Captura", report.capture_status || "unverifiable")} ${reportDimensionBadge("Relatório", report.report_status || "inconclusive")} ${reportDimensionBadge("Causal", report.causal_confirmation_status || "none")}</span>
        </div>
        <div class="summary-strip">
          ${stat("Confirmadas", report.counts?.confirmed || 0)}
          ${stat("Encontradas sem atribuição", report.counts?.found_not_attributed || 0)}
          ${stat("Ausentes", report.counts?.presence_absent || 0)}
          ${stat("Divergentes", report.counts?.divergent || 0)}
          ${stat("Ambíguas", report.counts?.execution_ambiguous || 0)}
          ${stat("Conflitos", report.counts?.execution_conflicts || 0)}
        </div>
        <p class="history">Presença no destino não prova sozinha autoria. “Confirmada” exige execução verificada, conteúdo integral, identidade correta e vínculo causal.</p>
        ${report.plan_blocked ? `<div class="danger"><strong>Confirmação global bloqueada pelo plano.</strong> ${esc((report.plan_block_reasons || []).join("; ") || "plano inválido")}</div>` : ""}
        ${(report.reasons || []).length ? `<div class="warning"><strong>Ressalvas:</strong> ${esc(report.reasons.join("; "))}</div>` : ""}
      </div>
      <div class="execution-card">
        <div class="execution-head">
          <h3>Resultado por ação</h3>
          <span class="history">${filtered.length} de ${actions.length}</span>
        </div>
        <div class="filter-row action-filters" role="group" aria-label="Filtrar verificações">
          ${[
            ["all", "Todas"],
            ["confirmed", "Confirmadas"],
            ["found_not_attributed", "Sem atribuição"],
            ["absent", "Ausentes"],
            ["divergent", "Divergentes"],
            ["ambiguous", "Ambíguas"],
            ["conflict", "Conflitos"],
          ]
            .map(
              ([key, label]) =>
                `<button data-report-filter="${key}" class="${wizard.reportFilter === key ? "active" : ""}" aria-pressed="${wizard.reportFilter === key}">${esc(label)} (${reportFilterCount(actions, key)})</button>`,
            )
            .join("")}
        </div>
        ${
          filtered.length
            ? `<div class="action-table-wrap"><table class="action-table">
                <thead><tr><th>Jogo</th><th>game_id</th><th>Execução</th><th>Presença</th><th>Confirmação</th><th>Nota</th><th>liked</th><th>Motivo</th></tr></thead>
                <tbody>${filtered
                  .map(
                    (action) => `<tr data-report-action="${esc(action.action_id)}" class="${wizard.selectedReportAction === action.action_id ? "selected" : ""}">
                      <td><strong>${esc(action.title || action.source_ref)}</strong><div class="history">${esc(action.source_ref)}</div></td>
                      <td>${esc(action.game_id ?? "—")}</td>
                      <td><span class="status-label ${esc(action.execution_status)}">${esc(statusLabel(action.execution_status))}</span></td>
                      <td>${reportPresenceLabel(action.presence)}</td>
                      <td>${reportConfirmationLabel(action.confirmation)}</td>
                      <td>${esc(action.planned_rating ?? "—")} / ${esc(action.found_rating ?? "—")}</td>
                      <td>${action.liked_status === "unavailable" ? "não verificável" : esc(action.liked_status)}</td>
                      <td>${esc((action.reasons || [])[0] || "—")}</td>
                    </tr>`,
                  )
                  .join("")}</tbody>
              </table></div>`
            : `<div class="empty">Nenhuma ação neste filtro.</div>`
        }
      </div>
      ${selected ? renderReportActionDetail(selected) : ""}
      <details class="execution-card"><summary>Hashes e proveniência do relatório</summary><pre>${esc(JSON.stringify({
        plan_sha256: report.plan_sha256,
        manifest_sha256: report.manifest_sha256,
        execution_sha256: report.execution_sha256,
        snapshot_manifest_sha256: report.snapshot_manifest_sha256,
        snapshot_semantic_status: report.snapshot_semantic_status,
        plan_verification: report.plan_verification,
      }, null, 2))}</pre></details>
    `;
  }

  node.innerHTML = `
    <div class="stage-head">
      <div>
        <h2>3. Conferir resultado</h2>
        <p class="history">Leitura autenticada do destino. Nenhuma review é criada, corrigida ou reenviada nesta etapa.</p>
      </div>
      ${fact("Execução", `<code>${esc(report?.execution_id || execution?.execution_id || "—")}</code>`)}
      ${fact("Integridade do relatório", wizard.reportValid === null ? "—" : wizard.reportValid ? badge("approved", "hash válido") : badge("invalid", "hash inválido"))}
    </div>
    <div class="cta-row">
      ${execution ? `<button id="verify-destination" class="primary" ${canVerify ? "" : "disabled"}>${report ? "Conferir novamente" : "Conferir resultado no destino"}</button>` : `<span class="history">Relatório histórico em modo somente leitura.</span>`}
      ${data.active_verifications?.length > 1 ? `<button id="older-verification" class="ghost">Ver relatório anterior</button>` : ""}
    </div>
    ${renderJobInline("verify_destination")}
    ${body}
  `;
  const verify = document.getElementById("verify-destination");
  if (verify) verify.onclick = verifyDestination;
  const older = document.getElementById("older-verification");
  if (older) older.onclick = () => showStage("advanced");
  node.querySelectorAll("[data-report-filter]").forEach((button) => {
    button.onclick = () => {
      wizard.reportFilter = button.dataset.reportFilter;
      renderResult();
    };
  });
  node.querySelectorAll("tr[data-report-action]").forEach((row) => {
    row.onclick = () => {
      wizard.selectedReportAction =
        wizard.selectedReportAction === row.dataset.reportAction ? null : row.dataset.reportAction;
      renderResult();
    };
  });
}

function reportDimensionBadge(label, value) {
  const good = new Set(["complete", "processed", "completed", "all", "valid"]);
  const bad = new Set(["invalid", "failed", "divergent"]);
  const warn = new Set(["incomplete", "partial", "none", "unverifiable", "inconclusive", "cancelled"]);
  const cls = good.has(value) ? "approved" : bad.has(value) ? "invalid" : warn.has(value) ? "ambiguous" : "";
  return `<span class="badge ${cls}">${esc(label)}: ${esc(value)}</span>`;
}

function reportFilterMatches(action, filter) {
  if (filter === "all") return true;
  return action.confirmation === filter;
}

function reportFilterCount(actions, filter) {
  return actions.filter((action) => reportFilterMatches(action, filter)).length;
}

function reportPresenceLabel(value) {
  return (
    {
      found: "✔ encontrada",
      absent: "✖ ausente",
      inconclusive: "? inconclusiva",
      duplicate: "! duplicada",
      divergent: "! divergente",
    }[value] || esc(value)
  );
}

function reportConfirmationLabel(value) {
  return (
    {
      confirmed: "✔ confirmada",
      found_not_attributed: "? sem atribuição",
      absent: "✖ ausente",
      divergent: "! divergente",
      ambiguous: "? ambígua",
      conflict: "! conflito",
      not_executed: "— não executada",
      inconclusive: "? inconclusiva",
    }[value] || esc(value)
  );
}

function renderReportActionDetail(action) {
  return `<div class="execution-card selected-action">
    <div class="execution-head">
      <h3>${esc(action.title || action.source_ref)} · verificação</h3>
      <span>${reportConfirmationLabel(action.confirmation)}</span>
    </div>
    <dl class="step-facts">
      <dt>Slug</dt><dd>${esc(action.destination_slug || action.source_ref)}</dd>
      <dt>game_id</dt><dd>${esc(action.game_id ?? "—")}</dd>
      <dt>Estado da execução</dt><dd>${esc(statusLabel(action.execution_status))}</dd>
      <dt>Presença</dt><dd>${reportPresenceLabel(action.presence)}</dd>
      <dt>Conteúdo</dt><dd>${esc(action.content_status)}</dd>
      <dt>Atribuição causal</dt><dd>${esc(action.causal)}</dd>
      <dt>Review no snapshot</dt><dd>${esc(action.review_id || "—")}</dd>
    </dl>
    <div class="columns">
      <div><h3>Planejado</h3><dl class="step-facts">
        <dt>Rating</dt><dd>${esc(action.planned_rating ?? "—")}</dd>
        <dt>liked</dt><dd>${esc(action.planned_liked ?? "—")}</dd>
      </dl><div class="review-text action-preview">${esc(action.planned_text || "")}</div></div>
      <div><h3>Encontrado</h3><dl class="step-facts">
        <dt>Rating</dt><dd>${esc(action.found_rating ?? "—")}</dd>
        <dt>liked</dt><dd>${action.liked_status === "unavailable" ? "não verificável" : esc(action.found_liked ?? "—")}</dd>
      </dl><div class="review-text action-preview">${esc(action.found_text || "—")}</div></div>
    </div>
    ${(action.reasons || []).length ? `<div class="warning"><strong>Motivos:</strong> ${esc(action.reasons.join("; "))}</div>` : ""}
    <details><summary>Evidências</summary><pre>${esc(JSON.stringify(action.evidence, null, 2))}</pre></details>
  </div>`;
}

async function verifyDestination() {
  const data = wizard.data;
  const planId = data?.active_plan_id;
  const executionId = data?.active_execution_id;
  if (!planId || !executionId) return;
  if (
    !confirm(
      "Capturar o destino autenticado e gerar um novo relatório? A operação é somente leitura e não corrige nem cria reviews.",
    )
  ) {
    return;
  }
  await startJob("verify_destination", { plan_id: planId, execution_id: executionId });
}

/* ---------- avançado ---------- */

function renderAdvanced() {
  const node = document.getElementById("stage-advanced");
  const data = wizard.data;
  if (!data) {
    node.innerHTML = "";
    return;
  }
  const prepare = data.prepare || {};
  const latest = prepare.latest;
  const safety = data.safety || {};
  const gates = currentGates();
  node.innerHTML = `
    <h2>Avançado</h2>
    <p class="history">Diagnóstico, histórico e evidências. Nada aqui é necessário para o fluxo normal e nada altera gates.</p>
    <div class="advanced-grid">
      <div class="execution-card">
        <h3>Gates de escrita</h3>
        <dl class="step-facts">
          <dt>MIGRATION_ALLOW_WRITES</dt><dd>${badge(gates.allow_writes ? "approved" : "off", String(gates.allow_writes))}</dd>
          <dt>MIGRATION_ENABLE_REAL_WRITE_TRANSPORT</dt><dd>${badge(gates.enable_real_write_transport ? "approved" : "off", String(gates.enable_real_write_transport))}</dd>
          <dt>Escrita possível</dt><dd>${gates.allow_writes && gates.enable_real_write_transport ? badge("approved", "sim") : badge("invalid", "não")}</dd>
        </dl>
        <p class="history">A interface não liga gates. Ambos precisam ser verdadeiros no processo do servidor.</p>
      </div>
      <div class="execution-card">
        <h3>Identidade e perfil</h3>
        <dl class="step-facts">
          <dt>Já Zerei</dt><dd>${esc(data.identity?.jazerei?.masked_user_id || "não verificado")}</dd>
          <dt>Última checagem</dt><dd>${esc(data.identity?.jazerei?.last_check?.checked_at || "nunca nesta sessão")}</dd>
          <dt>Perfil Chromium</dt><dd class="history">${esc(data.config?.browser_profile || "—")}</dd>
          <dt>Stash</dt><dd>${esc(data.identity?.stash?.user || "não configurado")}</dd>
        </dl>
        <div class="cta-row"><button id="login-jazerei-advanced">Entrar/verificar Já Zerei</button></div>
      </div>
    </div>
    ${safety.blocked ? `<div class="execution-card">
      <h3>Reconciliação necessária</h3>
      <p class="history">Resolução usa leitura autenticada do destino. Nenhum POST é repetido.</p>
      ${(safety.ambiguous || []).map((item) => `<div class="action-row">
        <span class="dot"></span>
        <span><code>${esc(item.execution_id)}</code> · ${esc(item.source_ref)} · game_id ${esc(item.game_id ?? "—")}</span>
        <button data-resolve="${esc(item.action_id)}" data-execution="${esc(item.execution_id)}" class="primary">Resolver ambiguidade</button>
      </div>`).join("")}
    </div>` : ""}
    <div class="execution-card">
      <h3>Última preparação</h3>
      ${latest ? `<dl class="step-facts">
        <dt>Run</dt><dd><code>${esc(latest.run_id)}</code></dd>
        <dt>Status</dt><dd>${badge(latest.status, statusLabel(latest.status))}</dd>
        <dt>Início</dt><dd>${esc(latest.started_at || "—")}</dd>
        <dt>Fim</dt><dd>${esc(latest.finished_at || "—")}</dd>
      </dl>
      <details><summary>Eventos da preparação</summary><pre>${esc(JSON.stringify(latest.events || [], null, 2))}</pre></details>` : `<p class="history">Nenhuma preparação registrada nesta máquina.</p>`}
    </div>
    <div class="execution-card">
      <h3>Execuções anteriores</h3>
      <p class="history">Somente leitura. Abrir um item não o torna executável nem altera o estado atual.</p>
      ${(data.all_executions || []).map((exec) => `<div class="action-row">
        <span class="dot"></span>
        <span><code>${esc(exec.execution_id)}</code> · ${esc(exec.plan_id)} · ${esc(statusLabel(exec.status))} · verificadas ${esc(exec.summary?.verified ?? 0)} · ambíguas ${esc(exec.summary?.ambiguous ?? 0)} · conflitos ${esc(exec.summary?.conflicts ?? 0)}</span>
        <button data-advanced-execution="${esc(exec.execution_id)}">Detalhes</button>
      </div>`).join("") || `<div class="history">Nenhuma execução.</div>`}
      ${renderAdvancedDetail()}
    </div>
    <div class="execution-card">
      <h3>Planos anteriores</h3>
      ${(data.plans || []).map((plan) => `<div class="action-row">
        <span class="dot"></span>
        <span><code>${esc(plan.plan_id)}</code> · ${esc(plan.approval_status)} · ${esc(plan.actions)} ações · ${esc(plan.executions)} execuções</span>
        <span class="history">${esc(plan.created_at || "")}</span>
      </div>`).join("") || `<div class="history">Nenhum plano.</div>`}
    </div>
    <div class="execution-card">
      <h3>Verificações</h3>
      ${(data.verifications || []).map((item) => `<div class="action-row">
        <span class="dot"></span>
        <span><code>${esc(item.verification_id)}</code> · ${esc(item.plan_id)} · ${esc(item.execution_id)} · confirmadas ${esc(item.counts?.confirmed ?? 0)} · ausentes ${esc(item.counts?.presence_absent ?? 0)}</span>
        <button data-verification="${esc(item.verification_id)}">Abrir relatório</button>
      </div>`).join("") || `<div class="history">Nenhuma verificação.</div>`}
    </div>
    <div class="execution-card">
      <h3>Coletas e snapshots</h3>
      <details><summary>Coletas do Stash (${(data.source?.runs || []).length})</summary>
        ${(data.source?.runs || []).map((run) => `<div class="action-row"><span class="dot"></span><span><code>${esc(run.run_id)}</code> · ${esc(run.outcome || "?")} · ${esc(run.files)} arquivos</span></div>`).join("")}
      </details>
      <details><summary>Snapshots e runs do destino (${(data.destination?.runs || []).length})</summary>
        ${(data.destination?.runs || []).map((run) => `<div class="action-row"><span class="dot"></span><span><code>${esc(run.run_id)}</code> · ${esc(run.kind || "?")} · ${esc(run.review_count ?? "?")} reviews · ${run.complete ? "completo" : "incompleto"}</span></div>`).join("")}
      </details>
    </div>
  `;
  const loginButton = document.getElementById("login-jazerei-advanced");
  if (loginButton) loginButton.onclick = () => startJob("login_jazerei");
  node.querySelectorAll("[data-advanced-execution]").forEach((button) => {
    button.onclick = async () => {
      try {
        wizard.advancedDetail = await api(`/api/executions/${encodeURIComponent(button.dataset.advancedExecution)}`);
        wizard.advancedError = null;
      } catch (error) {
        wizard.advancedError = error.body || { error: error.message };
      }
      renderAdvanced();
    };
  });
  node.querySelectorAll("[data-verification]").forEach((button) => {
    button.onclick = async () => {
      await loadVerificationReport(button.dataset.verification);
      showStage("result");
    };
  });
  node.querySelectorAll("[data-resolve]").forEach((button) => {
    button.onclick = () => resolveAmbiguity(button.dataset.execution, button.dataset.resolve);
  });
}

function renderAdvancedDetail() {
  if (wizard.advancedError) {
    return `<div class="danger">${esc(wizard.advancedError.error || "Falha ao carregar detalhe")}</div>`;
  }
  const detail = wizard.advancedDetail;
  if (!detail) return "";
  const actions = detail.actions || [];
  return `<div class="execution-card selected-action">
    <h3>${esc(detail.execution_id)}</h3>
    <dl class="step-facts">
      <dt>Status</dt><dd>${badge(detail.status, statusLabel(detail.status))}</dd>
      <dt>Plano</dt><dd><code>${esc(detail.plan_id)}</code></dd>
      <dt>plan_sha256</dt><dd class="history">${esc(detail.plan_sha256)}</dd>
      <dt>manifest_sha256</dt><dd class="history">${esc(detail.manifest_sha256)}</dd>
      <dt>preflight</dt><dd><code>${esc(detail.preflight_id)}</code></dd>
      <dt>Identidade</dt><dd>${esc(detail.user_id_masked || "—")} ${detail.identity_verified ? badge("approved", "verificada") : badge("invalid", "não verificada")}</dd>
      <dt>Integridade</dt><dd>${(detail.artifact_errors || []).length ? badge("invalid", `${detail.artifact_errors.length} erro(s)`) : badge("approved", "sem erros")}</dd>
    </dl>
    <div class="action-table-wrap"><table class="action-table">
      <thead><tr><th>Jogo</th><th>game_id</th><th>Estado</th><th>Escrita</th></tr></thead>
      <tbody>${actions.map((action) => `<tr>
        <td>${esc(action.title || action.source_ref)}</td>
        <td>${esc(action.game_id ?? "—")}</td>
        <td>${esc(statusLabel(action.status))}</td>
        <td>${esc(action.write_result?.status_code ? `HTTP ${action.write_result.status_code}` : "—")}</td>
      </tr>`).join("")}</tbody>
    </table></div>
    <details><summary>Eventos e confirmações</summary><pre>${esc(JSON.stringify({ events: detail.events, confirmations: detail.confirmations }, null, 2))}</pre></details>
  </div>`;
}

async function resolveAmbiguity(executionId, actionId) {
  if (
    !confirm(
      "Resolver a ambiguidade com leitura autenticada do destino? Nenhum POST será feito; a ação pode continuar ambígua se a evidência não bastar.",
    )
  ) {
    return;
  }
  await startJob("resolve_ambiguity", { execution_id: executionId, action_id: actionId });
}

/* ---------- jobs ---------- */

function renderJobInline(kind) {
  const job = wizard.job;
  if (!job || job.kind !== kind || !isActiveJob(job)) return "";
  return renderJobProgress(job);
}

function renderJobProgress(job) {
  const progress = job.progress || {};
  return `<div class="warning"><span class="spinner"></span> <strong>${esc(job.label || job.kind)}</strong>
    <div class="history">${esc(progress.message || progress.step || "Processando…")}</div>
    ${job.cancelable ? `<div class="cta-row"><button id="cancel-job-inline" class="bad">Cancelar</button></div>` : ""}
  </div>`;
}

function renderJob() {
  const node = document.getElementById("job-panel");
  const job = wizard.job;
  if (!job || !isActiveJob(job)) {
    node.hidden = true;
    node.innerHTML = "";
    return;
  }
  const progress = job.progress || {};
  node.hidden = false;
  node.className = `job-panel ${job.status === "waiting_user" ? "waiting_user" : ""}`;
  node.innerHTML = `
    <div class="job-head">
      <span class="spinner"></span>
      <strong>${esc(job.label || job.kind)}</strong>
      <span>${esc(statusLabel(job.status))}</span>
    </div>
    <div class="job-detail">${esc(progress.message || progress.step || "Processando…")}</div>
    ${job.cancelable ? `<div class="job-actions"><button id="cancel-job" class="bad">Cancelar</button></div>` : ""}`;
  const cancel = document.getElementById("cancel-job");
  if (cancel) cancel.onclick = cancelJob;
  bindLiveCancel();
}

function startJobPolling() {
  if (wizard.jobTimer) return;
  wizard.jobTimer = setTimeout(async () => {
    wizard.jobTimer = null;
    try {
      const body = await api("/api/jobs/current");
      wizard.job = body.job;
      renderJob();
      if (isActiveJob(wizard.job)) {
        startJobPolling();
        return;
      }
      if (wizard.job?.status === "done") {
        const result = wizard.job.result || {};
        if (result.pause === "policy") {
          toast("Revise a política automática e confirme para continuar.");
        } else if (result.warning) {
          toast(result.warning);
        } else {
          toast("Operação concluída.");
        }
      }
      if (wizard.job?.status === "failed") toast(wizard.job.error?.error || "Operação falhou.", true);
      if (wizard.job?.status === "cancelled") toast("Operação cancelada; artefatos anteriores preservados.");
      await refreshAll(state.selected);
    } catch (error) {
      wizard.job = null;
      renderJob();
      toast(error.message, true);
    }
  }, 900);
}

function startRunPolling() {
  if (wizard.runTimer) return;
  wizard.runTimer = setTimeout(async () => {
    wizard.runTimer = null;
    try {
      const run = await api("/api/migration/progress");
      if (run.running) {
        if (wizard.data?.plan) wizard.data.plan.run = run;
        renderPlanPanel();
        startRunPolling();
        return;
      }
      await refreshAll(state.selected);
    } catch (error) {
      toast(error.message, true);
    }
  }, 1500);
}

async function startJob(kind, params = {}) {
  try {
    wizard.error = null;
    const body = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ kind, params }),
    });
    wizard.job = body.job;
    renderJob();
    renderAll();
    startJobPolling();
  } catch (error) {
    wizard.error = error.body || { error: error.message };
    renderAll();
    toast(error.message, true);
  }
}

async function cancelJob() {
  try {
    const body = await api("/api/jobs/cancel", { method: "POST", body: "{}" });
    if (!body.cancelled) {
      toast("Nenhuma etapa cancelável em andamento.", true);
      return;
    }
    wizard.job = body.job;
    renderJob();
    startJobPolling();
  } catch (error) {
    toast(error.message, true);
  }
}

/* ---------- navegação ---------- */

function showStage(stage) {
  wizard.stage = stage;
  document.querySelectorAll(".stage-tabs button").forEach((button) => {
    button.classList.toggle("active", button.dataset.stage === stage);
  });
  document.getElementById("stage-prepare").hidden = stage !== "prepare";
  document.getElementById("stage-review").hidden = stage !== "review";
  document.getElementById("stage-result").hidden = stage !== "result";
  document.getElementById("stage-advanced").hidden = stage !== "advanced";
}

document.querySelectorAll(".stage-tabs button").forEach((button) => {
  button.onclick = () => showStage(button.dataset.stage);
});

document.getElementById("search").addEventListener("input", (event) => {
  state.query = event.target.value;
  renderList();
  renderDetail();
});

document.getElementById("refresh-view").onclick = () => refreshAll(state.selected);

showStage("prepare");

Promise.all([loadState(), loadWizard()])
  .catch((error) => {
    document.getElementById("stage-prepare").innerHTML =
      `<div class="danger">${esc(error.message)}</div>`;
    toast(error.message, true);
  });
