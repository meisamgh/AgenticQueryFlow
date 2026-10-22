const $ = (selector) => document.querySelector(selector);
const sessionId = localStorage.getItem("queryRoomSession") || crypto.randomUUID().replaceAll("-", "");
localStorage.setItem("queryRoomSession", sessionId);
document.addEventListener("DOMContentLoaded", () => {
  const el = $("#sessionId");
  if (el) el.textContent = sessionId;
});

const state = {
  busy: false, jobId: null, cancelled: false, currentQuestion: "", ready: false,
  lastBody: null, history: [], saved: [], glossaryByDb: {}, schemaRequest: 0,
  activeRunId: null, diffCache: new Map(),
  restorePending: false,
};
let voiceInput = null;
import("/static/voice.js?v=voice-1").then(async ({VoiceInput}) => {
  voiceInput = new VoiceInput({
    button: $("#voiceButton"), cancelButton: $("#voiceCancelButton"),
    input: $("#messageInput"), status: $("#voiceStatus"),
    onBusy: busy => { $("#sendButton").disabled = busy || state.busy || !state.ready; },
  });
  await voiceInput.initialize();
  $("#voiceButton").hidden = false;
}).catch(() => { $("#voiceStatus").textContent = "Voice input could not load. You can still type a question."; });

const recentStorageKey = "queryRoomRecentQueries";
const savedStorageKey = "queryRoomSaved";
const postSafetyOptimizationCodes = new Set([
  "OPTIMIZATION_NOT_FASTER",
  "OPTIMIZATION_CHANGED_RESULT",
  "OPTIMIZATION_EQUIVALENCE_UNPROVEN",
]);
const EXPLANATION_OPERATIONS_UI = new Set(["EXPLAIN", "EXPLAIN_SQL", "EXPLAIN_RESULT", "EXPLAIN_INTERPRETATION", "EXPLAIN_CONTEXT", "EXPLAIN_FAILURE"]);

function hydrateIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((slot) => {
    const name = slot.dataset.icon;
    if (!slot.innerHTML.trim()) slot.innerHTML = icon(name);
  });
}

function attemptOutcome(attempt) {
  const validation = attempt.validation || {};
  if (validation.valid === true) return "passed";
  if (postSafetyOptimizationCodes.has(validation.code)) return "not_selected";
  return "failed";
}  async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    let detail = body.detail;
    if (Array.isArray(detail)) {
      detail = detail.map((item) => {
        const path = Array.isArray(item.loc) ? item.loc.join(".") : item.loc;
        return [path, item.msg].filter(Boolean).join(": ");
      }).join("; ");
    }
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return response.json();
}

async function initialize() {
  hydrateIcons();
  try {
    await api("/api/health");
    setHealth(true);
    const databases = await api("/api/databases");
    fillSelect(
      "#databaseSelect",
      databases.filter((item) => item.configured),
      "db_id",
      (item) => `${item.db_id} (${item.dialect})`,
      null,
    );
    [...$("#databaseSelect").options].forEach((option) => {
      const match = databases.find((item) => item.db_id === option.value);
      if (match) option.dataset.dialect = match.dialect;
    });
    await Promise.all([loadDatabaseSchema(), refreshHistory(), loadGlossary($("#databaseSelect").value)]);
    const models = await api(`/api/models?refresh=${Date.now()}`, { cache: "no-store" });
    const preferredModel = models.find((item) => item.configured && item.provider === "codecraft")
      || models.find((item) => item.configured && item.provider === "justdowork")
      || models.find((item) => item.configured);
    const preferred = preferredModel && `${preferredModel.provider}|${preferredModel.model}`;
    fillContextSelect(models);
    fillSelect("#sqlModelSelect", models, (item) => `${item.provider}|${item.model}`, modelLabel, preferred);
    state.ready = !!$("#databaseSelect").value && !!preferredModel;
    $("#sendButton").disabled = !state.ready || !!voiceInput?.busy;
    if (!preferredModel) showError("No model can serve queries right now. Hover an entry in the model list to see why.");
    renderSaved();
  } catch (error) {
    setHealth(false);
    showError(error.message);
  }
}

function setHealth(online) {
  const pill = $("#healthStatus");
  pill.classList.toggle("online", online === true);
  pill.classList.toggle("offline", online === false);
  pill.lastChild.textContent = online === false ? " Offline" : " Online";
  $("#connectionStatus").textContent = online === false ? "Connection issue" : "API online";
}

let schemaRequest = 0;
let browserTables = [];
let browserRelationships = [];
let selectedExploreTable = null;

function setView(view, tableName = null) {
  const explore = view === "explore";
  $("#exploreView").hidden = !explore;
  document.querySelectorAll(".workspace-only").forEach(element => { element.hidden = explore; });
  document.querySelectorAll(".side-nav a[data-view]").forEach(link => {
    link.classList.toggle("active", link.dataset.view === view);
  });
  if (tableName) {
    selectedExploreTable = tableName;
    $("#exploreSearch").value = "";
  }
  if (explore) renderExplore();
  $("#mainColumn").scrollTo({ top: 0, behavior: "instant" });
}

function renderExplore() {
  const container = $("#exploreTables");
  const relations = $("#exploreRelationships");
  container.replaceChildren();
  relations.replaceChildren();
  const dbId = $("#databaseSelect").value;
  const term = $("#exploreSearch").value.trim().toLowerCase();
  const tables = browserTables.filter(table => table.name.toLowerCase().includes(term)
    || table.columns.some(column => column.name.toLowerCase().includes(term)));
  $("#exploreTitle").textContent = dbId ? `Explore ${dbId}` : "Explore database";
  $("#exploreSummary").textContent = dbId
    ? `${browserTables.length} tables · ${browserRelationships.length} relationships${term ? ` · ${tables.length} matching tables` : ""}`
    : "Select a configured database in Connections to inspect its schema.";
  if (!tables.length) {
    const empty = document.createElement("p");
    empty.className = "inspector-empty";
    empty.textContent = term ? "No tables or columns match this search." : "No tables are available.";
    container.append(empty);
  }
  tables.forEach(table => {
    const card = document.createElement("section");
    card.className = "explore-table-card";
    if (table.name === selectedExploreTable) card.classList.add("selected");
    const header = document.createElement("header");
    const name = document.createElement("h3");
    name.textContent = table.name;
    const count = document.createElement("span");
    count.textContent = `${table.columns.length} columns`;
    header.append(name, count);
    const columns = document.createElement("ul");
    table.columns.forEach(column => {
      const row = document.createElement("li");
      const label = document.createElement("strong");
      label.textContent = column.name;
      const meta = document.createElement("span");
      meta.textContent = `${column.type}${column.primary_key ? " · Primary key" : ""}`;
      row.append(label, meta);
      columns.append(row);
    });
    card.append(header, columns);
    container.append(card);
  });
  const visibleTables = new Set(tables.map(table => table.name));
  const matchingRelations = browserRelationships.filter(item =>
    !term || visibleTables.has(item.from_table) || visibleTables.has(item.to_table));
  if (!matchingRelations.length) {
    const empty = document.createElement("p");
    empty.className = "inspector-empty";
    empty.textContent = term
      ? "No relationships connect the matching tables. Clear the search to see all relationships."
      : "No declared or profiled relationships are available for this database.";
    relations.append(empty);
  }
  matchingRelations.forEach(item => {
    const row = document.createElement("div");
    row.className = "explore-relationship";
    const path = document.createElement("strong");
    path.textContent = `${item.from_table}.${item.from_column} → ${item.to_table}.${item.to_column}`;
    const source = document.createElement("span");
    source.textContent = item.source === "declared_foreign_key" ? "Declared foreign key"
      : `Profile inference${item.cardinality ? ` · ${item.cardinality.replaceAll("_", " ").toLowerCase()}` : ""}`;
    row.append(path, source);
    relations.append(row);
  });
}

async function loadDatabaseSchema() {
  const request = ++schemaRequest;
  const dbId = $("#databaseSelect").value;
  browserTables = [];
  browserRelationships = [];
  selectedExploreTable = null;
  $("#exploreSearch").value = "";
  if (!dbId) { renderExplore(); renderInspectorSchema(); return; }
  try {
    const schema = await api(`/api/databases/${encodeURIComponent(dbId)}/schema`);
    if (request !== schemaRequest) return;
    browserTables = schema.tables || [];
    browserRelationships = schema.relationships || [];
    renderExplore();
    renderInspectorSchema();
  } catch (error) {
    if (request !== schemaRequest) return;
    $("#exploreSummary").textContent = `Could not load tables: ${error.message}`;
    renderExplore();
  }
}

function fillSelect(selector, items, valueKey, labelKey, preferred) {
  const select = $(selector);
  select.replaceChildren();
  for (const item of items) {
    const option = document.createElement("option");
    option.value = typeof valueKey === "function" ? valueKey(item) : item[valueKey];
    option.textContent = typeof labelKey === "function" ? labelKey(item) : item[labelKey];
    option.disabled = item.configured === false;
    if (item.unavailable_reason) option.title = item.unavailable_reason;
    option.selected = option.value === preferred;
    select.append(option);
  }
  if (!select.value) {
    const first = [...select.options].find(option => !option.disabled);
    if (first) select.value = first.value;
  }
  if (!items.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No configured databases";
    option.disabled = true;
    select.append(option);
  }
}

function modelLabel(item) {
  const label = item.model;
  return item.configured ? label : `${label} — unavailable`;
}

function fillContextSelect(models) {
  const select = $("#contextModelSelect");
  select.replaceChildren();
  const retrieval = document.createElement("option");
  retrieval.value = "retrieval";
  retrieval.textContent = "Hybrid retrieval only";
  retrieval.selected = true;
  select.append(retrieval);
  for (const item of models) {
    const option = document.createElement("option");
    option.value = `${item.provider}|${item.model}`;
    option.textContent = modelLabel(item);
    option.disabled = item.configured === false;
    if (item.unavailable_reason) option.title = item.unavailable_reason;
    select.append(option);
  }
}

function formatSqlForDisplay(sql) {
  if (!sql || sql === "No SQL was accepted.") return sql;

  const protectedParts = [];
  const masked = sql.replace(/'(?:''|[^'])*'|"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^\]]*\]/g, (part) => {
    const marker = `__SQL_PART_${protectedParts.length}__`;
    protectedParts.push(part);
    return marker;
  });

  const clauses = [
    "UNION ALL", "GROUP BY", "ORDER BY", "LEFT OUTER JOIN", "RIGHT OUTER JOIN",
    "FULL OUTER JOIN", "INNER JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN",
    "CROSS JOIN", "UNION", "WITH", "SELECT", "FROM", "JOIN", "WHERE", "HAVING",
    "LIMIT", "OFFSET", "RETURNING",
  ];
  const clausePattern = new RegExp(`\\s*\\b(${clauses.join("|").replaceAll(" ", "\\s+")})\\b\\s*`, "gi");
  let formatted = masked
    .replace(/\s+/g, " ")
    .trim()
    .replace(clausePattern, (_, clause) => `\n${clause.toUpperCase().replace(/\s+/g, " ")} `)
    .replace(/^\n/, "")
    .replace(/\s*;\s*$/, ";");

  let depth = 0;
  formatted = [...formatted].map((character) => {
    if (character === "(") depth += 1;
    if (character === ")") depth = Math.max(0, depth - 1);
    return character === "," && depth === 0 ? ",\n  " : character;
  }).join("");

  formatted = formatted
    .split("\n")
    .map((line) => line.trimEnd())
    .filter(Boolean)
    .join("\n");

  protectedParts.forEach((part, index) => {
    formatted = formatted.replaceAll(`__SQL_PART_${index}__`, part);
  });
  return formatted;
}

const sqlKeywords = new Set([
  "ALL", "AND", "AS", "ASC", "BETWEEN", "BY", "CASE", "CAST", "CROSS", "DESC",
  "DISTINCT", "ELSE", "END", "EXCEPT", "EXISTS", "FROM", "FULL", "GROUP", "HAVING",
  "IN", "INNER", "INTERSECT", "IS", "JOIN", "LEFT", "LIKE", "LIMIT", "NOT", "NULL",
  "OFFSET", "ON", "OR", "ORDER", "OUTER", "OVER", "PARTITION", "RIGHT", "SELECT", "THEN",
  "UNION", "WHEN", "WHERE", "WINDOW", "WITH",
]);

const sqlFunctions = new Set([
  "AVG", "COALESCE", "COUNT", "DATE", "DATETIME", "IIF", "MAX", "MIN", "NULLIF",
  "ROUND", "ROW_NUMBER", "STRFTIME", "SUBSTR", "SUM", "TOTAL",
]);

