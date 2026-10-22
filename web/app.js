const $ = (selector) => document.querySelector(selector);
const sessionId = localStorage.getItem("queryRoomSession") || crypto.randomUUID().replaceAll("-", "");
localStorage.setItem("queryRoomSession", sessionId);
document.addEventListener("DOMContentLoaded", () => {
  const el = $("#sessionId");
  if (el) { el.textContent = sessionId; el.title = `Conversation session: ${sessionId}`; }
});

const state = {
  busy: false, jobId: null, cancelled: false, currentQuestion: "", ready: false,
  lastBody: null, history: [], saved: [], glossaryByDb: {}, schemaRequest: 0,
  activeRunId: null, diffCache: new Map(), modelConfig: null,
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
}).catch(() => { $("#voiceStatus").textContent = "Voice input could not load. You can still type a question."; });

const recentStorageKey = "queryRoomRecentQueries";
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
    const [models, uiConfig] = await Promise.all([
      api(`/api/models?refresh=${Date.now()}`, { cache: "no-store" }),
      api("/api/ui-config", { cache: "no-store" }),
    ]);
    const configuredSql = models.find(item => item.provider === uiConfig.sql_provider
      && item.model === uiConfig.sql_model && item.configured);
    const configuredContext = uiConfig.context_mode !== "model1" || models.some(item =>
      item.provider === uiConfig.context_provider && item.model === uiConfig.context_model && item.configured);
    state.modelConfig = uiConfig;
    state.ready = !!$("#databaseSelect").value && !!configuredSql && configuredContext;
    $("#sendButton").disabled = !state.ready || !!voiceInput?.busy;
    if (!configuredSql) showError("The SQL model configured in .env is unavailable. Check TEXT2SQL_SQL_PROVIDER and TEXT2SQL_SQL_MODEL.");
    else if (!configuredContext) showError("The context model configured in .env is unavailable. Check TEXT2SQL_CONTEXT_PROVIDER and TEXT2SQL_CONTEXT_MODEL.");
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
}

let schemaRequest = 0;
let browserTables = [];
let browserRelationships = [];