function schemaIdentifiers() {
  const tables = new Set();
  const columns = new Set();
  browserTables.forEach((table) => {
    tables.add(table.name.toLowerCase());
    (table.columns || []).forEach((column) => columns.add(column.name.toLowerCase()));
  });
  return { tables, columns };
}

function renderHighlightedSql(element, sql) {
  element.replaceChildren();
  String(sql || "").split("\n").forEach((line, index) => {
    const row = document.createElement("span");
    row.className = "sql-line";
    const number = document.createElement("span");
    number.className = "sql-line-number";
    number.textContent = String(index + 1);
    const content = document.createElement("span");
    content.className = "sql-line-content";
    renderSqlTokens(content, line || " ");
    row.append(number, content);
    element.append(row);
  });
}

function renderSqlTokens(element, sql) {
  const ident = schemaIdentifiers();
  const tokens = sql.match(/--[^\n]*|\/\*.*?\*\/|'(?:''|[^'])*'|"(?:""|[^"])*"|`(?:``|[^`])*`|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_$]*\b|[^A-Za-z0-9_]+/g) || [sql];
  tokens.forEach((token, index) => {
    let className = "";
    const upper = token.toUpperCase();
    const lower = token.toLowerCase();
    if (token.startsWith("--") || token.startsWith("/*")) className = "sql-comment";
    else if (token.startsWith("'") || token.startsWith('"') || token.startsWith("`")) className = "sql-string";
    else if (/^\d/.test(token)) className = "sql-number";
    else if (sqlKeywords.has(upper)) className = "sql-keyword";
    else if (sqlFunctions.has(upper)) className = "sql-function";
    if (!className && /[A-Za-z_]/.test(token) && (ident.tables.has(lower) || ident.columns.has(lower))) {
      const span = document.createElement("span");
      span.className = "sql-ident";
      span.textContent = token;
      if (ident.tables.has(lower)) span.dataset.table = token;
      else span.dataset.column = token;
      element.append(span);
      return;
    }
    if (!className) {
      element.append(document.createTextNode(token));
      return;
    }
    const span = document.createElement("span");
    span.className = className;
    span.textContent = token;
    element.append(span);
  });
}

function shortLabel(text, max = 46) {
  return String(text).length > max ? `${String(text).slice(0, max - 3)}…` : String(text);
}

function appendUser(message) {
  $("#emptyState").hidden = true;
  const intro = $("#threadIntro");
  if (intro) {
    intro.hidden = false;
    if (!state.activeRunId) {
      const title = $("#threadIntroTitle");
      if (title) title.textContent = shortLabel(message, 52);
    }
  }
  const crumb = $("#threadBreadcrumb");
  if (crumb) crumb.textContent = shortLabel(message);
  state.currentQuestion = message;
  const article = document.createElement("article");
  article.className = "message user-message";
  const speaker = document.createElement("div");
  speaker.className = "speaker";
  const avatar = document.createElement("span");
  avatar.className = "avatar";
  avatar.innerHTML = icon("user", 13);
  const name = document.createElement("span");
  name.textContent = "You";
  const time = document.createElement("time");
  time.textContent = "just now";
  speaker.append(avatar, name, time);
  const question = document.createElement("p");
  question.className = "question-text";
  question.textContent = message;
  article.append(speaker, question);
  $("#messages").append(article);
}

const pipelineStages = [
  ["conversation", "Understanding"],
  ["retrieval", "Retrieving"],
  ["context_selection", "Context"],
  ["grounding", "Grounding"],
  ["generation", "Generating"],
  ["validation", "Validating"],
  ["execution", "Executing"],
  ["result_review", "Review"],
  ["recovery", "Recovery"],
  ["presentation", "Explaining"],
];

const progressLabels = {
  queued: "Waiting to start…",
  presentation: "Preparing a plain-language answer",
  conversation: "Understanding your request",
  retrieval: "Finding relevant tables and columns",
  context_selection: "Selecting context with your context model",
  grounding: "Verifying schema and metadata",
  generation: "Generating SQL — waiting for the model",
  validation: "Checking SQL safety",
  optimization: "Checking a possible SQL optimization",
  execution: "Running the read-only query",
  result_review: "Reviewing an empty or NULL result",
  recovery: "Investigating the query issue",
  recovery_reasoning: "Recovery agent: deciding what evidence to check",
  recovery_schema: "Recovery agent: inspecting schema",
  recovery_values: "Recovery agent: checking actual database values",
  recovery_probe: "Recovery agent: running a bounded read-only check",
  recovery_checks: "Running bounded diagnostic checks",
  recovery_decision: "Recovery agent: evaluating evidence",
  completed: "Finished",
};

function appendProgress(provider, model, contextMode) {
  $("#emptyState").hidden = true;
  const fragment = $("#progressTemplate").content.cloneNode(true);
  const article = fragment.querySelector("article");
  const tracker = article.querySelector(".pipeline-tracker");
  tracker.replaceChildren();
  pipelineStages.forEach(([stage, label]) => {
    if (stage === "context_selection" && contextMode === "retrieval") return;
    const step = document.createElement("span");
    step.dataset.stage = stage;
    step.textContent = label;
    tracker.append(step);
  });
  article.querySelector(".progress-detail").textContent = `${provider} · ${model}`;
  hydrateIcons(article);
  $("#messages").append(article);
  article.scrollIntoView({ behavior: "smooth", block: "end" });
  return article;
}

function wait(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function waitForJob(jobId, progress) {
  while (true) {
    const job = await api(`/api/chat/jobs/${jobId}`);
    const seconds = ((job.elapsed_ms || 0) / 1000).toFixed(1);
    updatePipeline(progress, job.stage, job.status, job.events || [], job.response);
    const label = progressLabels[job.stage] || "Processing your request";
    progress.querySelector(".progress-stage").textContent = label;
    progress.querySelector(".progress-detail").textContent = `${seconds}s elapsed · ${Number(seconds) > 20 ? "Still working. You can cancel without changing your previous result." : "You can cancel at any time."}`;
    if (job.understanding) {
      const understanding = $("#understandingContent");
      understanding.replaceChildren();
      const title = document.createElement("strong");
      title.textContent = `Interpreting as ${job.understanding.operation.replaceAll("_", " ").toLowerCase()}`;
      const detail = document.createElement("p");
      detail.textContent = job.understanding.resolved_instruction;
      understanding.append(title, detail);
    }
    const log = progress.querySelector(".progress-history ul");
    log.replaceChildren();
    (job.events || []).forEach(event => {
      const item = document.createElement("li");
      item.textContent = `${(event.elapsed_ms / 1000).toFixed(1)}s · ${progressLabels[event.stage] || event.stage}`;
      log.append(item);
    });
    if (job.status === "completed") return { ...job.response, progress_events: job.events || [] };
    if (job.status === "cancelled") throw new Error("Request cancelled.");
    if (job.status === "failed") throw new Error(job.error || "Chat job failed.");
    await wait(500);
  }
}

function visiblePipelineStage(stage) {
  if (stage.startsWith("recovery_")) return "recovery";
  if (stage === "optimization") return "validation";
  return stage;
}

function updatePipeline(progress, currentStage, jobStatus, events = [], response = null) {
  const visibleStage = visiblePipelineStage(currentStage);
  const seen = new Set(JSON.parse(progress.dataset.seenStages || "[]"));
  events.forEach((event) => seen.add(visiblePipelineStage(event.stage)));
  if (progress.dataset.activeStage && progress.dataset.activeStage !== visibleStage) seen.add(progress.dataset.activeStage);
  seen.add(visibleStage);
  const resultAccepted = jobStatus === "completed" && response?.generation?.accepted === true;
  if (resultAccepted) seen.add("result_review");
  progress.dataset.activeStage = visibleStage;
  progress.dataset.seenStages = JSON.stringify([...seen]);
  progress.querySelectorAll(".pipeline-tracker span").forEach((step) => {
    const completed = seen.has(step.dataset.stage)
      && (jobStatus === "completed" || step.dataset.stage !== visibleStage);
    step.classList.toggle("complete", completed);
    step.classList.toggle("active", jobStatus !== "completed" && step.dataset.stage === visibleStage);
    const recoverySkipped = resultAccepted && step.dataset.stage === "recovery" && !seen.has("recovery");
    step.classList.toggle("skipped", recoverySkipped);
    if (recoverySkipped) step.title = "Not needed — the query succeeded without recovery";
  });
}

async function send(message, feedbackCategory = null) {
  if (state.busy || voiceInput?.busy || !message.trim()) return;
  setView("workspace");
  if (!state.ready) {
    showError("A configured database and SQL model are required before asking a question.");
    return;
  }
  state.busy = true;
  voiceInput?.setQueryBusy(true);
  $("#sendButton").disabled = true;
  ["#databaseSelect", "#sqlModelSelect", "#contextModelSelect", "#resetButton"].forEach(id => { $(id).disabled = true; });
  appendUser(message.trim());
  const [provider, model] = $("#sqlModelSelect").value.split("|");
  const contextSelection = $("#contextModelSelect").value;
  const contextMode = contextSelection === "retrieval" ? "retrieval" : "model1";
  const [contextProvider, contextModel] = contextMode === "model1" ? contextSelection.split("|") : [null, null];
  const started = performance.now();
  const progress = appendProgress(provider, model, contextMode);
  state.cancelled = false;
  state.pendingVizRequest = vizRequested(message);
  $("#cancelButton").hidden = false;
  try {
    if (state.restorePending && state.activeRunId) await continueRun(state.activeRunId, false);
    const created = await api("/api/chat/jobs", {
      method: "POST",
      body: JSON.stringify({
        session_id: sessionId,
        db_id: $("#databaseSelect").value,
        message: message.trim(),
        parent_run_id: state.activeRunId,
        provider,
        model,
        context_provider: contextMode === "model1" ? contextProvider : null,
        context_model: contextMode === "model1" ? contextModel : null,
        context_mode: contextMode,
        execute: true,
        max_rows: 100,
        feedback_category: feedbackCategory,
      }),
    });
    state.jobId = created.job_id;
    if (state.cancelled) await api(`/api/chat/jobs/${created.job_id}`, { method: "DELETE" });
    const body = await waitForJob(created.job_id, progress);
    progress.remove();
    state.activeRunId = body.history_saved ? body.run_id : null;
    await appendAssistant(body, (performance.now() - started) / 1000);
    void refreshHistory();
  } catch (error) {
    progress.remove();
    showError(error.message);
  } finally {
    state.busy = false;
    voiceInput?.setQueryBusy(false);
    state.jobId = null;
    $("#sendButton").disabled = !state.ready;
    ["#databaseSelect", "#sqlModelSelect", "#contextModelSelect", "#resetButton"].forEach(id => { $(id).disabled = false; });
    $("#cancelButton").hidden = true;
    $("#messageInput").focus();
  }
}

function responseStatus(body, attempts) {
  const accepted = body.generation?.accepted === true;
  const explanatory = EXPLANATION_OPERATIONS_UI.has(body.operation || "");
  const chartOnly = body.operation === "VISUALIZE";
  const modelUnavailable = body.generation && !accepted && body.generation.termination_reason === "model_error";
  const status = chartOnly ? (body.presentation?.chart_spec ? "CHART FROM STORED DATA" : "CHART UNAVAILABLE")
    : explanatory ? "NO NEW QUERY"
    : accepted ? (body.generation.execution_status === "ACCEPTED" ? "EXECUTED" : body.generation.execution_status || "SAFE SQL")
    : modelUnavailable ? "MODEL UNAVAILABLE" : "FAILED";
  return { accepted, explanatory, modelUnavailable, status };
}

function renderCallout(root, kind, iconName, text) {
  if (!text) return;
  const callout = root.querySelector(".callout");
  callout.hidden = false;
  callout.classList.remove("error", "warn", "info");
  callout.classList.add(kind);
  callout.querySelector(".callout-icon").innerHTML = icon(iconName);
  callout.querySelector(".callout-text").textContent = text;
}

function updateStatusPill(fragment, body, accepted, status) {
  const pill = fragment.querySelector(".status-pill");
  if (!pill) return;
  const label = pill.querySelector(".status-pill-label");
  const slot = pill.querySelector("[data-icon]");
  let iconName = "check";
  let text = accepted ? "Ready" : "Query issue";
  let kind = "ok";
  if (status === "MODEL UNAVAILABLE") { kind = "error"; iconName = "alert"; text = "Model unavailable"; }
  else if (status === "FAILED") { kind = "error"; iconName = "alert"; text = "Failed"; }
  else if (status === "NO NEW QUERY") { kind = "warn"; iconName = "info"; text = "Answered without a new query"; }
  else if (status === "CHART UNAVAILABLE") { kind = "warn"; iconName = "alert"; text = "Chart unavailable"; }
  else if (status === "CHART FROM STORED DATA") { text = "Chart ready"; }
  else if (accepted && String(status).startsWith("EXEC")) { text = "Executed"; }
  pill.classList.remove("warn", "error");
  if (kind === "warn") pill.classList.add("warn");
  if (kind === "error") pill.classList.add("error");
  if (label) label.textContent = text;
  if (slot) slot.innerHTML = icon(iconName);
}

/* ---------- Intent-adaptive response modes (Queryflow v3) ---------- */

const VIZ_REQUEST_PATTERN = /\b(?:plot(?:s|ted|ting)?|chart(?:s|ed|ing)?|graph(?:s|ed|ing)?|visuali[sz]|histogram|heatmap|treemap|box ?plot|donut|scatter plot|line chart|bar chart|area chart|pie chart|stacked (?:bar|area))\b/i;

function vizRequested(...messages) {
  return messages.filter(Boolean).some((message) => VIZ_REQUEST_PATTERN.test(String(message)));
}

function responseMode(body, opts = {}) {
  if (EXPLANATION_OPERATIONS_UI.has(body.operation || "") || body.clarification_required) return "explanation";
  const generation = body.generation || {};
  if (generation.accepted !== true) return "sql";
  const presentation = body.presentation;
  if (presentation?.chart_spec) return "chart";
  if (opts.viz || body.operation === "VISUALIZE") return "chart_unavailable";
  return "sql";
}

/* The backend's versioned PresentationState is authoritative when present:
   analytical corrections received a "chart" mode stay chart-first, and a new
   independent question comes back as "sql". Client heuristics only serve
   responses produced before this contract existed. */
function presentationMode(body, opts = {}) {
  const stateMode = body.presentation_state?.mode;
  if (stateMode === "chart") {
    return body.presentation?.chart_spec ? "chart" : "chart_unavailable";
  }
  if (stateMode === "sql") {
    const fallback = responseMode(body, opts);
    return fallback === "explanation" ? "explanation" : "sql";
  }
  return responseMode(body, opts);
}

function isChartMode(screen) {
  return screen === "chart";
}

/* Queryflow v4: contextual investigations are explicit user actions, never SQL executed
   from a table cell or a chart event. All follow-ups go through the existing API. */
async function sendFromAnswer(runId, prompt) {
  if (runId && runId !== state.activeRunId) await continueRun(runId, false);
  await send(prompt);
}
let activeInsight = null;
function dismissInsight() {
  if (activeInsight) { activeInsight._cleanup?.(); activeInsight.remove(); }
  activeInsight = null;
}
function placeInsight(anchor, panel) {
  dismissInsight();
  panel.className = "query-insight-popover";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "Investigate result");
  panel.addEventListener("click", event => event.stopPropagation());
  document.body.append(panel);
  activeInsight = panel;
  const bounds = anchor.getBoundingClientRect();
  const x = Math.max(12, Math.min(bounds.left, innerWidth - panel.offsetWidth - 12));
  const y = bounds.bottom + panel.offsetHeight + 12 > innerHeight
    ? Math.max(12, bounds.top - panel.offsetHeight - 8) : bounds.bottom + 8;
  panel.style.left = x + "px";
  panel.style.top = y + "px";
  const escape = event => {
    if (event.key === "Escape") { dismissInsight(); document.removeEventListener("keydown", escape); }
  };
  document.addEventListener("keydown", escape);
  // Dispose keyboard listener when the next popover is opened.
  panel._cleanup = () => document.removeEventListener("keydown", escape);
}
function insightMenu(anchor, heading, description, items, runId = null) {
  const panel = document.createElement("div");
  const title = document.createElement("strong");
  title.className = "query-insight-title";
  title.textContent = heading;
  const caption = document.createElement("p");
  caption.textContent = description;
  panel.append(title, caption);
  items.forEach(({label, prompt}) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "query-insight-action";
    button.textContent = label;
    button.addEventListener("click", async () => {
      dismissInsight();
      if (state.busy || !state.ready) return;
      // Deliberate click on an action, routed through existing interpretation and SQL safety.
      try { await sendFromAnswer(runId, prompt); }
      catch (error) { showError(error.message); }
    });
    panel.append(button);
  });
  placeInsight(anchor, panel);
}
function editSemanticChip(anchor, dimension, current, runId) {
  const panel = document.createElement("form");
  const title = document.createElement("label");
  title.className = "query-insight-title";
  title.textContent = "Change " + dimension.toLowerCase();
  const input = document.createElement("input");
  input.className = "query-insight-input";
  input.value = current;
  input.maxLength = 220;
  input.required = true;
  input.setAttribute("aria-label", "New " + dimension.toLowerCase());
  const note = document.createElement("p");
  note.textContent = "Applies a conversational correction through validated SQL.";
  const apply = document.createElement("button");
  apply.className = "query-insight-primary";
  apply.type = "submit";
  apply.textContent = "Apply change";
  panel.append(title, input, note, apply);
  panel.addEventListener("submit", async event => {
    event.preventDefault();
    if (state.busy || !state.ready || !input.value.trim()) return;
    const revised = input.value.trim();
    dismissInsight();
    const prompt = "Change the " + dimension.toLowerCase() + " in the previous query from " +
      JSON.stringify(current) + " to " + JSON.stringify(revised) +
      ". Preserve all other confirmed metrics, filters, and grouping requirements.";
    try { await sendFromAnswer(runId, prompt); }
    catch (error) { showError(error.message); }
  });
  placeInsight(anchor, panel);
  input.focus();
  input.select();
}
function renderSemanticChips(fragment, body) {
  const strip = fragment.querySelector(".query-semantic-strip");
  if (!strip) return;
  const contract = body.state?.semantic_contract;
  if (!contract || body.clarification_required) return;
  const items = [];
  if (contract.measures?.length) items.push(["Metric", contract.measures.join(", ")]);
  if (contract.aggregation) items.push(["Aggregation", contract.aggregation]);
  if (contract.grain?.length) items.push(["Grouping", contract.grain.join(", ")]);
  if (contract.filters?.length) {
    for (const item of contract.filters.slice(0, 3)) {
      items.push(["Filter", [item.operand || item.column, item.operator || "=", JSON.stringify(item.value)].filter(Boolean).join(" ")]);
    }
  }
  if (!items.length) return;
  strip.hidden = false;
  const eyebrow = document.createElement("span");
  eyebrow.className = "query-semantic-eyebrow";
  eyebrow.textContent = "UNDERSTOOD AS";
  strip.append(eyebrow);
  items.slice(0, 6).forEach(([kind, value]) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "query-semantic-chip";
    chip.title = "Refine " + kind.toLowerCase() + " in the next query";
    const k = document.createElement("small");
    k.textContent = kind;
    const v = document.createElement("span");
    v.textContent = value.length > 55 ? value.slice(0, 52) + "…" : value;
    chip.append(k, v);
    chip.addEventListener("click", () => editSemanticChip(chip, kind, value, body.run_id));
    strip.append(chip);
  });
}
function attachResultInvestigations(table, body) {
  const cols = body.generation?.columns || [];
  const rows = body.generation?.rows || [];
  const actual = table?.tBodies?.[0];
  if (!actual || !cols.length) return;
  Array.from(actual.rows).forEach((tr, r) => {
    Array.from(tr.cells).forEach((cell, c) => {
      const value = rows[r]?.[c], column = cols[c];
      if (value == null || !column) return;
      cell.classList.add("investigable-cell");
      cell.tabIndex = 0;
      cell.title = "Select to investigate this value";
      const action = () => {
        const key = JSON.stringify(String(column)), val = JSON.stringify(value);
        insightMenu(cell, String(column) + ": " + String(value), "Use this observed value as the starting point for a follow-up.", [
          {label:"Investigate this value",prompt:"Investigate the observed value " + val +
            " in result column " + key + " from the previous query. Keep the existing analytical context and explain what can be verified."},
          {label:"Filter to this value",prompt:"Refine the previous query to focus on " + key + " = " + val +
            ". Verify that this result column maps to an appropriate source dimension; ask for clarification if it is an aggregate or ambiguous alias."},
          {label:"Compare with other values",prompt:"Compare " + key + " = " + val +
            " to other values of the same dimension, preserving the previous metric definition and relevant filters."}
        ], body.run_id);
      };
      cell.addEventListener("click", action);
      cell.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); action(); }
      });
    });
  });
}
function attachChartInvestigations(article, presentation, runId) {
  const spec = presentation?.chart_spec;
  if (!article._echart?.onSelect || !spec?.data?.length) return;
  article._echart.onSelect(point => {
    const observedX = point.name !== "" && point.name != null
      ? point.name : (Array.isArray(point.value) ? point.value[0] : undefined);
    const matches = spec.data.filter(row => String(row[spec.x]) === String(observedX)
      && (!spec.series || !point.seriesName || String(row[spec.series]) === String(point.seriesName)));
    if (!matches.length) return;
    const value = matches[0][spec.x];
    const anchor = article.querySelector(".chart-slot");
    const dimension = JSON.stringify(spec.x), selected = JSON.stringify(value);
    const y = JSON.stringify(spec.y);
    insightMenu(anchor, String(spec.x) + ": " + String(value), "Explore this chart category using the original query.", [
      {label:"Break down this point", prompt:"For the previous chart, break down the " + y +
        " measure where " + dimension + " = " + selected +
        " by a meaningful verified dimension. Preserve existing filters."},
      {label:"Compare with other periods or groups", prompt:"Compare the previous chart result at " +
        dimension + " = " + selected + " with relevant neighboring periods or groups, maintaining its metric definition."},
      {label:"Show contributing records", prompt:"Show the underlying records contributing to the previous chart at " +
        dimension + " = " + selected + ". Verify the source grain and joins; do not infer individual records from aggregates."}
    ], runId);
  });
}

function renderChartInto(slot, presentation, generation) {
  slot._echart?.dispose?.();
  slot.replaceChildren();
  slot.classList.add("chart-slot-ec");
  const ec = window.QueryflowECharts?.render(slot, presentation, generation) || null;
  slot._echart = ec;
  if (ec) {
    return { echarts: ec };
  }
  slot.classList.remove("chart-slot-ec");
  const note = document.createElement("p");
  note.className = "chart-note";
  note.textContent = "Interactive charts are unavailable. SQL and the exact result table remain below.";
  slot.append(note);
  return { echarts: null };
}

/* Independent SQL/data inspection for chart-first responses: at most one
   panel is open at a time, each closable without touching the chart. */
function setChartInspection(article, which) {
  const split = article.querySelector(".sql-result-split");
  const sqlPane = article.querySelector(".sql-panel-pane");
  const dataPane = article.querySelector(".result-panel-pane");
  if (!split || !sqlPane || !dataPane) return;
  split.classList.add("chart-inspect");
  split.hidden = which === null;
  sqlPane.hidden = which !== "sql";
  dataPane.hidden = which !== "data";
}

function wireChartSection(article, presentation, generation, runId) {
  const section = article.querySelector(".chart-section");
  const slot = article.querySelector(".chart-slot");
  if (!section || !slot) return;
  const kinds = section.querySelector(".chart-kinds");
  const more = section.querySelector(".chart-more");
  const menu = section.querySelector(".chart-menu");
  article._echart = null;
  article._chartType = presentation.chart_type;
  kinds.hidden = true;
  article._echart = (renderChartInto(slot, presentation, generation)).echarts;
  /* Theme switches recolor the shared token palette, so live charts repaint. */
  article._chartRerender = () => {
    const result = renderChartInto(slot, presentation, generation);
    article._echart = result.echarts;
    attachChartInvestigations(article, presentation, runId);
  };
  [".sql-panel-pane", ".result-panel-pane"].forEach((selector) => {
    const header = article.querySelector(`${selector} > header`);
    if (!header) return;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "icon-button chart-inspect-close";
    close.title = "Close panel";
    close.setAttribute("aria-label", "Close panel");
    close.innerHTML = icon("x", 14);
    header.append(close);
    close.addEventListener("click", () => setChartInspection(article, null));
  });
  attachChartInvestigations(article, presentation, runId);
  const closeMenu = () => { menu.hidden = true; more.setAttribute("aria-expanded", "false"); };
  more.addEventListener("click", (event) => {
    event.stopPropagation();
    menu.hidden = !menu.hidden;
    more.setAttribute("aria-expanded", String(!menu.hidden));
  });
  menu.addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("click", closeMenu, { once: false });
  menu.querySelectorAll("[data-chart-action]").forEach((item) => {
    item.addEventListener("click", () => {
      closeMenu();
      const action = item.dataset.chartAction;
      if (action === "view-sql") {
        setChartInspection(article, "sql");
      } else if (action === "view-data") {
        setChartInspection(article, "data");
      } else if (action === "download") {
        exportChartPng(article);
      } else if (action === "expand") {
        openChartFullscreen(article, presentation, generation);
      } else if (action === "inspect") {
        revealInspector();
        const validation = $("#validationSection");
        if (validation) { validation.open = true; validation.scrollIntoView({ behavior: "smooth", block: "start" }); }
      }
    });
  });
}