async function loadDatabaseSchema() {
  const request = ++schemaRequest;
  const dbId = $("#databaseSelect").value;
  browserTables = [];
  browserRelationships = [];
  if (!dbId) { renderInspectorSchema(); return; }
  try {
    const schema = await api(`/api/databases/${encodeURIComponent(dbId)}/schema`);
    if (request !== schemaRequest) return;
    browserTables = schema.tables || [];
    browserRelationships = schema.relationships || [];
    renderInspectorSchema();
  } catch (error) {
    if (request !== schemaRequest) return;
    renderInspectorSchema();
    console.warn(`Could not load database schema: ${error.message}`);
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

function appendProgress(contextMode) {
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
  article.querySelector(".progress-detail").textContent = "Using the configured query pipeline";
  hydrateIcons(article);
  $("#messages").append(article);
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
  if (!state.ready) {
    showError("A configured database and SQL model are required before asking a question.");
    return;
  }
  state.busy = true;
  voiceInput?.setQueryBusy(true);
  $("#sendButton").disabled = true;
  ["#databaseSelect", "#resetButton"].forEach(id => { $(id).disabled = true; });
  appendUser(message.trim());
  const contextMode = state.modelConfig?.context_mode || "retrieval";
  const started = performance.now();
  const progress = appendProgress(contextMode);
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
    ["#databaseSelect", "#resetButton"].forEach(id => { $(id).disabled = false; });
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

function sameDisplayText(left, right) {
  const normalize = value => String(value || "").trim().replace(/\s+/g, " ").replace(/[.!?]+$/, "").toLowerCase();
  return Boolean(normalize(left)) && normalize(left) === normalize(right);
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
function editSemanticChip(anchor, dimension, current, runId, body, filterItem = null) {
  const panel = document.createElement("form");
  const title = document.createElement("label");
  title.className = "query-insight-title";
  title.textContent = "Change " + dimension.toLowerCase();
  const contract = body.state?.semantic_contract || {};
  const verified = body.generation?.model_context?.tables || {};
  let suggestions = [];
  if (dimension === "Metric") suggestions = contract.measures || [];
  if (dimension === "Grain") suggestions = contract.grain || [];
  if (dimension === "Aggregation") suggestions = ["count", "sum", "average", "minimum", "maximum"];
  if (dimension === "Filter" && filterItem) {
    const [tableName, columnName] = String(filterItem.operand || filterItem.column || "").split(".");
    const profile = verified[tableName]?.columns?.[columnName] || {};
    suggestions = profile.example_values || profile.top_values || [];
  }
  const input = suggestions.length > 1 && dimension !== "Filter"
    ? document.createElement("select") : document.createElement("input");
  input.className = "query-insight-input";
  if (input.tagName === "SELECT") {
    [...new Set([current, ...suggestions])].forEach(value => {
      const option = document.createElement("option"); option.value = value;
      option.textContent = readableResultLabel(value); input.append(option);
    });
  }
  if (dimension === "Period" && /^\d{4}-\d{2}$/.test(current)) input.type = "month";
  else if (dimension === "Period" && /^\d{4}-\d{2}-\d{2}$/.test(current)) input.type = "date";
  else if (dimension === "Period" && /^\d{4}$/.test(current)) {
    input.type = "number"; input.min = "1900"; input.max = "2200";
  }
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
  if (dimension === "Filter" && filterItem) {
    const operator = document.createElement("select");
    operator.className = "query-insight-input";
    for (const value of ["=", "!=", ">", ">=", "<", "<=", "IN"]) {
      const option = document.createElement("option"); option.value = value; option.textContent = value;
      operator.append(option);
    }
    operator.value = filterItem.operator || "=";
    panel.insertBefore(operator, input);
    panel._operator = operator;
    if (suggestions.length) {
      const list = document.createElement("datalist");
      list.id = `verified-values-${Math.random().toString(36).slice(2)}`;
      suggestions.slice(0, 20).forEach(value => {
        const option = document.createElement("option"); option.value = String(value); list.append(option);
      });
      input.setAttribute("list", list.id); panel.append(list);
    }
    input.value = Array.isArray(filterItem.value) ? filterItem.value.join(", ") : String(filterItem.value ?? "");
  }
  panel.addEventListener("submit", async event => {
    event.preventDefault();
    if (state.busy || !state.ready || !input.value.trim()) return;
    const revised = input.value.trim();
    dismissInsight();
    const update = filterItem && dimension === "Filter"
      ? `${filterItem.operand || filterItem.column} ${panel._operator.value} ${JSON.stringify(revised)}`
      : filterItem && dimension === "Period"
        ? `${filterItem.operand || filterItem.column} ${filterItem.operator || "="} ${JSON.stringify(revised)}`
        : JSON.stringify(revised);
    const prompt = "Change the " + dimension.toLowerCase() + " in the previous query from " +
      JSON.stringify(current) + " to " + update +
      ". Preserve all other confirmed metrics, filters, and grouping requirements.";
    try { await sendFromAnswer(runId, prompt); }
    catch (error) { showError(error.message); }
  });
  placeInsight(anchor, panel);
  input.focus();
  input.select?.();
}
function semanticFilterText(item, includeColumn = true) {
  const column = String(item.operand || item.column || "").split(".").at(-1);
  const value = Array.isArray(item.value) ? item.value.join(", ") : String(item.value ?? "");
  const operator = item.operator || "=";
  if (!includeColumn && operator === "=") return value;
  const label = includeColumn ? readableResultLabel(column) : "";
  return operator === "=" && includeColumn ? `${label}: ${value}`
    : [label, operator, value].filter(Boolean).join(" ");
}

function renderSemanticChips(fragment, body) {
  const strip = fragment.querySelector(".query-semantic-strip");
  if (!strip) return;
  const contract = body.state?.semantic_contract;
  if (!contract || body.clarification_required || body.generation?.accepted === false) return;
  const items = [];
  if (contract.measures?.length) items.push([
    "Metric", contract.measures.map(readableResultLabel).join(", "), contract.measures.join(", "),
  ]);
  const temporal = [], otherFilters = [];
  if (contract.filters?.length) {
    for (const item of contract.filters) {
      const operand = String(item.operand || item.column || "");
      if (/(?:^|[._])(?:date|year|month|period|timestamp|time)$/i.test(operand)) temporal.push(item);
      else otherFilters.push(item);
    }
  }
  if (temporal.length) items.push([
    "Period", temporal.map(item => semanticFilterText(item, false)).join("; "),
    temporal.length === 1 ? String(temporal[0].value ?? "")
      : temporal.map(item => semanticFilterText(item)).join("; "),
    temporal.length === 1 ? temporal[0] : null,
  ]);
  if (contract.grain?.length) items.push([
    "Grain", contract.grain.map(item => readableResultLabel(String(item).split(".").at(-1).replace(/(?:_id|ID)$/, ""))).join(", "),
    contract.grain.join(", "),
  ]);
  if (contract.aggregation) items.push(["Aggregation", readableResultLabel(contract.aggregation), contract.aggregation]);
  for (const item of otherFilters) items.push(["Filter", semanticFilterText(item),
    [item.operand || item.column, item.operator || "=", JSON.stringify(item.value)].filter(Boolean).join(" "), item]);
  if (!items.length) return;
  strip.hidden = false;
  const eyebrow = document.createElement("span");
  eyebrow.className = "query-semantic-eyebrow";
  eyebrow.textContent = "UNDERSTOOD AS";
  strip.append(eyebrow);
  const appendChip = ([kind, display, raw, filterItem]) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "query-semantic-chip";
    chip.title = `Change ${kind.toLowerCase()}: ${display}`;
    chip.setAttribute("aria-label", chip.title);
    const k = document.createElement("small");
    k.textContent = kind;
    const v = document.createElement("span");
    v.className = "chip-value";
    v.textContent = display;
    const arrow = document.createElement("span");
    arrow.className = "chip-chevron";
    arrow.setAttribute("data-icon", "chevron-right");
    chip.append(k, v, arrow);
    chip.addEventListener("click", () => editSemanticChip(chip, kind, raw, body.run_id, body, filterItem));
    strip.append(chip);
  };
  items.slice(0, 6).forEach(appendChip);
  if (items.length > 6) {
    const more = document.createElement("button");
    more.type = "button"; more.className = "query-semantic-chip query-semantic-more";
    more.textContent = `+${items.length - 6} filters`;
    more.setAttribute("aria-expanded", "false");
    more.addEventListener("click", () => {
      items.slice(6).forEach(appendChip); more.remove();
    });
    strip.append(more);
  }
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
    const observedX = point.rawX ?? (point.name !== "" && point.name != null
      ? point.name : (Array.isArray(point.value) ? point.value[0] : undefined));
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
  note.textContent = "Interactive charts are unavailable. SQL and the exact result table remain in their tabs.";
  slot.append(note);
  return { echarts: null };
}

/* Response views are local to one message. They never call the query API. */
let responseTabSequence = 0;
function setResponseTab(article, view) {
  const tabs = article.querySelector(".response-tabs");
  const answerPane = article.querySelector(".answer-tab-panel");
  const split = article.querySelector(".sql-result-split");
  const sqlPane = article.querySelector(".sql-panel-pane");
  const dataPane = article.querySelector(".result-panel-pane");
  if (!tabs || tabs.hidden || !answerPane || !split || !sqlPane || !dataPane) return;
  const button = tabs.querySelector(`[data-response-tab="${view}"]`);
  if (!button || button.disabled) return;
  article.dataset.activeResponseTab = view;
  answerPane.hidden = view !== "answer";
  split.hidden = view === "answer";
  sqlPane.hidden = view !== "sql";
  dataPane.hidden = view !== "data";
  const versions = article.querySelector(".versions-panel");
  if (versions) versions.hidden = view !== "sql" || versions.dataset.available !== "true";
  tabs.querySelectorAll("[data-response-tab]").forEach((tab) => {
    const active = tab === button;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  if (view === "answer") window.requestAnimationFrame?.(() => article._echart?.resize?.());
}

function wireResponseTabs(article, available = {sql: true, data: true}) {
  const tabs = article.querySelector(".response-tabs");
  if (!tabs) return;
  tabs.hidden = false;
  const group = `response-tabs-${++responseTabSequence}`;
  tabs.querySelectorAll("[data-response-tab]").forEach((tab) => {
    const view = tab.dataset.responseTab;
    tab.id = `${group}-${view}`;
    tab.disabled = view !== "answer" && !available[view];
    const panel = article.querySelector(view === "answer" ? ".answer-tab-panel" : `.${view === "sql" ? "sql-panel-pane" : "result-panel-pane"}`);
    if (panel) {
      panel.id = `${group}-${view}-panel`;
      panel.setAttribute("aria-labelledby", tab.id);
      tab.setAttribute("aria-controls", panel.id);
    }
    tab.addEventListener("click", () => setResponseTab(article, view));
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const enabled = [...tabs.querySelectorAll("[data-response-tab]:not(:disabled)")];
      const next = enabled[(enabled.indexOf(tab) + (event.key === "ArrowRight" ? 1 : enabled.length - 1)) % enabled.length];
      setResponseTab(article, next.dataset.responseTab);
      next.focus();
    });
  });
  setResponseTab(article, "answer");
}

/* Data scope is presentation state only; neither tab runs a query. */
function wireDataScope(article, generation) {
  const pane = article.querySelector(".result-panel-pane");
  if (!pane) return;
  const sourceTab = pane.querySelector(".data-source-tab");
  const visibleTab = pane.querySelector(".data-visible-tab");
  const note = pane.querySelector(".data-scope-note");
  const wrap = pane.querySelector(".table-wrap");
  let sourceRows = generation.rows || [];
  const columns = generation.columns || [];
  article._visibleChartSpec = null;
  /* Unified chart-view display filters (chartViewState concept): narrows the
     visible chart and the Visible Chart Data tab only. The Source Data tab
     and the accepted SQL are immutable; SQL-backed filters go through the
     normal validated pipeline instead of touching this state. */
  let clientFilters = [];
  function allowedValues(filter) {
    return filter.values && filter.values.length
      ? filter.values.map(String) : [String(filter.value ?? "")];
  }
  function applyClientFilters(rows, filters = clientFilters) {
    if (!filters.length || generation.truncated) return rows;
    const filtered = rows.filter(row => filters.every(filter => {
      const index = columns.indexOf(filter.column);
      if (index < 0) return true;
      return allowedValues(filter).includes(String(row[index]));
    }));
    return filtered;
  }
  function filterNoteText() {
    return clientFilters.map(filter => {
      const allowed = allowedValues(filter);
      const value = allowed.length <= 3 ? allowed.join(" | ") : `${allowed.length} values`;
      return `${filter.column} = ${value}`;
    }).join(", ");
  }
  function setClientFilters(filters) {
    clientFilters = filters;
    if (filters.length && !generation.truncated) scope = "visible";
    /* One shared controller: the ECharts view updates through the same row
       filters; the Data tab re-renders; Source Data stays the original rows. */
    article._setChartViewRowFilters?.(filters);
    renderScope();
  }
  let scope = "source";
  function renderScope() {
    const visible = scope === "visible" && !generation.truncated
      && (article._visibleChartSpec || clientFilters.length);
    const spec = article._visibleChartSpec;
    const shownColumns = visible && spec?.transformation && spec.transformation !== "none"
      ? Object.keys(spec.data[0] || {}) : columns;
    /* Source Data is the original executed result, never a filtered copy. */
    const rows = visible
      ? spec ? spec.data.map(row => shownColumns.map(column => row[column] ?? null))
        : applyClientFilters(sourceRows)
      : sourceRows;
    wrap.replaceChildren();
    renderTable(wrap, shownColumns, rows);
    if (!visible) {
      const table = wrap.querySelector("table");
      if (table) { table.className = "result-grid"; attachResultInvestigations(table, {generation, run_id: article.dataset.runId}); }
    }
    const filterNote = visible && clientFilters.length && !generation.truncated
      ? ` · row filter: ${filterNoteText()} (display only)`
      : "";
    note.textContent = visible
      ? spec
        ? `${rows.length} visible chart rows · X: ${spec.x} · Y: ${(spec.y_measures || [spec.y]).join(", ")}${filterNote}`
        : `${rows.length} visible rows · display filter: ${filterNoteText()} · SQL unchanged`
      : `${sourceRows.length} of ${generation.row_count ?? sourceRows.length} returned rows (source result, unfiltered)${generation.truncated ? " · preview only; source result truncated" : " · complete within the executed row limit"}`;
    sourceTab.setAttribute("aria-selected", String(!visible));
    visibleTab.setAttribute("aria-selected", String(Boolean(visible)));
    visibleTab.disabled = !spec && !clientFilters.length;
  }
  sourceTab.addEventListener("click", () => { scope = "source"; renderScope(); });
  visibleTab.addEventListener("click", () => { scope = "visible"; renderScope(); });
  article._refreshDataScope = renderScope;
  article._setClientFilter = filter => setClientFilters([filter]);
  article._setClientFilters = setClientFilters;
  article._clearClientFilter = () => setClientFilters([]);
  article._hasClientFilter = () => clientFilters.length > 0;
  renderScope();
}

function wireChartSection(article, presentation, generation, runId) {
  const section = article.querySelector(".chart-section");
  const slot = article.querySelector(".chart-slot");
  if (!section || !slot) return;
  const kinds = section.querySelector(".chart-kinds");
  const more = section.querySelector(".chart-more");
  const menu = section.querySelector(".chart-menu");
  const explorer = window.QueryflowChartExplorer;
  const original = presentation.chart_spec;
  const panel = section.querySelector(".chart-explorer");
  const options = explorer?.filters(original);
  const settingKey = runId ? `queryflow-chart-${runId}` : null;
  let stored = null;
  try { stored = settingKey ? JSON.parse(sessionStorage.getItem(settingKey) || "null") : null; }
  catch { stored = null; }
  const settings = stored && typeof stored === "object" ? stored : {};
  const chartPres = { ...presentation, chart_spec: original };
  function saveSettings() {
    if (settingKey) {
      try { sessionStorage.setItem(settingKey, JSON.stringify(settings)); } catch { /* private browsing */ }
    }
  }
  function fillFilter(selector, values, emptyLabel = null) {
    const select = section.querySelector(selector);
    select.replaceChildren();
    if (emptyLabel) {
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = emptyLabel;
      select.append(empty);
    }
    values.forEach(value => {
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = String(value);
      select.append(option);
    });
  }
  function renderCurrent() {
    const next = explorer?.apply(original, settings) || original;
    if (!next.data.length) {
      article._echart?.dispose?.();
      article._echart = null;
      slot.replaceChildren();
      const message = document.createElement("p");
      message.className = "chart-note";
      message.textContent = "No rows in the executed result match these display filters. The source query is unchanged.";
      slot.append(message);
      article._visibleChartSpec = next;
      article._refreshDataScope?.();
      section.querySelector(".chart-active-label").textContent = "No visible chart rows · display filters only; SQL unchanged";
      return;
    }
    if (explorer?.isTime(next) && settings.zoom
        && new Set(next.data.map(row => String(row[next.x]))).size > 25) {
      next.zoom_start = settings.zoom.start;
      next.zoom_end = settings.zoom.end;
    }
    chartPres.chart_spec = next;
    article._visibleChartSpec = next;
    article._refreshDataScope?.();
    const activeParts = [];
    if (settings.rowFilters && Object.keys(settings.rowFilters).length)
      activeParts.push(Object.entries(settings.rowFilters).map(([column, values]) =>
        `${column}: ${values.length <= 3 ? values.join(" | ") : `${values.length} values`}`).join(", "));
    if (settings.categories?.length) activeParts.push(settings.categories.join(", "));
    if (settings.start || settings.end) activeParts.push([settings.start || "first", settings.end || "last"].join(" – "));
    if (settings.topN) activeParts.push(`Top ${settings.topN}`);
    if (settings.x && settings.x !== original.x) activeParts.push(`X: ${settings.x}`);
    if (settings.metrics?.length && (settings.metrics.length !== 1 || settings.metrics[0] !== original.y))
      activeParts.push(settings.metrics.join(" + "));
    if (settings.series !== undefined && settings.series !== original.series)
      activeParts.push(`Series: ${settings.series || "none"}`);
    if (settings.chartType && settings.chartType !== original.chart_type)
      activeParts.push(settings.chartType.replaceAll("_", " "));
    section.querySelector(".chart-category-current").textContent = settings.categories?.length
      ? settings.categories.length === 1 ? settings.categories[0] : `${settings.categories.length} selected`
      : "All";
    const selectedMeasures = settings.metrics?.length ? settings.metrics : [original.y];
    section.querySelector(".chart-measures-current").textContent = selectedMeasures.length === 1
      ? selectedMeasures[0] : `${selectedMeasures.length} selected`;
    section.querySelector(".chart-active-label").textContent = activeParts.length
      ? `${activeParts.join(" · ")} · display only; SQL unchanged`
      : "Current executed result · no display filters";
    section.querySelector(".chart-sub").textContent = next.chart_type.replaceAll("_", " ");
    article._echart = renderChartInto(slot, chartPres, generation).echarts;
    article._echart?.onZoom?.(range => {
      settings.zoom = range;
      saveSettings();
    });
    attachChartInvestigations(article, chartPres, runId);
    const zoomActions = section.querySelector(".chart-zoom-actions");
    zoomActions.hidden = !explorer?.isTime(next) || new Set(next.data.map(row => String(row[next.x]))).size <= 25;
    const suggestions = section.querySelector(".chart-suggestions");
    suggestions.replaceChildren();
    if (explorer) {
      const label = document.createElement("span");
      label.textContent = "Suggested charts";
      suggestions.append(label);
      explorer.choices(next).forEach(type => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "chart-kind";
        button.textContent = type.replaceAll("_", " ");
        button.classList.toggle("active", type === next.chart_type);
        button.setAttribute("aria-pressed", String(type === next.chart_type));
        button.addEventListener("click", () => {
          settings.chartType = type;
          saveSettings();
          renderCurrent();
        });
        suggestions.append(button);
      });
    }
    const active = activeParts.length;
    const count = section.querySelector(".chart-filter-count");
    count.hidden = active === 0;
    count.textContent = active ? String(active) : "";
  }
  article._echart = null;
  article._chartType = presentation.chart_type;
  kinds.hidden = true;
  /* Shared chart-view state (chartViewState): one controller for the chart
     toolbar AND backend-suggested chips. `tryChart` re-validates a proposed
     shape through the same deterministic chart explorer — display only, and
     it may fall back to the conversational pipeline for missing data. */
  article._chartViewState = {
    sourceRunId: runId,
    settings,
    accepted: original,
    tryChart(chart) {
      if (!chart || !chart.x || !chart.y) return false;
      const specColumns = new Set([
        ...Object.keys(original.data?.[0] || {}),
        ...(original.source_columns || []),
      ]);
      if (!specColumns.has(chart.x)) return false;
      const measures = [...new Set([...(chart.y_measures || []), chart.y].filter(Boolean))];
      if (!measures.every(name => specColumns.has(name))) return false;
      const merged = {
        ...settings,
        x: chart.x,
        metrics: measures,
        series: chart.series || null,
        chartType: chart.chart_type,
        rowFilters: {},
        categories: [],
        start: null,
        end: null,
        topN: null,
      };
      const applied = explorer?.apply(original, merged);
      if (!applied || !applied.data.length) return false;
      if (applied.chart_type !== chart.chart_type) return false;
      Object.keys(settings).forEach(key => delete settings[key]);
      Object.assign(settings, merged);
      saveSettings();
      renderCurrent();
      return true;
    },
    setRowFilters(rows) {
      settings.rowFilters = (Array.isArray(rows) ? rows : [rows]).reduce((map, filter) => {
        if (!filter?.column) return map;
        const values = filter.values && filter.values.length
          ? filter.values.map(String) : [String(filter.value ?? "")];
        return { ...map, [filter.column]: values };
      }, {});
      saveSettings();
      renderCurrent();
    },
  };
  article._setChartViewRowFilters = filters => article._chartViewState.setRowFilters(filters);
  if (explorer && options && panel) {
    panel.hidden = false;
    fillFilter(".chart-category-filter", options.categories);
    fillFilter(".chart-metric-filter", options.metric);
    fillFilter(".chart-measures-filter", options.metric);
    fillFilter(".chart-x-filter", options.dimensions);
    fillFilter(".chart-series-filter", options.dimensions.filter(name => name !== original.x), "None");
    fillFilter(".chart-start-filter", options.dates, "First date");
    fillFilter(".chart-end-filter", options.dates, "Last date");
    section.querySelector(".chart-category-control").hidden = !options.categories.length;
    section.querySelector(".chart-metric-control").hidden = options.metric.length < 2;
    section.querySelector(".chart-measures-control").hidden = options.metric.length < 2;
    section.querySelector(".chart-x-control").hidden = options.dimensions.length < 2;
    section.querySelector(".chart-series-control").hidden = options.dimensions.length < 2;
    section.querySelector(".chart-start-control").hidden = !options.dates.length;
    section.querySelector(".chart-end-control").hidden = !options.dates.length;
    section.querySelector(".chart-top-control").hidden = explorer.isTime(original) || Boolean(original.series)
      || new Set(original.data.map(row => String(row[original.x]))).size !== original.data.length;
    const inputs = { metric: ".chart-metric-filter", x: ".chart-x-filter", series: ".chart-series-filter",
      start: ".chart-start-filter", end: ".chart-end-filter", topN: ".chart-top-filter" };
    const syncInputs = () => Object.entries(inputs).forEach(([key, selector]) => {
      const select = section.querySelector(selector);
      select.value = settings[key] ?? (key === "metric" ? original.y
        : key === "x" ? original.x : key === "series" ? original.series || "" : "");
      if (!select.value && key === "metric") select.value = original.y;
      if (!select.value && key === "x") select.value = original.x;
    });
    const syncMeasures = () => {
      const selected = new Set(settings.metrics || [original.y]);
      [...section.querySelector(".chart-measures-filter").options].forEach(option => {
        option.selected = selected.has(option.value);
      });
      section.querySelector(".chart-legend-filter").checked = settings.legend !== false;
    };
    const syncCategories = () => {
      const selected = new Set(settings.categories || []);
      [...section.querySelector(".chart-category-filter").options].forEach(option => {
        option.selected = selected.has(option.value);
      });
    };
    const reset = () => {
      Object.keys(settings).forEach(key => delete settings[key]);
      saveSettings();
      syncInputs();
      syncCategories();
      syncMeasures();
      renderCurrent();
    };
    section.querySelector(".chart-apply-filter").addEventListener("click", () => {
      Object.entries(inputs).forEach(([key, selector]) => {
        const value = section.querySelector(selector).value;
        settings[key] = key === "topN" ? (value ? Number(value) : null) : value || null;
      });
      settings.categories = [...section.querySelector(".chart-category-filter").selectedOptions]
        .map(option => option.value);
      settings.metrics = [...section.querySelector(".chart-measures-filter").selectedOptions]
        .map(option => option.value);
      if (settings.metrics.length && !explorer.compatibleMeasures(settings.metrics, original)) {
        toast("These measures have incompatible units. Choose measures with the same verified unit.");
        return;
      }
      settings.legend = section.querySelector(".chart-legend-filter").checked;
      section.querySelectorAll(".chart-choice-chip").forEach(choice => { choice.open = false; });
      delete settings.zoom;
      saveSettings();
      renderCurrent();
    });
    section.querySelector(".chart-reset-filter").addEventListener("click", reset);
    section.querySelector(".chart-clear-filter").addEventListener("click", () => {
      for (const key of ["categories", "start", "end", "topN"]) delete settings[key];
      saveSettings(); syncInputs(); syncCategories(); renderCurrent();
    });
    section.querySelector(".chart-reset-zoom").addEventListener("click", () => {
      delete settings.zoom;
      saveSettings();
      article._echart?.resetZoom?.();
    });
    syncInputs();
    syncCategories();
    syncMeasures();
    const fields = generation.model_context?.tables || generation.model_context?.execution_context?.tables || {};
    const availableFields = [...new Set([
      ...Object.entries(fields).flatMap(([table, meta]) =>
        (Array.isArray(meta.columns) ? meta.columns : Object.keys(meta.columns || {}))
          .map(column => `${table}.${column}`)),
      ...browserTables.flatMap(table => (table.columns || []).map(column => `${table.name}.${column.name}`)),
    ])].sort();
    fillFilter(".chart-query-field", availableFields);
    section.querySelector(".chart-query-parameters").hidden = !availableFields.length;
    section.querySelector(".chart-apply-query").addEventListener("click", async () => {
      const field = section.querySelector(".chart-query-field").value;
      const operator = section.querySelector(".chart-query-operator").value;
      const value = section.querySelector(".chart-query-value").value.trim();
      if (!availableFields.includes(field) || !value || !["=", "!=", ">", "<"].includes(operator)) {
        toast("Choose a verified field, operator and value."); return;
      }
      try {
        await sendFromAnswer(runId, `Add the filter ${JSON.stringify(field)} ${operator} ${JSON.stringify(value)} to the previous query. Preserve every other accepted requirement. Verify the value and regenerate safely.`);
      } catch (error) { showError(error.message); }
    });
  }
  renderCurrent();
  /* Theme switches recolor the shared token palette, so live charts repaint. */
  article._chartRerender = renderCurrent;
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
        setResponseTab(article, "sql");
      } else if (action === "view-data") {
        setResponseTab(article, "data");
      } else if (action === "download") {
        exportChartPng(article);
      } else if (action === "expand") {
        openChartFullscreen(article, chartPres, generation);
      } else if (action === "inspect") {
        revealInspector("validation");
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

function revealInspector(tab = "understanding") {
  selectInspectorTab(tab);
  setInspectorOpen(true);
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

function readableResultLabel(column) {
  const words = String(column).replace(/_/g, " ").replace(/([a-z0-9])([A-Z])/g, "$1 $2").trim();
  return words ? words[0].toUpperCase() + words.slice(1).replace(/\bId\b/g, "ID") : "Result";
}

function formatKpiValue(value) {
  if (value == null) return "NULL";
  const raw = String(value);
  // Group ordinary decimal numbers without rounding or altering identifier-like strings.
  if (!/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(raw)) return raw;
  const [integer, fraction] = raw.split(".");
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return fraction === undefined ? grouped : `${grouped}.${fraction}`;
}

function renderAnswerPreview(fragment, body, mode) {
  const preview = fragment.querySelector(".answer-preview");
  const generation = body.generation || {};
  const columns = generation.columns || [];
  const rows = generation.rows || [];
  if (!preview || mode === "chart" || mode === "explanation" || body.clarification_required
      || !generation.accepted || columns.length !== 1 || rows.length !== 1) return;
  preview.hidden = false;
  preview.classList.add("answer-kpi");
  const label = document.createElement("span");
  label.textContent = readableResultLabel(columns[0]);
  const value = document.createElement("strong");
  value.textContent = formatKpiValue(rows[0][0]);
  preview.append(label, value);
}

async function appendAssistant(body, elapsed, options = {}) {
  const fragment = $("#assistantTemplate").content.cloneNode(true);
  const article = fragment.querySelector("article");
  const generation = body.generation || {};
  const accepted = generation.accepted === true;
  const attempts = generation.attempts || [];
  const { explanatory, modelUnavailable, status } = responseStatus(body, attempts);
  updateStatusPill(fragment, body, accepted, status);

  const responseMessage = body.explanation || body.presentation?.summary || body.message || "";
  fragment.querySelector(".response-note").textContent = responseMessage;
  const note = fragment.querySelector(".presentation-note");
  if (body.presentation?.note) { note.hidden = false; note.textContent = body.presentation.note; }
  renderSemanticChips(fragment, body);

  // Distinct empty / anomaly states, grounded in the real response.
  if (accepted && generation.row_count === 0) {
    const diagnosis = generation.recovery?.diagnosis_summary
      || "The query executed but returned no rows. Adjust the question or check the filters.";
    if (!sameDisplayText(responseMessage, diagnosis)
        && !sameDisplayText(body.human_review?.reason, diagnosis)) {
      renderCallout(fragment, "warn", "search", diagnosis);
    }
  } else if (!accepted && !explanatory && !modelUnavailable) {
    const failure = body.human_review?.reason || body.message || "The query could not be completed.";
    if (!body.human_review && !sameDisplayText(responseMessage, failure)) {
      renderCallout(fragment, "error", "alert", failure);
    }
  }

  // The Answer tab adapts to intent; SQL and Data use the same executed response.
  const viz = options.viz ?? state.pendingVizRequest === true;
  state.pendingVizRequest = null;
  const mode = presentationMode(body, { viz });
  const chartSection = fragment.querySelector(".chart-section");
  const presentation = body.presentation || null;
  const chartDataComplete = body.presentation_state?.data_complete !== false
    && !generation.truncated
    && (!(presentation?.chart_spec) || presentation.chart_spec.source_row_count === 0
      || (presentation.chart_spec.transformation && presentation.chart_spec.transformation !== "none")
      || presentation.chart_spec.data.length >= Math.min(presentation.chart_spec.source_row_count, 500));
  if (mode === "chart" && chartDataComplete) {
    chartSection.hidden = false;
    const spec = presentation.chart_spec;
    fragment.querySelector(".chart-title").textContent = presentation.title || `${presentation.y} by ${presentation.x}`;
    fragment.querySelector(".chart-sub").textContent = [
      spec.chart_type.replaceAll("_", " "), ...(spec.warnings || []),
    ].join(" · ");
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
      || body.message || "This result cannot support the requested chart. SQL and data are available in their tabs.");
  } else {
    chartSection.remove();
  }
  renderAnswerPreview(fragment, body, mode === "chart" && !chartDataComplete ? "sql" : mode);

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
  if (accepted && !explanatory && !sqlNotAvailable) {
    const sqlActions = fragment.querySelector(".sql-actions");
    const runId = body.run_id || options.run?.run_id || null;
    sqlActions.append(
      actionButton("Check correctness", "shield", () =>
        sendFromAnswer(runId, "Check whether the previous SQL and result are correct.")
          .catch(error => showError(error.message))),
      actionButton("Optimize", "zap", () =>
        sendFromAnswer(runId, "Optimize the previous SQL without changing its result.")
          .catch(error => showError(error.message))),
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
  actions.hidden = !actions.children.length;

  renderHumanReview(fragment, body.human_review, responseMessage);
  if (!sqlNotAvailable) {
    fragment.querySelector(".copy-button").addEventListener("click", (event) => {
      navigator.clipboard.writeText(sqlText);
      event.currentTarget.classList.add("active");
      window.setTimeout(() => event.currentTarget.classList.remove("active"), 1200);
    });
    fragment.querySelector(".csv-button").addEventListener("click", () => {
      downloadCsv(generation.columns || [], generation.rows || []);
    });
    const wrap = fragment.querySelector(".result-panel-pane .table-wrap");
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
      versionPanel.dataset.available = "true";
      versionPanel.querySelector("summary").onclick = () => { void loadVersionPanel(versionPanel, previous, sqlText); };
    }
  }
  if (generation.recovery && generation.recovery.activated) {
    renderRecoveryTimeline(fragment.querySelector(".recovery-panel"), generation.recovery, body.recovery_events || []);
  }
  hydrateIcons(article);
  $("#messages").append(fragment);
  if (!sqlNotAvailable && !explanatory) {
    article.dataset.runId = body.run_id || "";
    wireDataScope(article, generation);
  }
  if (mode === "chart") wireChartSection(article, presentation, generation, body.run_id);
  if (accepted && !explanatory && !body.clarification_required) renderBackendSuggestions(article, body);
  if (accepted && !explanatory && !body.clarification_required) {
    wireResponseTabs(article, {sql: Boolean(sqlText), data: Boolean(generation.columns?.length)});
  }
  if (!options.restored) updateInspector(body);
  return article;
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
    const kicker = document.createElement("div");
    kicker.className = "inspect-kicker";
    kicker.textContent = "QUERY UNDERSTANDING · CURRENT RESPONSE";
    understanding.append(kicker);
    const contract = body.state?.semantic_contract || {};
    const dateFilter = (contract.filters || []).find(item =>
      /(?:date|time|year|month|period)/i.test(String(item.operand || item.column || "")));
    const formatFilter = item => `${item.operand || item.column || "Value"} ${item.operator || "="} ${item.value ?? "?"}`;
    const chartPeriods = chartSpec?.time_format !== "raw" && chartSpec?.x
      ? [...new Set((chartSpec.data || []).map(row => String(row[chartSpec.x])))].sort()
      : [];
    const period = dateFilter ? formatFilter(dateFilter)
      : chartPeriods.length ? `Observed ${chartPeriods[0]}${chartPeriods.length > 1 ? `–${chartPeriods.at(-1)}` : ""}`
        : "Not specified";
    const tiles = [
      ["Measure", contract.measures?.join(", ") || chartSpec?.y_label || contract.outputs?.join(", ") || "Not specified"],
      ["Period", period],
      ["Dimension", contract.grain?.join(", ") || chartSpec?.x_label || "Not specified"],
      ["Filter", contract.filters?.length ? contract.filters.map(formatFilter).join("; ") : "None specified"],
    ];
    const grid = document.createElement("div");
    grid.className = "inspect-grid";
    tiles.forEach(([key, value]) => {
      const card = document.createElement("div");
      card.className = "inspect-tile";
      const label = document.createElement("span");
      label.className = "inspect-tile-label";
      label.textContent = key;
      const content = document.createElement("span");
      content.className = "inspect-tile-value";
      content.textContent = value;
      card.append(label, content);
      grid.append(card);
    });
    understanding.append(grid);
    const modelContext = body.generation?.model_context || {};
    const contextTables = Object.keys(modelContext.tables || modelContext.execution_context?.tables || {});
    if (contextTables.length) {
      const label = document.createElement("div");
      label.className = "inspect-label";
      label.textContent = "Selected SQL reference";
      understanding.append(label);
      const node = document.createElement("div");
      node.className = "inspect-reference";
      const title = document.createElement("strong");
      title.textContent = contextTables.join(" · ");
      const detail = document.createElement("code");
      detail.textContent = contextTables.map(name => {
        const selectedColumns = (modelContext.tables || modelContext.execution_context?.tables || {})[name]?.columns || {};
        const columns = Array.isArray(selectedColumns) ? selectedColumns : Object.keys(selectedColumns);
        return `${name}: ${columns.length ? columns.join(" · ") : "selected table"}`;
      }).join("\n");
      node.append(title, detail);
      understanding.append(node);
    }
    const presentation = document.createElement("div");
    presentation.className = "inspect-presentation";
    const presentationLabel = document.createElement("div");
    presentationLabel.className = "inspect-label";
    presentationLabel.textContent = "Presentation";
    const presentationValue = document.createElement("div");
    presentationValue.className = "inspect-presentation-value";
    presentationValue.textContent = chartSpec ? "Chart requested by the user"
      : body.generation?.accepted ? "SQL and executed data" : "No accepted result";
    presentation.append(presentationLabel, presentationValue);
    understanding.append(presentation);
    const extra = rows.filter(([key]) => !["Metric", "Grouping / grain", "Filters", "Metric columns"].includes(key));
    if (extra.length) {
      const more = document.createElement("details");
      more.className = "inspect-more";
      const summary = document.createElement("summary");
      summary.textContent = "More context";
      more.append(summary);
      extra.forEach(([key, value]) => {
        const row = document.createElement("div");
        row.className = "uq-row";
        const label = document.createElement("span");
        label.className = "k";
        label.textContent = key;
        const content = document.createElement("span");
        content.className = "v";
        content.textContent = value;
        row.append(label, content);
        more.append(row);
      });
      understanding.append(more);
    }
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
  const recoveryActive = body.generation?.recovery?.activated === true;
  selectInspectorTab(recoveryActive ? "recovery" : "understanding");
}

function emptyNote(text) {
  const p = document.createElement("p");
  p.className = "inspector-empty";
  p.textContent = text;
  return p;
}

function renderInspectorSchema() {
  const graphBox = $("#schemaGraph");
  const detail = $("#schemaDetail");
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
    const shownRelationships = new Set();
    names.forEach((name) => {
      const table = rawTables[name] || {};
      const card = document.createElement("button");
      card.type = "button";
      card.className = "inspect-schema-node";
      const title = document.createElement("strong");
      title.textContent = name;
      const columns = document.createElement("code");
      const selected = Array.isArray(table.columns)
        ? table.columns.map(column => [column, ""])
        : Object.entries(table.columns || {});
      columns.textContent = selected.map(([column, metadata]) => {
        const kind = typeof metadata === "object" ? metadata.type || metadata.database_type || "" : "";
        const role = (table.primary_key || []).includes(column) ? " · PRIMARY KEY" : "";
        return `${column}${kind ? ` · ${kind}` : ""}${role}`;
      }).join("\n") || "Selected table";
      card.append(title, columns);
      card.addEventListener("click", () => showSchemaDetail(detail, { table: name }));
      graphBox.append(card);
      (context.relationships || []).forEach((relationship, index) => {
        if (shownRelationships.has(index)) return;
        const left = String(relationship.left || "");
        const right = String(relationship.right || "");
        if (left.split(".")[0] !== name && right.split(".")[0] !== name) return;
        const join = document.createElement("div");
        join.className = "inspect-schema-join";
        const cardinality = relationship.cardinality
          ? ` · ${relationship.cardinality.replaceAll("_", " ").toLowerCase()}` : "";
        const source = relationship.state === "VERIFIED_FK" ? " · declared FK"
          : relationship.state ? " · inferred relationship" : "";
        join.textContent = `${left} → ${right}${cardinality}${source}`;
        graphBox.append(join);
        shownRelationships.add(index);
      });
    });
  } else {
    graphBox.append(emptyNote("Run a query to see the verified tables it used."));
  }
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

function inspectorRecoverySteps(recovery, events = []) {
  if (!recovery?.activated) return [];
  const signal = events.find(event => event.event_type === "SQL_FAILED");
  const modes = {
    ZERO_RESULT: ["Unmatched result detected", "The query ran but returned no rows."],
    NULL_RESULT: ["NULL result detected", "The query returned a NULL value."],
    FAILURE: ["Query failure detected", signal?.summary || "The SQL attempt failed."],
    CORRECTNESS: ["Correctness review requested", "The answer was flagged for review."],
    FILTER: ["Filter issue detected", "A filter produced an unexpected result."],
  };
  const [title, detail] = modes[recovery.mode] || ["Recovery started", recovery.failure_category || "An anomaly was recorded."];
  const steps = [{title, detail, meta: recovery.failure_code || "", icon: "alert", tone: "warn"}];
  const toolTitles = {
    inspect_schema: "Schema inspection", inspect_values: "Value check",
    query_database: "Database probe", run_probe: "Database probe",
  };
  for (const call of recovery.tool_calls || []) {
    const observations = [call.purpose, call.result_summary].filter(Boolean);
    steps.push({
      title: toolTitles[call.tool] || "Evidence check",
      detail: observations.filter((item, index) => !index || !sameDisplayText(item, observations[0])).join(" — "),
      meta: [call.tool, call.evidence_id ? `Observation ${call.evidence_id}` : null].filter(Boolean).join(" · "),
      icon: call.tool === "inspect_schema" ? "table" : "database", tone: "accent",
    });
  }
  if (recovery.agent_action || recovery.diagnosis_summary || recovery.agent_diagnosis || recovery.claims?.length) {
    const evidenceIds = [...new Set((recovery.claims || []).flatMap(claim => claim.evidence_ids || []))];
    steps.push({
      title: "Evidence evaluated",
      detail: recovery.diagnosis_summary || recovery.agent_diagnosis || recovery.claims[0]?.statement || "A recovery decision was recorded.",
      meta: [recovery.agent_action ? `Outcome: ${recovery.agent_action}` : "Agent interpretation",
        evidenceIds.length ? `Evidence ${evidenceIds.join(", ")}` : null].filter(Boolean).join(" · "),
      icon: recovery.agent_action === "ESCALATE" ? "alert" : "check",
      tone: recovery.agent_action === "ESCALATE" ? "warn" : "ok",
    });
  }
  return steps;
}

function renderInspectorRecovery(body) {
  const box = $("#recoveryMini");
  const recovery = body.generation?.recovery;
  box.replaceChildren();
  const kicker = document.createElement("div");
  kicker.className = "inspect-kicker";
  kicker.textContent = "INVESTIGATION · RECORDED EVIDENCE";
  box.append(kicker);
  if (!recovery || !recovery.activated) {
    box.append(emptyNote("The recovery agent only activates on anomalies."));
    return;
  }
  const info = document.createElement("div");
  info.className = "inspect-recovery-timeline";
  for (const step of inspectorRecoverySteps(recovery, body.recovery_events || [])) {
    const item = document.createElement("div");
    item.className = "inspect-recovery-step";
    const marker = document.createElement("span");
    marker.className = `inspect-recovery-marker ${step.tone}`;
    marker.innerHTML = icon(step.icon, 17);
    const content = document.createElement("div");
    content.className = "inspect-recovery-content";
    const title = document.createElement("strong");
    title.textContent = step.title;
    const detail = document.createElement("p");
    detail.textContent = step.detail;
    content.append(title, detail);
    if (step.meta) {
      const meta = document.createElement("small");
      meta.textContent = step.meta;
      content.append(meta);
    }
    item.append(marker, content);
    info.append(item);
  }
  box.append(info);
}

function renderInspectorPerformance(body) {
  const box = $("#validationContent");
  box.replaceChildren();
  const kicker = document.createElement("div");
  kicker.className = "inspect-kicker";
  kicker.textContent = "EXECUTION · MEASURED DETAILS";
  box.append(kicker);
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
      revealInspector("schema");
      $("#schemaContent").scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
  });
}

function renderHumanReview(fragment, review, responseMessage = "") {
  const panel = fragment.querySelector(".human-review");
  if (!panel || !review) return;
  panel.hidden = false;
  const reason = panel.querySelector(".human-review-reason");
  reason.textContent = review.reason || "Automated review is uncertain.";
  reason.hidden = sameDisplayText(responseMessage, reason.textContent);
  const question = panel.querySelector(".human-review-question");
  question.textContent = review.question || "Please clarify.";
  question.hidden = sameDisplayText(responseMessage, question.textContent)
    || (!reason.hidden && sameDisplayText(reason.textContent, question.textContent));
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

/* Backend-verified suggestions: chart recommendations and safe filters found
   on the executed result. SQL-backed filters go through the normal
   conversation pipeline; client filters only ever narrow already-fetched
   rows. High-cardinality fields rely on bounded server-side search instead
   of presenting 25 values as the full domain. */
function itemValueOptions(item, search) {
  const params = new URLSearchParams({
    table: item.table || "",
    column: item.column || "",
  });
  if (search) params.set("search", search);
  return fetch(
    `/api/databases/${encodeURIComponent($("#databaseSelect").value)}/filter-values`
    + `?${params.toString()}`,
  ).then((response) => {
    if (!response.ok) throw new Error(`Value request failed (${response.status})`);
    return response.json();
  });
}

function suggestionValueMenu(anchor, item, values, article, runId) {
  const panel = document.createElement("div");
  const title = document.createElement("strong");
  title.className = "query-insight-title";
  title.textContent = `Filter by ${item.label || item.field}`;
  const caption = document.createElement("p");
  caption.textContent = item.exec_mode === "CLIENT_FILTER"
    ? "Hide all other rows on this page. SQL unchanged."
    : `Runs a new, validated SQL through the normal pipeline (${item.join_path?.length ? `joining via ${item.join_path.join(" then ")}` : "the approved tables"}).`;
  panel.append(title, caption);
  if (item.control === "search") {
    /* High-cardinality field: bounded server-side search, never the claim
       that the listed values are the complete domain. */
    const searchRow = document.createElement("p");
    searchRow.className = "query-insight-search-row";
    const input = document.createElement("input");
    input.type = "search";
    input.maxLength = 64;
    input.placeholder = `Search ${item.label || item.field}…`;
    const go = document.createElement("button");
    go.type = "button";
    go.className = "query-insight-action muted";
    go.textContent = "Search values";
    go.addEventListener("click", () => {
      const term = input.value.trim();
      if (!term) return;
      go.disabled = true;
      itemValueOptions(item, term)
        .then(payload => {
          suggestionValueMenu(anchor, item, payload.values.slice(0, 25), article, runId);
        })
        .catch(error => { showError(error.message); go.disabled = false; });
    });
    searchRow.append(input, go);
    panel.append(searchRow);
  }
  values.forEach((value) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "query-insight-action";
    button.textContent = value;
    button.addEventListener("click", () => {
      dismissInsight();
      if (state.busy || !state.ready) return;
      if (item.exec_mode === "CLIENT_FILTER") {
        article._setClientFilter?.({ column: item.field, value });
      } else {
        sendFromAnswer(
          runId,
          `Restrict the results to ${item.column || item.field} being ${JSON.stringify(value)}.`,
        ).catch(error => showError(error.message));
      }
    });
    panel.append(button);
  });
  if (!values.length) {
    const empty = document.createElement("p");
    empty.className = "query-insight-empty";
    empty.textContent = "No matching values found. Try another search.";
    panel.append(empty);
  }
  if (article._clearClientFilter && article._hasClientFilter?.()) {
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "query-insight-action muted";
    clear.textContent = "Clear row filter";
    clear.addEventListener("click", () => {
      dismissInsight();
      article._clearClientFilter();
    });
    panel.append(clear);
  }
  placeInsight(anchor, panel);
}

function renderBackendSuggestions(article, body) {
  const section = article.querySelector(".backend-suggestions");
  if (!section) return;
  const runId = article.dataset.runId || body.run_id || null;
  const recommendations = body.chart_recommendations;
  const filters = body.suggested_filters;
  const generation = body.generation || {};
  if (!recommendations && !filters) return;
  const strip = document.createElement("div");
  strip.className = "backend-suggestion-strip";
  if (recommendations) {
    const label = document.createElement("span");
    label.className = "suggestion-group-label";
    label.textContent = "Suggested charts";
    strip.append(label);
    const charts = [recommendations.primary_chart, ...(recommendations.alternative_charts || [])];
    charts.forEach((chart) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "suggestion-chip chart-chip";
      button.textContent = chart.chart_type.replaceAll("_", " ");
      button.title = chart.reason || "";
      if (chart === recommendations.primary_chart) button.classList.add("primary");
      button.addEventListener("click", () => {
        if (state.busy || !state.ready) return;
        // Validated recommendation over the executed result: apply the typed
        // chart action locally (no model call, no SQL) whenever the result
        // already contains every required column.
        if (article._chartViewState?.tryChart({
          x: chart.x,
          y: chart.y,
          y_measures: chart.y_measures || [chart.y],
          series: chart.series,
          chart_type: chart.chart_type,
        })) return;
        const measures = (chart.y_measures && chart.y_measures.length > 1)
          ? chart.y_measures.join(", ") : chart.y;
        sendFromAnswer(
          runId,
          `Plot ${chart.x} against ${measures} as a ${chart.chart_type.replaceAll("_", " ")} chart.`,
        ).catch(error => showError(error.message));
      });
      strip.append(button);
    });
  }
  if (filters) {
    const actionable = [
      ...(filters.client_filters || []),
      ...(filters.sql_filters || []),
    ];
    if (actionable.length) {
      const label = document.createElement("span");
      label.className = "suggestion-group-label";
      label.textContent = "Filter fields";
      strip.append(label);
      actionable.slice(0, 6).forEach((item) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "suggestion-chip filter-chip";
        button.textContent = item.label || item.field;
        if (item.exec_mode === "CLIENT_FILTER") button.classList.add("rows");
        button.title = `${item.reason}${item.cardinality != null ? ` · ${item.cardinality} distinct values` : ""}`;
        button.addEventListener("click", (event) => {
          const anchor = event.currentTarget;
          if (item.exec_mode === "CLIENT_FILTER") {
            // Client filters live in the executed result, not in any table.
            const index = (generation.columns || []).indexOf(item.field);
            const rows = generation.rows || [];
            const values = index < 0 ? [] : [
              ...new Set(rows.map(row => row[index] == null ? "" : String(row[index]))),
            ].filter(Boolean).sort();
            if (!values.length) {
              showError("No values for this field in the executed result.");
              return;
            }
            suggestionValueMenu(anchor, item, values.slice(0, 25), article, runId);
            return;
          }
          itemValueOptions(item)
            .then((payload) => suggestionValueMenu(
              anchor, item, payload.values.slice(0, 25), article, runId,
            ))
            .catch(error => showError(error.message));
        });
        strip.append(button);
      });
    }
    const unavailable = (filters.unavailable_filters || []).length;
    if (unavailable) {
      const muted = document.createElement("span");
      muted.className = "suggestion-unavailable";
      muted.textContent = `${unavailable} field${unavailable === 1 ? "" : "s"} not filterable (sensitive or too broad)`;
      muted.title = (filters.unavailable_filters || [])
        .map(item => `${item.field}: ${item.reason}`).join("\n");
      strip.append(muted);
    }
  }
  if (!strip.childElementCount) return;
  section.append(strip);
  section.hidden = false;
  hydrateIcons(section);
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
  const groups = new Map();
  for (const run of state.history) {
    const id = run.conversation_id || run.run_id;
    if (!groups.has(id)) groups.set(id, run);
  }
  const visible = [...groups.values()];
  if (!visible.length) {
    container.append(emptyNote("Completed queries appear here."));
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

const INSPECTOR_SECTIONS = {
  understanding: "#understandingSection",
  schema: "#schemaSection",
  recovery: "#recoverySection",
  validation: "#validationSection",
};

function selectInspectorTab(tab) {
  const selected = INSPECTOR_SECTIONS[tab] ? tab : "understanding";
  Object.entries(INSPECTOR_SECTIONS).forEach(([name, selector]) => {
    const section = $(selector);
    if (section) {
      section.hidden = name !== selected;
      if (name === selected) section.open = true;
    }
  });
  document.querySelectorAll("[data-inspector-tab]").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.inspectorTab === selected));
  });
}