function toast(message) {
  document.querySelector(".toasty")?.remove();
  const node = document.createElement("div");
  node.className = "toasty";
  node.textContent = message;
  document.body.append(node);
  window.setTimeout(() => node.remove(), 2600);
}

function exportedChartSource(article) {
  if (article._echart?.getDataURL) {
    return article._echart.getDataURL({ pixelRatio: 2, backgroundColor: themeBackgroundColor() });
  }
  return null;
}

function themeBackgroundColor() {
  return getComputedStyle(document.body).backgroundColor || "#ffffff";
}

function exportChartPng(article) {
  const dataUrl = exportedChartSource(article);
  if (!dataUrl) { toast("Chart export is unavailable while the chart library is offline."); return; }
  const link = document.createElement("a");
  link.href = dataUrl;
  link.download = `queryflow-chart-${new Date().toISOString().slice(0, 10)}.png`;
  link.click();
}

function openChartFullscreen(article, presentation, generation) {
  closeChartFullscreen();
  const overlay = document.createElement("div");
  overlay.className = "chart-fullscreen";
  const inner = document.createElement("div");
  inner.className = "chart-fullscreen-inner";
  inner.setAttribute("role", "dialog");
  inner.setAttribute("aria-modal", "true");
  inner.setAttribute("aria-label", "Expanded chart");
  const header = document.createElement("header");
  const title = document.createElement("strong");
  title.textContent = presentation.title || `${presentation.y} by ${presentation.x}`;
  const closeButton = document.createElement("button");
  closeButton.type = "button";
  closeButton.className = "icon-button";
  closeButton.id = "chartFullscreenClose";
  closeButton.title = "Close expanded chart";
  closeButton.innerHTML = icon("x", 16);
  const slotHolder = document.createElement("div");
  slotHolder.className = "chart-fullscreen-slot";
  header.append(title, closeButton);
  inner.append(header, slotHolder);
  overlay.append(inner);
  overlay.addEventListener("click", (event) => { if (event.target === overlay) closeChartFullscreen(); });
  document.body.append(overlay);
  closeButton.addEventListener("click", closeChartFullscreen);
  const slot = slotHolder;
  const { echarts } = renderChartInto(slot, presentation, generation);
  overlay._echart = echarts;
  overlay._chartRerender = () => {
    overlay._echart = renderChartInto(slot, presentation, generation).echarts;
  };
}

function closeChartFullscreen() {
  document.querySelectorAll(".chart-fullscreen").forEach((overlay) => {
    overlay._echart?.dispose?.();
    overlay.remove();
  });
}

function revealInspector() {
  if (window.innerWidth <= 1020) document.body.classList.add("inspector-open");
  else $("#appShell").classList.remove("inspector-hidden");
}

function mergeRunChanges(changes) {
  const merged = [];
  for (const change of changes) {
    const last = merged[merged.length - 1];
    if (last && last.kind === change.kind) last.token = `${last.token} ${change.token}`.trim();
    else merged.push({ kind: change.kind, token: change.token });
  }
  return merged;
}

function recoveryTimelineSteps(recovery, events = []) {
  if (!recovery || recovery.activated === false) return [];
  if (events.length) {
    const titles = {
      SQL_FAILED: "SQL attempt failed", SCHEMA_INSPECTED: "Inspected live schema",
      VALUES_INSPECTED: "Checked database values", DATABASE_PROBED: "Ran a bounded database check",
      TOOL_OBSERVED: "Collected evidence", CLAIM_REPORTED: "Agent interpretation",
      RECOVERY_DECISION: "Recovery decision", SQL_EXECUTED: "Selected SQL executed",
    };
    return events.map(event => ({
      icon: event.status === "FAILED" ? "alert" : event.status === "EXECUTED" ? "check" : "search",
      tone: event.status === "FAILED" ? "warn" : event.status === "EXECUTED" ? "ok" : "accent",
      title: `${titles[event.event_type] || event.event_type}${event.attempt ? ` · Attempt ${event.attempt}` : ""}`,
      detail: event.summary, evidenceId: event.evidence_ids.join(", "),
      observation: event.detail, sql: event.sql,
    }));
  }
  const steps = [];
  const mode = recovery.mode || "FAILURE";
  steps.push({
    icon: "alert", tone: "warn", title: `Initial ${mode === "FAILURE" ? "failure" : mode.toLowerCase()}`,
    detail: [recovery.failure_code, recovery.diagnosis_summary].filter(Boolean).join(" — ") || "The first candidate hit an anomaly.",
  });
  (recovery.tool_calls || []).forEach((call) => {
    const toolIcons = { inspect_schema: "table", inspect_values: "search", query_database: "database" };
    steps.push({
      icon: toolIcons[call.tool] || "search",
      tone: "accent",
      title: `Recovery tool · ${call.tool}`,
      detail: [call.purpose, call.result_summary].filter(Boolean).join(" — "),
      evidenceId: call.evidence_id || null,
    });
  });
  (recovery.claims || []).forEach((claim) => {
    steps.push({
      icon: "check", tone: "ok", title: `Claim · ${claim.claim_type.replaceAll("_", " ").toLowerCase()}`,
      detail: `${claim.statement} — evidence: ${(claim.evidence_ids || []).join(", ")}`,
    });
  });
  (recovery.evidence || []).forEach((item, index) => {
    steps.push({
      icon: "shield", tone: "info", title: "Recorded evidence",
      detail: typeof item === "string" ? item : JSON.stringify(item),
    });
  });
  if (recovery.agent_diagnosis || recovery.agent_action) {
    steps.push({
      icon: recovery.agent_action === "REPAIR" ? "refresh" : recovery.agent_action === "INFORM" ? "info" : "alert",
      tone: recovery.agent_action === "REPAIR" ? "accent" : recovery.agent_action === "INFORM" ? "ok" : "warn",
      title: `Agent decision · ${recovery.agent_action || "REVIEW"}`,
      detail: [recovery.agent_diagnosis, recovery.repair_instruction ? `Repair instruction: ${recovery.repair_instruction}` : null]
        .filter(Boolean).join(" "),
    });
  }
  return steps;
}

async function appendAssistant(body, elapsed, options = {}) {
  const fragment = $("#assistantTemplate").content.cloneNode(true);
  const article = fragment.querySelector("article");
  const generation = body.generation || {};
  const accepted = generation.accepted === true;
  const attempts = generation.attempts || [];
  const usage = body.token_usage || generation.token_usage || {};
  const tokenTotal = usage.input_tokens == null && usage.output_tokens == null
    ? null
    : (usage.input_tokens || 0) + (usage.output_tokens || 0);
  const timings = body.timings_ms || {};
  const optimization = generation.optimization || null;
  const { explanatory, modelUnavailable, status } = responseStatus(body, attempts);
  const badges = [];
  if (status === "FAILED" || modelUnavailable) badges.push(status === "FAILED" ? "FAILED" : "MODEL UNAVAILABLE");
  if (optimization
    && (optimization.status === "changed_result" || optimization.status === "equivalence_unproven")) {
    badges.push("OPTIMIZATION SKIPPED");
  }
  if (options.restored) badges.push(`SAVED ${new Date(options.run.created_at * 1000).toLocaleString()}`);
  // Latency, attempts, token accounting, pipeline order and the verified
  // model context live in the inspector's run details — not on the message.
  const meta = fragment.querySelector(".response-meta");
  const detail = document.createElement("details");
  detail.className = "response-meta-disclosure";
  const disclosureTitle = document.createElement("summary");
  disclosureTitle.textContent = "Execution details";
  detail.append(disclosureTitle);
  meta.before(detail);
  detail.append(meta);
  badges.forEach((value) => {
    const badge = document.createElement("span");
    badge.className = `badge${value === "FAILED" || value === "MODEL UNAVAILABLE" ? " error" : value === "RESTORED" ? " accent" : ""}`;
    badge.textContent = value;
    meta.append(badge);
  });
  if (badges.length) meta.hidden = false;
  fragment.querySelector(".response-heading h3").textContent = modelUnavailable ? "Model unavailable"
    : body.operation === "VISUALIZE" ? "Visualization"
    : explanatory ? "Explanation" : accepted ? "Answer" : "Query issue";
  updateStatusPill(fragment, body, accepted, status);

  const responseMessage = body.explanation || body.presentation?.summary || body.message || "";
  fragment.querySelector(".response-note").textContent = responseMessage;
  const note = fragment.querySelector(".presentation-note");
  if (body.presentation?.note) { note.hidden = false; note.textContent = body.presentation.note; }
  renderSemanticChips(fragment, body);

  // Distinct empty / anomaly states, grounded in the real response.
  if (accepted && generation.row_count === 0) {
    renderCallout(fragment, "warn", "search", generation.recovery?.diagnosis_summary
      || "The query executed but returned no rows. Adjust the question or check the filters.");
  } else if (!accepted && !explanatory && !modelUnavailable) {
    renderCallout(fragment, "error", "alert", body.human_review?.reason || body.message || "The query could not be completed.");
  }

  // Intent-adaptive rendering: ordinary requests show SQL + data (Mode A),
  // explicit visualization requests show a chart-first response (Mode B).
  const viz = options.viz ?? state.pendingVizRequest === true;
  state.pendingVizRequest = null;
  const mode = presentationMode(body, { viz });
  const chartSection = fragment.querySelector(".chart-section");
  const presentation = body.presentation || null;
  const chartDataComplete = body.presentation_state?.data_complete !== false
    && !generation.truncated
    && (!(presentation?.chart_spec) || presentation.chart_spec.source_row_count === 0
      || presentation.chart_spec.data.length >= Math.min(presentation.chart_spec.source_row_count, 500));
  if (mode === "chart" && chartDataComplete) {
    chartSection.hidden = false;
    const spec = presentation.chart_spec;
    fragment.querySelector(".chart-title").textContent = presentation.title || `${presentation.y} by ${presentation.x}`;
    fragment.querySelector(".chart-sub").textContent = [
      spec.chart_type.replaceAll("_", " "), ...(spec.warnings || []),
    ].join(" · ");
    fragment.querySelector(".sql-result-split").hidden = true;
    fragment.querySelector(".sql-panel-pane").hidden = true;
    fragment.querySelector(".result-panel-pane").hidden = true;
    const chartFoot = document.createElement("div");
    chartFoot.className = "chart-foot";
    const chartLegend = document.createElement("span");
    chartLegend.className = "legend";
    chartLegend.textContent = spec.y_label || generation.columns?.[1] || "Results";
    const chartFootnote = document.createElement("span");
    chartFootnote.className = "plot-footnote";
    chartFootnote.textContent = `Generated on request · ${spec.data.length} result rows`;
    chartFoot.append(chartLegend, chartFootnote);
    chartSection.append(chartFoot);
  } else if (mode === "chart") {
    // A versioned "chart" state with incomplete data must never render a
    // partial chart; demand a fresh validated execution instead.
    chartSection.remove();
    renderCallout(fragment, "warn", "alert",
      "The stored chart snapshot is incomplete. Rerun the query to restore this visualization from a fresh, complete result.");
    state.lastPresentationMode = "chart_incomplete";
  } else if (mode === "chart_unavailable") {
    chartSection.remove();
    renderCallout(fragment, "warn", "alert", presentation?.chart_reason
      || body.message || "This result cannot support the requested chart. SQL and data are shown below.");
  } else {
    chartSection.remove();
  }

  const sqlNotAvailable = explanatory || modelUnavailable || body.clarification_required;
  const sqlText = generation.sql || "";
  const displaySql = sqlNotAvailable ? "" : (generation.formatted_sql || formatSqlForDisplay(sqlText));
  fragment.querySelector(".sql-panel-pane").hidden = sqlNotAvailable;
  if (!sqlNotAvailable) {
    renderHighlightedSql(fragment.querySelector(".language-sql"), displaySql);
    const validity = fragment.querySelector(".sql-validity");
    if (validity) {
      const parts = ["Validated"];
      if (body.sql_version) parts.push(`SQL v${body.sql_version}`);
      if (generation.truncated) parts.push("preview");
      validity.textContent = parts.join(" · ");
    }
  }
  fragment.querySelector(".result-panel-pane").hidden = sqlNotAvailable || explanatory;

  const actions = fragment.querySelector(".answer-actions");
  if (accepted && !explanatory) {
    if (body.run_id && !options.restored) actions.append(actionButton("Branch from here", "git", async () => {
      try { await continueRun(body.run_id); $("#messageInput").placeholder = "Explore an alternative question from this point…"; }
      catch (error) { showError(error.message); }
    }));
    actions.append(
      actionButton("Check correctness", "shield", () => send("Check whether the previous SQL and result are correct.")),
      actionButton("Optimize", "zap", () => send("Optimize the previous SQL without changing its result.")),
      actionButton("Save question", "bookmark", () => toggleSaved(generationQuestion(body, generation), article)),
    );
  }
  if (options.restored && options.run) {
    actions.append(actionButton("Continue from here", "message", () => continueRun(options.run.run_id).catch(error => showError(error.message))));
    if (options.run.accepted && options.run.sql) {
      actions.append(actionButton("Rerun saved SQL", "rerun", () => rerunSaved(options.run)));
    }
    const snapshot = document.createElement("p");
    snapshot.className = "snapshot-note";
    snapshot.textContent = `Saved snapshot · ${Math.min(options.run.rows_preview.length, 20)} preview rows of ${options.run.row_count} returned${options.run.truncated ? " (original result truncated)" : ""}. Opening history did not run the query.`;
    actions.before(snapshot);
  }

  renderHumanReview(fragment, body.human_review);
  if (!sqlNotAvailable) {
    fragment.querySelector(".copy-button").addEventListener("click", (event) => {
      navigator.clipboard.writeText(sqlText);
      event.currentTarget.classList.add("active");
      window.setTimeout(() => event.currentTarget.classList.remove("active"), 1200);
    });
    fragment.querySelector(".csv-button").addEventListener("click", () => {
      downloadCsv(generation.columns || [], generation.rows || []);
    });
    const wrap = fragment.querySelector(".table-wrap");
    renderTable(wrap, generation.columns || [], generation.rows || []);
    const table = wrap.querySelector("table");
    if (table) {
      table.className = "result-grid";
      attachResultInvestigations(table, body);
    }
    fragment.querySelector(".row-count").textContent = `${generation.row_count || 0} rows${generation.truncated ? " · truncated" : ""}`;
    wireSqlIdents(fragment, article);
    const versionPanel = fragment.querySelector(".versions-panel");
    const previous = latestAcceptedPreviousSql(body, options.restored ? options.run : null);
    if (sqlText && previous) {
      versionPanel.hidden = false;
      versionPanel.querySelector("summary").onclick = () => { void loadVersionPanel(versionPanel, previous, sqlText); };
    }
  }
  if (generation.recovery && generation.recovery.activated) {
    renderRecoveryTimeline(fragment.querySelector(".recovery-panel"), generation.recovery, body.recovery_events || []);
  }
  hydrateIcons(article);
  $("#messages").append(fragment);
  if (mode === "chart") wireChartSection(article, presentation, generation, body.run_id);
  if (accepted) { $("#composerChips")?.querySelectorAll(".chip").forEach((chip) => { chip.disabled = false; }); }
  article.scrollIntoView({ behavior: "smooth", block: "end" });
  if (!options.restored) updateInspector(body);
  return article;
}

function generationQuestion(body, generation) {
  return generation.question || body.resolved_question || body.conversation_interpretation?.resolved_instruction || "";
}

function actionButton(label, iconName, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "ghost-button";
  button.innerHTML = `${icon(iconName)}<span>${label}</span>`;
  button.addEventListener("click", onClick);
  return button;
}

function latestAcceptedPreviousSql(body, restoredRun) {
  return body.previous_sql || state.history.find(run => run.run_id === body.parent_run_id)?.sql || null;
}

async function loadVersionPanel(panel, previousSql, currentSql) {
  const list = panel.querySelector(".version-list");
  const diffBox = panel.querySelector(".diff-changes");
  if (list.dataset.loaded === "1") return;
  list.replaceChildren();
  list.dataset.loaded = "1";
  const difference = await fetchDiff(previousSql, currentSql);
  const changes = mergeRunChanges(difference.changes || []);
  const button = document.createElement("button");
  button.type = "button";
  button.className = "version-item";
  const num = document.createElement("span");
  num.className = "version-num";
  num.textContent = "diff";
  const text = document.createElement("span");
  text.textContent = `vs previous accepted SQL · ${changes.length} change${changes.length === 1 ? "" : "s"}`;
  button.append(num, text);
  button.addEventListener("click", () => renderDiff(diffBox, changes));
  list.append(button);
  renderDiff(diffBox, changes);
  if (Object.keys(difference.invariants || {}).length) {
    const comparisons = document.createElement("p");
    comparisons.className = "snapshot-note";
    comparisons.textContent = "SQL structure: " + Object.entries(difference.invariants).map(([name, status]) => `${name} ${status}`).join(" · ");
    diffBox.append(comparisons);
  }
  for (const change of difference.filter_changes || []) {
    const update = document.createElement("p");
    update.className = "snapshot-note";
    update.textContent = `${change.operation.replaceAll("_", " ")}: ${change.previous_value || "—"} → ${change.new_value || "—"}`;
    diffBox.append(update);
  }
}

async function fetchDiff(previousSql, currentSql) {
  const key = `${previousSql}\u0000${currentSql}`;
  if (state.diffCache.has(key)) return state.diffCache.get(key);
  const observedDialect = $("#databaseSelect")?.selectedOptions?.[0]?.dataset.dialect || "sqlite";
  const body = await api("/api/sql/diff", {
    method: "POST",
    body: JSON.stringify({ previous_sql: previousSql, updated_sql: currentSql, dialect: observedDialect }),
  });
  state.diffCache.set(key, body);
  return body;
}

function renderDiff(diffBox, changes) {
  diffBox.hidden = false;
  diffBox.querySelectorAll(".diff-line").forEach(node => node.remove());
  const title = diffBox.querySelector(".diff-changes-title");
  if (!changes.length) {
    title.textContent = "No structural changes detected. This comparison does not prove business correctness.";
    return;
  }
  title.textContent = `${changes.filter(c => c.kind === "insert").length} added · ${changes.filter(c => c.kind === "remove").length} removed expressions (SQLGlot-aware)`;
  const existing = diffBox.querySelector(".diff-lines");
  if (existing) existing.remove();
  const container = document.createElement("div");
  container.className = "diff-lines";
  mergeRunChanges(changes).forEach((change) => {
    const line = document.createElement("div");
    line.className = `diff-line ${change.kind}`;
    line.textContent = change.token;
    container.append(line);
  });
  diffBox.append(container);
}

function renderRecoveryTimeline(panel, recovery, events = []) {
  panel.hidden = false;
  const modeBadge = panel.querySelector(".recovery-mode-badge");
  if (recovery.mode) {
    modeBadge.hidden = false;
    modeBadge.textContent = recovery.mode;
  }
  const decisionBadges = panel.querySelector(".recovery-decision-badges");
  decisionBadges.replaceChildren();
  const actions = [["REPAIR", "refresh", "accent"], ["INFORM", "info", ""], ["ESCALATE", "alert", ""]];
  actions.forEach(([action, iconName, tone]) => {
    if (recovery.agent_action !== action) return;
    const badge = document.createElement("span");
    badge.className = `badge ${tone === "accent" ? "accent" : "warn"}`;
    badge.innerHTML = `${icon(iconName)}${action}`;
    decisionBadges.append(badge);
  });
  const agentBadges = panel.querySelector(".recovery-agent-badges");
  agentBadges.replaceChildren();
  const usage = recovery.usage || {};
  [
    `${(usage.llm_calls || 0)} LLM calls`,
    `${(usage.database_probe_count || 0)} database probes`,
    `${Math.round((usage.latency_ms || 0) / 100) / 10}s`,
    usage.budget_exhausted ? "budget exhausted" : null,
  ].filter(Boolean).forEach((text) => {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = text;
    agentBadges.append(badge);
  });
  const steps = panel.querySelector(".recovery-steps");
  steps.replaceChildren();
  recoveryTimelineSteps(recovery, events).forEach((step) => {
    const node = document.createElement("div");
    node.className = "recovery-step";
    const iconWrap = document.createElement("span");
    iconWrap.className = `recovery-step-icon ${step.tone === "info" ? "" : step.tone}`;
    iconWrap.innerHTML = icon(step.icon, 13);
    const title = document.createElement("div");
    title.className = "recovery-step-title";
    title.textContent = step.title;
    const detail = document.createElement("div");
    detail.className = "recovery-step-detail";
    detail.textContent = step.detail;
    node.append(iconWrap, title, detail);
    if (step.evidenceId) {
      const evidence = document.createElement("span");
      evidence.className = "evidence-id";
      evidence.textContent = step.evidenceId;
      detail.append(" ", evidence);
    }
    if (step.observation || step.sql) {
      const observation = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = step.sql ? "View SQL" : "View observation";
      const pre = document.createElement("pre");
      if (step.sql) renderHighlightedSql(pre, step.sql);
      else pre.textContent = step.observation;
      observation.append(summary, pre);
      detail.append(observation);
    }
    steps.append(node);
  });
}

function underRows(interpretation, stateLike) {
  const rows = [];
  if (!interpretation && !stateLike) return rows;
  const source = (interpretation?.source || "rules");
  if (interpretation) {
    rows.push(["Operation", `${interpretation.operation} · ${source === "model" ? "model interpretation" : "rules"}`]);
    rows.push(["Resolved request", interpretation.resolved_instruction]);
    if (interpretation.confidence != null) rows.push(["Confidence", String(interpretation.confidence)]);
  }
  const contract = stateLike?.semantic_contract;
  if (contract) {
    if (contract.measures?.length) rows.push(["Metric", contract.measures.join(", ")]);
    if (contract.aggregation) rows.push(["Aggregation", contract.aggregation]);
    if (contract.filters?.length) rows.push(["Filters", contract.filters.map(item => `${item.operand || item.column || ""} ${item.operator || "="} ${JSON.stringify(item.value)}`).join("; ")]);
    if (contract.grain?.length) rows.push(["Grouping / grain", contract.grain.join(", ")]);
    if (contract.limit != null) rows.push(["Limit", String(contract.limit)]);
    if (contract.outputs?.length) rows.push(["Metric columns", contract.outputs.join(", ")]);
    if (contract.proposed_tables?.length) rows.push(["Candidate tables", contract.proposed_tables.join(", ")]);
    if (contract.ambiguities?.length) rows.push(["Ambiguities", contract.ambiguities.join("; ")]);
  }
  for (const change of interpretation?.changes || []) {
    rows.push(["Requested change", `${change.operation}: ${change.target} · ${change.previous_value ?? "—"} → ${change.new_value ?? "—"}`]);
  }
  return rows;
}

function updateInspector(body) {
  state.lastBody = body;
  const understanding = $("#understandingContent");
  understanding.replaceChildren();
  const rows = underRows(body.conversation_interpretation, body.state);
  const chartSpec = body.presentation?.chart_spec;
  if (chartSpec) {
    // A chart answer: provenance of what is on screen comes first.
    const provenance = [
      ["Chart type", String(chartSpec.chart_type || "").replaceAll("_", " ")],
      ["X axis", chartSpec.x_label || chartSpec.x || ""],
      ["Y axis", chartSpec.y_label || chartSpec.y || ""],
      chartSpec.series ? ["Series", String(chartSpec.series)] : null,
      ["Result rows", String(chartSpec.source_row_count ?? chartSpec.data?.length ?? "")],
      ...(chartSpec.warnings || []).map((warning) => ["Chart note", warning]),
    ].filter((row) => row && row[1]);
    rows.splice(0, 0, ...provenance);
  }
  if (!rows.length) {
    understanding.append(emptyNote("Run a query to see how it was understood."));
  } else {
    rows.forEach(([key, value]) => {
      const row = document.createElement("div");
      row.className = "uq-row";
      const k = document.createElement("span");
      k.className = "k";
      k.textContent = key;
      const v = document.createElement("span");
      v.className = "v";
      v.textContent = value;
      row.append(k, v);
      understanding.append(row);
    });
    const note = document.createElement("p");
    note.className = "uq-note";
    note.textContent = body.conversation_interpretation?.source === "model"
      ? "The resolver's interpretation is shown for inspection. Model confidence does not prove business correctness."
      : "This view shows the request and available context; execution alone does not verify the intended business meaning.";
    understanding.append(note);
  }
  renderInspectorSchema();
  renderInspectorRecovery(body);
  renderInspectorPerformance(body);
  renderInspectorRunDetails(body);
  prioritizeInspector(body);
}

/* The inspector holds everything technical that used to sit under the answer:
   validation attempts, token accounting, pipeline timings and the verified
   model context. Sections are ordered by what the current response is. */