function setInspectorOpen(open) {
  document.body.classList.toggle("inspector-open", open);
  $("#inspectorBackdrop").hidden = !open;
  $("#inspectorPane").setAttribute("aria-hidden", String(!open));
  $("#inspectorToggle").setAttribute("aria-expanded", String(open));
  if (open) $("#inspectorClose").focus();
  else $("#inspectorToggle").focus();
}

$("#inspectorToggle")?.addEventListener("click", () => {
  setInspectorOpen(!document.body.classList.contains("inspector-open"));
});
$("#inspectorClose")?.addEventListener("click", () => setInspectorOpen(false));
$("#inspectorBackdrop")?.addEventListener("click", () => setInspectorOpen(false));
document.querySelectorAll("[data-inspector-tab]").forEach((button) => {
  button.addEventListener("click", () => selectInspectorTab(button.dataset.inspectorTab));
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && document.body.classList.contains("inspector-open")) {
    setInspectorOpen(false);
  }
});
selectInspectorTab("understanding");

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

document.addEventListener("pointerdown", event => {
  if (activeInsight && !activeInsight.contains(event.target)) dismissInsight();
});
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
document.querySelectorAll(".side-nav a[data-view]").forEach(link => {
  link.addEventListener("click", event => {
    event.preventDefault();
    $("#messageInput").focus();
  });
});

document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    $("#resetButton").click();
  }
  if (event.key === "Escape") closeChartFullscreen();
});