function renderInspectorRunDetails(body) {
  const target = $("#validationContent");
  const schemaContent = $("#schemaContent");
  if (!target) return;
  const generation = body.generation || {};
  const tech = document.createElement("div");
  tech.innerHTML = [
    '<details class="attempts-panel inspector-block">',
    '  <summary><span data-icon="shield"></span> SQL validation attempts <span class="attempts-count"></span></summary>',
    '  <div class="attempt-summary"></div>',
    '  <div class="attempts-list"></div>',
    '</details>',
    '<details class="token-card inspector-block">',
    '  <summary><span data-icon="hash"></span> Token usage</summary>',
    '  <div class="token-reduction"></div>',
    '  <div class="token-grid"></div>',
    '  <p class="token-note"></p>',
    '</details>',
    '<details class="pipeline-details inspector-block">',
    '  <summary><span data-icon="clock"></span> Pipeline timings</summary>',
    '  <div class="pipeline-rows"></div>',
    '  <details class="progress-history"><summary>Request activity</summary><ul></ul></details>',
    '</details>',
  ].join("\n");
  renderTokenAccounting(tech, body, generation);
  renderPipelineDetails(tech, body.timings_ms || {}, body, generation);
  renderAttempts(tech, generation.attempts || []);
  [...tech.querySelectorAll("details")].forEach((block) => target.append(block));
  const context = JSON.stringify(generation.model_context || {}, null, 2);
  if (context !== "{}" && schemaContent) {
    schemaContent.querySelectorAll("[data-context-panel]").forEach((node) => node.remove());
    const details = document.createElement("details");
    details.className = "inspector-block";
    details.dataset.contextPanel = "true";
    details.innerHTML = '<summary><span data-icon="network"></span> Verified context sent to the model</summary>';
    const pre = document.createElement("pre");
    pre.className = "context-json";
    pre.append(Object.assign(document.createElement("code"), { textContent: context }));
    details.append(pre);
    schemaContent.append(details);
  }
}

function prioritizeInspector(body) {
  const sections = {
    understanding: $("#understandingSection"),
    schema: $("#schemaSection"),
    recovery: $("#recoverySection"),
    validation: $("#validationSection"),
  };
  if (!sections.understanding || !sections.validation) return;
  const bodyEl = sections.understanding.parentElement;
  const recoveryActive = body.generation?.recovery?.activated === true;
  const chartMode = Boolean(body.presentation?.chart_spec);
  if (!recoveryActive && !chartMode && !body.generation?.recovery) {
    bodyEl.append(sections.schema, sections.understanding, sections.validation, sections.recovery);
    return;
  }
  const order = recoveryActive
    ? [sections.recovery, sections.understanding, sections.validation, sections.schema]
    : chartMode
      ? [sections.understanding, sections.schema, sections.validation, sections.recovery]
      : [sections.understanding, sections.validation, sections.schema, sections.recovery];
  order.forEach((section) => { if (section) { section.open = section === order[0]; bodyEl.append(section); } });
}

function emptyNote(text) {
  const p = document.createElement("p");
  p.className = "inspector-empty";
  p.textContent = text;
  return p;
}

function renderInspectorSchema() {
  const graphBox = $("#schemaGraph");
  const chips = $("#schemaChips");
  const detail = $("#schemaDetail");
  chips.replaceChildren();
  detail.replaceChildren();
  const generation = state.lastBody?.generation;
  const context = generation?.model_context || {};
  const rawTables = context.tables || context.execution_context?.tables || {};
  const names = Object.keys(rawTables);
  graphBox.replaceChildren();
  if (!names.length && !browserTables.length) {
    graphBox.append(emptyNote("The verified tables used by the current query appear here."));
    return;
  }
  if (names.length) {
    graphBox.append(schemaGraphSvg(names, context.relationships || []));
    names.forEach((name) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "schema-chip";
      chip.innerHTML = `${icon("table", 12)}<span>${name}</span>`;
      chip.addEventListener("click", () => showSchemaDetail(detail, { table: name }));
      chips.append(chip);
    });
  } else {
    graphBox.append(emptyNote("Run a query to see the verified tables it used."));
  }
}

function schemaGraphSvg(tableNames, relationships) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "schema-graph-svg");
  const radius = 46;
  const center = radius * (tableNames.length > 1 ? 2.1 : 1.4);
  const width = center * 2;
  const height = center * 2;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const positions = tableNames.map((name, index) => {
    const angle = (Math.PI * 2 * index) / Math.max(tableNames.length, 1) - Math.PI / 2;
    return { name, x: center + radius * 2 * Math.cos(angle), y: center + radius * 2 * Math.sin(angle) };
  });
  const byName = Object.fromEntries(positions.map((pos) => [pos.name, pos]));
  const parsed = relationships.map((relationship) => {
    const [leftTable] = String(relationship.left || "").split(".");
    const [rightTable] = String(relationship.right || "").split(".");
    return { leftTable, rightTable, left: relationship.left, right: relationship.right, fanout: relationship.fanout_risk };
  }).filter((edge) => byName[edge.leftTable] && byName[edge.rightTable]);
  parsed.forEach((edge) => {
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", byName[edge.leftTable].x);
    line.setAttribute("y1", byName[edge.leftTable].y);
    line.setAttribute("x2", byName[edge.rightTable].x);
    line.setAttribute("y2", byName[edge.rightTable].y);
    line.setAttribute("class", `graph-edge${isDeclared(edge.leftTable, edge.rightTable) ? " graph-edge-declared" : ""}`);
    svg.append(line);
  });
  positions.forEach((pos) => {
    const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circle.setAttribute("cx", pos.x);
    circle.setAttribute("cy", pos.y);
    circle.setAttribute("r", 26);
    circle.setAttribute("class", "graph-node");
    svg.append(circle);
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", pos.x);
    label.setAttribute("y", pos.y + 4);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("class", "graph-label");
    label.textContent = pos.name;
    svg.append(label);
  });
  return svg;
}

function isDeclared(fromTable, toTable) {
  return browserRelationships.some((item) =>
    ((item.from_table === fromTable && item.to_table === toTable)
      || (item.from_table === toTable && item.to_table === fromTable))
    && item.source === "declared_foreign_key");
}

async function loadGlossary(dbId) {
  if (!dbId || state.glossaryByDb[dbId]) return state.glossaryByDb[dbId] || {};
  try {
    const terms = await api(`/api/databases/${encodeURIComponent(dbId)}/glossary`);
    state.glossaryByDb[dbId] = Object.fromEntries(terms.map((term) => [term.term.toLowerCase(), term]));
  } catch {
    state.glossaryByDb[dbId] = {};
  }
  return state.glossaryByDb[dbId];
}

function showSchemaDetail(container, selection) {
  const { table, column } = selection;
  container.replaceChildren();
  loadGlossary($("#databaseSelect").value).then((glossary) => {
    const tableInfo = browserTables.find(item => item.name.toLowerCase() === String(table || "").toLowerCase());
    const glossaryMatch = glossary[String(table || "").toLowerCase()] || glossary[String(column || "").toLowerCase()];
    const box = document.createElement("section");
    box.className = "table-meta";
    if (column) {
      const candidates = browserTables.filter(t => !table || t.name.toLowerCase() === String(table).toLowerCase())
        .flatMap(t => (t.columns || []).filter(c => c.name.toLowerCase() === String(column).toLowerCase())
          .map(c => ({...c, tableWithColumn: t.name})));
      const columnInfo = candidates.length === 1 ? candidates[0] : null;
      if (candidates.length > 1) {
        box.append(metaRow("Column", `${column} appears in multiple tables`));
        for (const candidate of candidates) {
          box.append(actionButton(candidate.tableWithColumn, "table", () => showSchemaDetail(container, {
            table: candidate.tableWithColumn, column,
          })));
        }
      }
      if (columnInfo) {
        box.append(metaRow("Column", `${columnInfo.tableWithColumn}.${columnInfo.name}`));
        box.append(metaRow("Physical type", columnInfo.type || "unknown"));
        if (columnInfo.primary_key) box.append(metaRow("Key", "Primary key"));
        const tags = document.createElement("div");
        tags.className = "tagrow";
        if (columnInfo.primary_key) tags.innerHTML += `<span class="schema-tag pk">PK</span>`;
        box.append(tags);
      } else {
        box.append(metaRow("Column", String(column)));
      }
    } else if (tableInfo) {
      box.append(metaRow("Table", tableInfo.name));
      box.append(metaRow("Columns", String(tableInfo.columns?.length || 0)));
      const primaryKeys = (tableInfo.columns || []).filter(c => c.primary_key).map(c => c.name);
      if (primaryKeys.length) box.append(metaRow("Primary key", primaryKeys.join(", ")));
      const relations = browserRelationships.filter(item => item.from_table === tableInfo.name || item.to_table === tableInfo.name);
      relations.forEach(relationship => {
        if (relationship.source === "declared_foreign_key") {
          box.append(metaRow("Declared FK", `${relationship.from_table}.${relationship.from_column} → ${relationship.to_table}.${relationship.to_column}`));
        }
      });
    }
    if (glossaryMatch) {
      box.append(metaRow("Glossary", glossaryMatch.definition));
      if (glossaryMatch.grain) box.append(metaRow("Grain", glossaryMatch.grain));
    }
    container.append(box);
  });
}

function metaRow(k, v) {
  const row = document.createElement("div");
  row.className = "meta-row";
  const kEl = document.createElement("span");
  kEl.className = "k";
  kEl.textContent = k;
  const vEl = document.createElement("span");
  vEl.className = "v";
  vEl.textContent = v;
  row.append(kEl, vEl);
  return row;
}

function renderInspectorRecovery(body) {
  const box = $("#recoveryMini");
  const recovery = body.generation?.recovery;
  box.replaceChildren();
  if (!recovery || !recovery.activated) {
    box.append(emptyNote("The recovery agent only activates on anomalies."));
    return;
  }
  const info = document.createElement("div");
  info.className = "recovery-mini";
  info.append(metaRow("Mode", recovery.mode || ""));
  info.append(metaRow("Decision", recovery.agent_action || "pending"));
  if (recovery.agent_confidence != null) info.append(metaRow("Confidence", String(recovery.agent_confidence)));
  if (recovery.diagnosis_summary) info.append(metaRow("Diagnosis", recovery.diagnosis_summary));
  (recovery.claims || []).forEach((claim) => {
    const item = document.createElement("div");
    item.className = "claim-item";
    item.innerHTML = "";
    const text = document.createElement("span");
    text.textContent = claim.statement;
    const small = document.createElement("small");
    small.textContent = `${claim.claim_type} · evidence ${(claim.evidence_ids || []).join(", ")}`;
    item.append(text, small);
    info.append(item);
  });
  const link = document.createElement("button");
  link.type = "button";
  link.className = "ghost-button";
  link.innerHTML = `${icon("binoculars", 13)}<span>Open full timeline</span>`;
  link.addEventListener("click", () => {
    const last = [...document.querySelectorAll(".recovery-panel")].pop();
    if (last) { last.open = true; last.scrollIntoView({ behavior: "smooth", block: "center" }); }
  });
  info.append(link);
  box.append(info);
}

function renderInspectorPerformance(body) {
  const box = $("#validationContent");
  box.replaceChildren();
  const generation = body.generation || {};
  const timings = body.timings_ms || {};
  const usage = body.token_usage || generation.token_usage || {};
  const total = usage.input_tokens == null && usage.output_tokens == null ? null : (usage.input_tokens || 0) + (usage.output_tokens || 0);
  const attempts = generation.attempts || [];
  const failed = attempts.filter(a => attemptOutcome(a) === "failed").length;
  box.append(metaRow("Attempts", `${attempts.length}${failed ? ` (${failed} failed)` : ""}`));
  box.append(metaRow("Total latency", timings.total != null ? `${timings.total} ms` : "unreported"));
  box.append(metaRow("Model calls in ledger", String((body.call_ledger || []).length)));
  box.append(metaRow("Total tokens", total == null ? "unavailable" : total.toLocaleString()));
  const rejected = attempts.filter(a => attemptOutcome(a) !== "passed");
  if (rejected.length) {
    const label = document.createElement("p");
    label.className = "uq-note";
    label.textContent = rejected.map(a => `${a.validation?.code || "?"}: ${a.validation?.message || ""}`.trim()).slice(0, 3).join(" · ");
    box.append(label);
  }
}

function wireSqlIdents(fragment, article) {
  fragment.querySelectorAll(".sql-ident").forEach((span) => {
    span.addEventListener("click", () => {
      const detail = $("#schemaDetail");
      showSchemaDetail(detail, span.dataset.table ? { table: span.dataset.table } : { column: span.dataset.column });
      if (window.innerWidth <= 1020) document.body.classList.add("inspector-open");
      $("#schemaContent").scrollIntoView({ behavior: "smooth", block: "center" });
    });
  });
}

function renderHumanReview(fragment, review) {
  const panel = fragment.querySelector(".human-review");
  if (!panel || !review) return;
  panel.hidden = false;
  panel.querySelector(".human-review-reason").textContent = review.reason || "Automated review is uncertain.";
  panel.querySelector(".human-review-question").textContent = review.question || "Please clarify.";
  const options = panel.querySelector(".review-actions");
  const editableActions = {
    "Enter another ID": "Enter the replacement ID in the chat…",
    "Edit filters": "Describe the corrected filters in the chat…",
    "Edit the question": "Enter the complete revised question…",
    "Choose another period": "Enter the replacement period in the chat…",
    "Check another period": "Enter the period to check in the chat…",
  };
  (review.options || []).forEach((label) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", () => {
      if (editableActions[label]) {
        const input = $("#messageInput");
        input.placeholder = editableActions[label];
        input.focus();
        return;
      }
      if (review.replacement_target) {
        send(
          `Replace filter value ${JSON.stringify(review.replacement_target)} with ${JSON.stringify(label)}.`,
          "missing_filter",
        );
        return;
      }
      send(label, "other");
    });
    options.append(button);
  });
}

function renderTable(container, columns, rows) {
  if (!columns.length) {
    const empty = document.createElement("div");
    empty.className = "empty-result";
    empty.textContent = "No rows returned, or execution was not completed.";
    container.append(empty);
    return;
  }
  const table = document.createElement("table");
  const head = table.createTHead().insertRow();
  columns.forEach((column) => { const cell = document.createElement("th"); cell.textContent = column; head.append(cell); });
  const body = table.createTBody();
  rows.forEach((row) => {
    const tr = body.insertRow();
    row.forEach((value) => {
      const td = tr.insertCell();
      if (value === null) {
        const badge = document.createElement("span");
        badge.className = "null-value";
        badge.textContent = "NULL";
        td.append(badge);
      } else {
        td.textContent = String(value);
        if (typeof value === "number") td.classList.add("numeric");
      }
    });
  });
  container.append(table);
}

function downloadCsv(columns, rows) {
  if (!columns.length) return;
  const escape = (value) => {
    if (value === null || value === undefined) return "";
    const text = String(value);
    return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
  };
  const csv = [columns, ...rows].map((row) => row.map(escape).join(",")).join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `query-result-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  URL.revokeObjectURL(link.href);
}

function usageTotal(usage) {
  if (!usage || (usage.input_tokens == null && usage.output_tokens == null)) return null;
  return (usage.input_tokens || 0) + (usage.output_tokens || 0);
}

function sumKnown(values) {
  if (values.some((value) => value == null)) return null;
  return values.reduce((total, value) => total + value, 0);
}

function renderTokenAccounting(fragment, body, generation) {
  const panel = fragment.querySelector(".token-card");
  const grid = fragment.querySelector(".token-grid");
  const note = fragment.querySelector(".token-note");
  if (!panel || !grid || !note) return;

  const attempts = generation.attempts || [];
  const telemetry = generation.telemetry || {};
  const usage = body.token_usage || generation.token_usage || {};
  const total = usageTotal(usage);
  const context = telemetry.planner_call_used ? usageTotal(telemetry.planner_call) : 0;
  const chartCall = (body.call_ledger || []).find((entry) => entry.component === "chart_selection");
  const interpretationCall = (body.call_ledger || []).find((entry) => entry.component === "result_interpretation");
  const chartTokens = chartCall ? usageTotal(chartCall) : 0;
  const interpretationTokens = interpretationCall ? usageTotal(interpretationCall) : 0;
  const attemptTotals = attempts.map((attempt) => usageTotal(attempt.token_usage));
  const sql = attempts.length ? sumKnown(attemptTotals) : 0;
  const discardedAttempts = generation.accepted ? attemptTotals.slice(0, -1) : attemptTotals;
  const wasted = discardedAttempts.length ? sumKnown(discardedAttempts) : 0;
  const conversation = total == null || context == null || sql == null
    || chartTokens == null || interpretationTokens == null
    ? null
    : Math.max(0, total - context - sql - chartTokens - interpretationTokens);
  const cacheRead = body.token_usage?.cache_read_tokens ?? generation.token_usage?.cache_read_tokens ?? null;
  const cacheCreation = body.token_usage?.cache_creation_tokens ?? generation.token_usage?.cache_creation_tokens ?? null;
  const sentTokens = telemetry.selected_model_context_tokens ?? null;
  const avoidedTokens = telemetry.pruned_tokens ?? null;
  const fullTokens = sentTokens == null || avoidedTokens == null ? null : sentTokens + avoidedTokens;
  const reduction = fullTokens ? Math.round((avoidedTokens / fullTokens) * 1000) / 10 : null;
  const reductionBox = fragment.querySelector(".token-reduction");
  if (reductionBox) {
    reductionBox.innerHTML = `<div class="token-reduction-grid"><div><strong>${fullTokens == null ? "—" : fullTokens.toLocaleString()}</strong><small>Full schema estimate</small></div><b>→</b><div><strong>${sentTokens == null ? "—" : sentTokens.toLocaleString()}</strong><small>Sent to LLM</small></div></div><div class="reduction-pill">${reduction == null ? "Reduction unavailable" : `${reduction}% fewer context tokens`}</div>`;
  }

  const rows = [
    ["Total provider tokens", total],
    ["Other calls (resolver / recovery)", conversation],
    ["Context model", context],
    ["SQL attempts", sql],
    ...(chartTokens > 0 ? [["Legacy chart-selection call", chartTokens]] : []),
    ["Result interpretation", interpretationTokens],
    ["Discarded-attempt tokens", wasted],
    ["Input tokens", usage.input_tokens ?? null],
    ["Output tokens", usage.output_tokens ?? null],
    ["Cache read", cacheRead],
    ["Cache creation", cacheCreation],
  ];
  rows.forEach(([label, value]) => {
    const item = document.createElement("div");
    if (label === "Discarded-attempt tokens") item.className = "token-waste";
    const heading = document.createElement("strong");
    heading.textContent = label;
    const amount = document.createElement("span");
    amount.textContent = value == null ? "Unavailable" : Number(value).toLocaleString();
    item.append(heading, amount);
    grid.append(item);
  });

  attempts.forEach((attempt, index) => {
    const item = document.createElement("div");
    const discarded = !generation.accepted || index < attempts.length - 1;
    if (discarded) item.className = "token-waste";
    const heading = document.createElement("strong");
    heading.textContent = `Attempt ${attempt.number || index + 1}${discarded ? " · discarded" : " · selected"}`;
    const amount = document.createElement("span");
    const value = attemptTotals[index];
    amount.textContent = value == null ? "Unavailable" : Number(value).toLocaleString();
    item.append(heading, amount);
    grid.append(item);
  });

  note.textContent = total == null
    ? "This provider did not return token usage; unavailable values are not treated as zero."
    : "Discarded-attempt tokens are the measurable retry waste. Context and conversation tokens are shown separately because they may be necessary rather than wasted.";
  panel.open = (wasted || 0) > 0;
}

function renderAttempts(fragment, attempts) {
  const panel = fragment.querySelector(".attempts-panel");
  const list = fragment.querySelector(".attempts-list");
  const summary = fragment.querySelector(".attempt-summary");
  const count = fragment.querySelector(".attempts-count");
  if (!panel || !list || !summary) return;
  const visibleAttempts = attempts.filter((attempt) => attemptOutcome(attempt) !== "passed");
  if (count) count.textContent = attempts.length ? `· ${attempts.length}` : "";
  if (!visibleAttempts.length) {
    summary.textContent = attempts.length
      ? `${attempts.length} attempt${attempts.length === 1 ? "" : "s"} passed validation.`
      : "No SQL validation attempt was required for this response.";
    panel.open = false;
    return;
  }

  const failed = visibleAttempts.filter((attempt) => attemptOutcome(attempt) === "failed").length;
  const notSelected = visibleAttempts.filter((attempt) => attemptOutcome(attempt) === "not_selected").length;
  const summaryParts = [`${visibleAttempts.length} rejected candidate${visibleAttempts.length === 1 ? "" : "s"}`];
  if (failed) summaryParts.push(`${failed} failed`);
  if (notSelected) summaryParts.push(`${notSelected} not selected`);
  summary.textContent = summaryParts.join(" · ");
  panel.open = true;
  visibleAttempts.forEach((attempt, index) => {
    const validation = attempt.validation || {};
    const outcome = attemptOutcome(attempt);
    const passed = outcome === "passed";
    const notSelected = outcome === "not_selected";
    const item = document.createElement("section");
    item.className = `attempt-item ${passed ? "attempt-pass" : notSelected ? "attempt-warn" : "attempt-fail"}`;

    const header = document.createElement("div");
    header.className = "attempt-header";
    const title = document.createElement("strong");
    const outcomeLabel = passed ? "PASSED" : notSelected ? "NOT SELECTED" : "FAILED";
    title.textContent = `Attempt ${attempt.number || index + 1} · ${outcomeLabel}`;
    header.append(title);
    const code = document.createElement("code");
    code.textContent = validation.code || "UNKNOWN_VALIDATION_RESULT";
    header.append(code);

    const reason = document.createElement("p");
    reason.className = "attempt-reason";
    reason.textContent = validation.message || "No validator explanation was returned.";
    item.append(header, reason);

    if (!passed && attempt.sql) {
      const sqlPre = document.createElement("pre");
      const sqlCode = document.createElement("code");
      sqlCode.className = "language-sql";
      renderHighlightedSql(sqlCode, formatSqlForDisplay(attempt.sql));
      sqlPre.append(sqlCode);
      item.append(sqlPre);
    }
    list.append(item);
  });
}

function renderPipelineDetails(fragment, timings, body, generation) {
  const container = fragment.querySelector(".pipeline-rows");
  if (!container) return;
  const stages = [
    ["Conversation interpretation", timings.conversation_interpretation],
    ["Routing", timings.routing],
    ["Context planning", timings.planning],
    ["Generation / validation / execution", timings.generation_validation_execution],
    ["Total request", timings.total],
  ];
  stages.forEach(([label, milliseconds]) => {
    const row = document.createElement("div");
    row.className = `pipeline-run${milliseconds == null ? " unmeasured" : ""}`;
    const status = document.createElement("span");
    status.textContent = label;
    const duration = document.createElement("b");
    duration.textContent = milliseconds == null ? "Not reported" : `${Number(milliseconds).toLocaleString()} ms`;
    row.append(status, duration);
    container.append(row);
  });
  if (body.progress_events?.length) {
    const history = container.parentElement.querySelector(".progress-history");
    const list = history ? history.querySelector("ul") : null;
    if (list) {
      list.replaceChildren();
      body.progress_events.forEach(event => {
        const item = document.createElement("li");
        item.textContent = `${(event.elapsed_ms / 1000).toFixed(1)}s · ${progressLabels[event.stage] || event.stage}`;
        list.append(item);
      });
    }
  }
}

function showError(message) {
  appendAssistant({ operation: "ERROR", message, generation: { accepted: false, attempts: [] } }, 0);
}

/* ---------- History ---------- */

function summarizeRun(run) {
  const created = run.created_at ? new Date(run.created_at * 1000) : null;
  const time = created && !Number.isNaN(created.getTime())
    ? created.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : "";
  return {
    title: run.original_message || run.resolved_question || "Query",
    subtitle: [run.operation, `${run.row_count || 0} rows`, run.outcome?.toLowerCase().replaceAll("_", " ") || (run.accepted ? "executed" : "failed"), time]
      .filter(Boolean).join(" · "),
    failed: run.outcome ? run.outcome === "FAILED" : run.accepted === false,
  };
}

async function refreshHistory() {
  try {
    state.history = await api(`/api/history?session_id=${encodeURIComponent(sessionId)}&limit=50`);
    renderHistory();
  } catch {
    state.history = [];
    renderHistory();
  }
}

function renderHistory() {
  const container = $("#conversationHistory");
  container.replaceChildren();
  const term = $("#historySearch").value.trim().toLowerCase();
  const matches = state.history.filter((run) =>
    !term
    || (run.original_message || "").toLowerCase().includes(term)
    || (run.resolved_question || "").toLowerCase().includes(term)
    || (run.sql || "").toLowerCase().includes(term));
  const groups = new Map();
  for (const run of matches) {
    const id = run.conversation_id || run.run_id;
    if (!groups.has(id)) groups.set(id, run);
  }
  const visible = [...groups.values()];
  if (!visible.length) {
    container.append(emptyNote(term ? "No history matches this search." : "Completed queries appear here."));
    return;
  }
  for (const run of visible) {
    const item = summarizeRun(run);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "run-item";
    button.dataset.runId = run.run_id;
    if (run.run_id === state.activeRunId) button.classList.add("active");
    const iconWrap = document.createElement("span");
    iconWrap.innerHTML = icon(item.failed ? "alert" : "message", 14);
    const main = document.createElement("div");
    main.className = "run-main";
    const title = document.createElement("span");
    title.className = "run-title";
    const turns = state.history.filter(turn => (turn.conversation_id || turn.run_id) === (run.conversation_id || run.run_id));
    const first = turns[turns.length - 1];
    title.textContent = first?.original_message || item.title;
    const sub = document.createElement("span");
    sub.className = "run-sub";
    sub.textContent = `${run.db_id} · ${turns.length} turn${turns.length === 1 ? "" : "s"} · ${item.subtitle}`;
    main.append(title, sub);
    const dot = document.createElement("span");
    dot.className = `run-status${item.failed ? " failed" : ""}`;
    button.append(iconWrap, main, dot);
    button.addEventListener("click", () => openRun(run));
    container.append(button);
  }
}

async function openRun(run) {
  if (state.busy || voiceInput?.busy) return;
  setView("workspace");
  $("#emptyState").hidden = true;
  state.activeRunId = run.run_id;
  renderHistory();
  try {
    const records = await api(`/api/history/${encodeURIComponent(run.run_id)}/turns?session_id=${encodeURIComponent(sessionId)}`);
    const record = records[records.length - 1];
    if (!record) return;
    await loadGlossary(record.db_id);
    $("#databaseSelect").value = record.db_id;
    await loadDatabaseSchema();
    $("#messages").replaceChildren();
    for (const turn of records) {
      appendUser(turn.original_message);
      await appendAssistant(restoredRunToResponse(turn), 0, {restored: true, run: turn});
    }
    state.restorePending = true;
    updateInspector(restoredRunToResponse(record));
    $("#mainColumn").scrollTo({top: 0});
  } catch (error) { showError(`Could not open the saved conversation: ${error.message}`); }
}

async function continueRun(runId, focus = true) {
  const savedState = await api(`/api/history/${encodeURIComponent(runId)}/continue?session_id=${encodeURIComponent(sessionId)}`, {method: "POST"});
  state.activeRunId = runId;
  state.restorePending = false;
  $("#databaseSelect").value = savedState.db_id;
  renderHistory();
  if (focus) {
    $("#messageInput").placeholder = "Continue this conversation or correct the previous query…";
    $("#messageInput").focus();
  }
}

async function rerunSaved(record) {
  if (state.busy || voiceInput?.busy) return;
  state.busy = true;
  voiceInput?.setQueryBusy(true);
  $("#sendButton").disabled = true;
  try {
    const response = await api(`/api/history/${encodeURIComponent(record.run_id)}/rerun?session_id=${encodeURIComponent(sessionId)}`, {method: "POST"});
    state.activeRunId = response.run_id;
    state.restorePending = false;
    appendUser("Rerun the saved SQL against current data.");
    await appendAssistant(response, 0);
    await refreshHistory();
  } catch (error) { showError(error.message); }
  finally {
    state.busy = false;
    voiceInput?.setQueryBusy(false);
    $("#sendButton").disabled = !state.ready;
  }
}

function restoredRunToResponse(record) {
  if (record.response_payload && Object.keys(record.response_payload).length) {
    const payload = record.response_payload;
    if (payload.presentation_state == null) {
      // History saved before the typed presentation state existed: synthesize
      // it from the validated chart spec so restored threads stay chart-first.
      const spec = payload.presentation?.chart_spec;
      payload.presentation_state = spec
        ? {
            mode: "chart",
            chart_type: spec.chart_type,
            x: spec.x,
            y: spec.y,
            series: spec.series,
            size: spec.size,
            title: spec.title,
          }
        : payload.generation?.accepted
          ? { mode: "sql" }
          : null;
    }
    return payload;
  }
  return {
    session_id: record.session_id,
    operation: record.operation,
    resolved_question: record.resolved_question,
    message: record.presentation_summary || record.resolved_question,
    state: { turn_count: record.turn_count, semantic_contract: null },
    token_usage: record.token_usage || {},
    timings_ms: record.timings_ms || {},
    presentation: record.presentation_summary ? { summary: record.presentation_summary, note: "" } : null,
    generation: {
      accepted: record.accepted,
      execution_status: record.execution_status,
      question: record.resolved_question,
      sql: record.sql,
      columns: record.columns || [],
      rows: record.rows_preview || [],
      row_count: record.row_count || 0,
      truncated: record.truncated,
      attempts: [],
      token_usage: record.token_usage || {},
      recovery: record.recovery || null,
    },
  };
}

/* ---------- Saved questions ---------- */

function getSaved() {
  try { return JSON.parse(localStorage.getItem(savedStorageKey) || "[]"); }
  catch { return []; }
}

function toggleSaved(question, article) {
  const saved = getSaved();
  const exists = saved.includes(question);
  const next = exists ? saved.filter(item => item !== question) : [question, ...saved].slice(0, 20);
  localStorage.setItem(savedStorageKey, JSON.stringify(next));
  renderSaved();
  if (article) {
    const button = article.querySelector(".save-button");
    if (button) button.title = exists ? "Save this question" : "Saved";
  }
}

function renderSaved() {
  const container = $("#savedQuestions");
  container.replaceChildren();
  const saved = getSaved();
  if (!saved.length) {
    container.append(emptySaveNote());
    return;
  }
  saved.forEach(question => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "run-item";
    const iconWrap = document.createElement("span");
    iconWrap.innerHTML = icon("bookmark", 14);
    const main = document.createElement("div");
    main.className = "run-main";
    const title = document.createElement("span");
    title.className = "run-title";
    title.textContent = question;
    main.append(title);
    button.append(iconWrap, main);
    button.addEventListener("click", () => {
      $("#messageInput").value = question;
      $("#messageInput").focus();
    });
    container.append(button);
  });
}

function emptySaveNote() {
  const p = document.createElement("p");
  p.className = "list-empty";
  p.textContent = "Star a result to keep the question here.";
  return p;
}

/* ---------- Theme, inspector, keyboard ---------- */

function applyThemeButtons() {
  const dark = document.documentElement.dataset.theme === "dark";
  [["#themeToggle", "Toggle appearance"], ["#themeToggleTop", ""]].forEach(([selector, label]) => {
    const button = $(selector);
    if (!button) return;
    button.innerHTML = `<span data-icon="${dark ? "sun" : "moon"}"></span>${label ? ` ${label}` : ""}`;
    hydrateIcons(button);
  });
}

$("#themeToggle")?.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("queryStudioTheme", next);
  applyThemeButtons();
  document.querySelectorAll("article").forEach((article) => article._chartRerender?.());
  document.querySelectorAll(".chart-fullscreen").forEach((overlay) => overlay._chartRerender?.());
});
$("#themeToggleTop")?.addEventListener("click", () => $("#themeToggle").click());
applyThemeButtons();

$("#inspectorToggle")?.addEventListener("click", toggleInspector);
$("#inspectorClose")?.addEventListener("click", toggleInspector);

function toggleInspector() {
  if (window.innerWidth <= 1020) {
    document.body.classList.toggle("inspector-open");
    $("#inspectorToggle").classList.toggle("active");
    document.body.classList.contains("inspector-open")
      ? document.body.insertAdjacentHTML("beforeend", '<button class="inspector-drawer-backdrop" id="inspectorBackdrop" aria-label="Close inspector"></button>')
      : $("#inspectorBackdrop")?.remove();
    $("#inspectorBackdrop")?.addEventListener("click", toggleInspector);
    return;
  }
  const shell = $("#appShell");
  const hidden = shell.classList.toggle("inspector-hidden");
  $("#inspectorToggle").classList.toggle("active", hidden);
  $("#inspectorToggle").setAttribute("aria-expanded", String(!hidden));
  localStorage.setItem("queryStudioInspector", hidden ? "hidden" : "shown");
  if (hidden) shell.style.removeProperty("grid-template-columns");
  else applyInspectorWidth();
}

function applyInspectorWidth() {
  const stored = Number(localStorage.getItem("queryStudioInspectorWidth") || 0);
  if (stored >= 240) document.documentElement.style.setProperty("--inspector-w", `${stored}px`);
}

(function setupResizer() {
  document.addEventListener("DOMContentLoaded", () => {
    applyInspectorWidth();
    if (localStorage.getItem("queryStudioInspector") !== "shown") {
      $("#appShell").classList.add("inspector-hidden");
      $("#inspectorToggle").classList.add("active");
    }
    const resizer = $("#inspectorResizer");
    if (!resizer) return;
    let dragging = false;
    resizer.addEventListener("pointerdown", (event) => {
      dragging = true;
      resizer.classList.add("dragging");
      resizer.setPointerCapture(event.pointerId);
    });
    resizer.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      const width = Math.min(560, Math.max(240, window.innerWidth - event.clientX));
      document.documentElement.style.setProperty("--inspector-w", `${width}px`);
      localStorage.setItem("queryStudioInspectorWidth", String(width));
    });
    resizer.addEventListener("pointerup", () => {
      dragging = false;
      resizer.classList.remove("dragging");
    });
    resizer.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      const current = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--inspector-w"), 10) || 340;
      const next = Math.min(560, Math.max(240, current + (event.key === "ArrowLeft" ? 24 : -24)));
      document.documentElement.style.setProperty("--inspector-w", `${next}px`);
      localStorage.setItem("queryStudioInspectorWidth", String(next));
    });
    if (window.innerWidth <= 1020) $("#appShell").classList.remove("inspector-hidden");
  });
})();

$("#cancelButton").addEventListener("click", async () => {
  if (state.cancelled) return;
  state.cancelled = true;
  $("#cancelButton").disabled = true;
  try {
    if (state.jobId) await api(`/api/chat/jobs/${state.jobId}`, { method: "DELETE" });
  } catch (error) {
    state.cancelled = false;
    showError(`Could not cancel: ${error.message}`);
  } finally {
    $("#cancelButton").disabled = false;
  }
});

$("#composerChips")?.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    send(chip.dataset.question);
  });
});

$("#chatForm").addEventListener("submit", (event) => {
  event.preventDefault();
  if (state.busy || voiceInput?.busy || !state.ready) return;
  const input = $("#messageInput");
  const message = input.value;
  input.value = "";
  send(message);
});

$("#messageInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#chatForm").requestSubmit();
  }
});

const HERO_EXAMPLES = [
  ["table", "Show the top customers by spending"],
  ["clock", "Compare this month with the previous one"],
  ["network", "Join two tables step by step"],
  ["zap", "Plot monthly revenue as a line chart"],
];
document.addEventListener("DOMContentLoaded", () => {
  const target = $("#heroExamples");
  target.replaceChildren();
  HERO_EXAMPLES.forEach(([iconName, text]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "example";
    button.innerHTML = `<span data-icon="${iconName}"></span><span></span>`;
    button.lastElementChild.textContent = text;
    button.addEventListener("click", () => send(text));
    target.append(button);
  });
  target.querySelectorAll("[data-icon]").forEach((slot) => { slot.innerHTML = icon(slot.dataset.icon, 18); });
});

document.addEventListener("pointerdown", event => {
  if (activeInsight && !activeInsight.contains(event.target)) dismissInsight();
});
$("#historySearch").addEventListener("input", renderHistory);
$("#clearHistory").addEventListener("click", async () => {
  try {
    await api(`/api/history?session_id=${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  } catch (error) { showError(`Could not clear history: ${error.message}`); return; }
  state.history = [];
  state.activeRunId = null;
  state.restorePending = false;
  renderHistory();
});

$("#resetButton").addEventListener("click", async () => {
  try {
    await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, db_id: $("#databaseSelect").value, message: "Reset context" }),
    });
    $("#messages").replaceChildren();
    state.activeRunId = null;
    state.restorePending = false;
    $("#emptyState").hidden = false;
    $("#threadIntro").hidden = true;
    $("#threadBreadcrumb").textContent = "Live database";
    $("#composerChips")?.querySelectorAll(".chip").forEach((chip) => { chip.disabled = true; });
    $("#messageInput").focus();
  } catch (error) { showError(error.message); }
});

initialize();

$("#databaseSelect").addEventListener("change", async () => {
  state.activeRunId = null;
  state.restorePending = false;
  await loadDatabaseSchema();
  await loadGlossary($("#databaseSelect").value);
});
$("#exploreSearch").addEventListener("input", renderExplore);
document.querySelectorAll(".side-nav a[data-view]").forEach(link => {
  link.addEventListener("click", event => {
    event.preventDefault();
    if (link.dataset.view === "connections") {
      setView("workspace");
      $("#connectionCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
      document.querySelectorAll(".side-nav a[data-view]").forEach(item => {
        item.classList.toggle("active", item === link);
      });
    } else {
      setView(link.dataset.view);
    }
  });
});

document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    $("#resetButton").click();
  }
  if (event.key === "Escape") closeChartFullscreen();
});
