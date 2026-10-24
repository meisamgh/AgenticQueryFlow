// Run with node --test tests/frontend_rendering.cjs. No browser dependencies.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(`${__dirname}/../web/app.js`, 'utf8');
const styles = fs.readFileSync(`${__dirname}/../web/styles.css`, 'utf8');
const markup = fs.readFileSync(`${__dirname}/../web/index.html`, 'utf8');
const explorerContext = vm.createContext({window: {}});
vm.runInContext(fs.readFileSync(`${__dirname}/../web/chart_explorer.js`, 'utf8'), explorerContext);
const explorer = explorerContext.window.QueryflowChartExplorer;
const filtersContext = vm.createContext({window: {}});
vm.runInContext(fs.readFileSync(`${__dirname}/../web/query_filters.js`, 'utf8'), filtersContext);
const queryFilters = filtersContext.window.QueryflowFilters;
test('inspector counts model calls without counting tools as models', () => {
  const summary = source.slice(source.indexOf('function renderInspectorTokenSummary('),
    source.indexOf('function wireSqlIdents('));
  assert.ok(summary.includes('entry.kind === "MODEL"'));
  assert.ok(summary.includes('usage.input_tokens == null && usage.output_tokens == null'));
});
test('explicit chart-type clicks enter the top-layer conversation path', () => {
  const toolbar = source.slice(source.indexOf('function wireChartSection('),
    source.indexOf('function closeChartFullscreen('));
  assert.ok(toolbar.includes('sendFromAnswer(runId, `Change the previous chart'));
  assert.ok(!toolbar.includes('settings.chartType = type'));
});
test('query filter utilities remain available without a visible filter bar', () => {
  const body = {
    resolved_question: 'Show orders by country and date',
    generation: {model_context: {tables: {orders: {columns: {country: {}, order_date: {}}}}}},
    suggested_filters: {
      sql_filters: [
        {field: 'orders.country', table: 'orders', column: 'country', exec_mode: 'SQL_FILTER'},
        {field: 'users.region', table: 'users', column: 'region', exec_mode: 'SQL_FILTER'},
      ],
      client_filters: [{field: 'order_date', control: 'date_range', exec_mode: 'CLIENT_FILTER'}],
    },
  };
  assert.deepEqual(Array.from(queryFilters.relevantFields(body), item => item.field),
    ['orders.country']);
  assert.deepEqual(Array.from(queryFilters.selectedValues({filters: [
    {operand: 'orders.country', operator: 'IN', value: ['DE', 'FR']},
  ]}, 'orders.country')), ['DE', 'FR']);
  assert.ok(!markup.includes('class="query-filter-bar"'));
  assert.ok(!source.includes('wireQueryFilterBar('));
});
test('query filter prompt utility still requests a governed query', () => {
  const prompt = queryFilters.changePrompt([
    {kind: 'values', field: 'orders.country', values: ['DE', "O'Reilly"]},
  ]);
  assert.ok(prompt.includes('"orders.country"'));
  assert.ok(prompt.includes('"O\'Reilly"'));
  assert.ok(prompt.includes('one validated read-only SQL query'));
  assert.ok(!markup.includes('query-filter-apply'));
});
test('success badge is hidden and plot choices are centered inside the answer', () => {
  assert.ok(source.includes('if (accepted && status !== "CHART UNAVAILABLE") { pill.hidden = true;'));
  const section = markup.split('<div class="chart-section"')[1].split('<div class="chart-slot"')[0];
  assert.ok(section.includes('class="recommended-plots"'));
  assert.ok(section.indexOf('class="recommended-plots"') > section.indexOf('</header>'));
  assert.ok(styles.includes('.recommended-plots { display: flex; align-items: center; justify-content: center;'));
  assert.ok(source.includes('body.chart_recommendations'));
});
test('database is configured privately and the header hides the session ID', () => {
  const composer = markup.split('<form class="composer"')[1].split('</form>')[0];
  const header = markup.split('<header class="topbar">')[1].split('</header>')[0];
  for (const id of ['databaseSelect', 'connectionCard', 'healthStatus']) {
    assert.ok(header.includes(`id="${id}"`), `${id} is missing from the header`);
    assert.ok(!composer.includes(`id="${id}"`), `${id} should not remain in the composer`);
  }
  assert.ok(header.includes('id="databaseSelect" hidden'));
  assert.ok(!markup.includes('id="sessionId"'));
  assert.ok(source.includes('item.db_id === uiConfig.default_db_id'));
  for (const id of ['voiceButton', 'sendButton']) {
    assert.ok(composer.includes(`id="${id}"`), `${id} is missing from the composer`);
  }
  assert.ok(!composer.includes('sqlModelSelect'));
  assert.ok(!composer.includes('contextModelSelect'));
  assert.ok(!markup.includes('id="composerChips"'));
  assert.ok(!markup.includes('id="heroExamples"'));
  assert.ok(!markup.includes('Enter to send'));
});
test('empty success responses clear history and Clear resets the visible conversation', async () => {
  const apiSource = source.slice(source.indexOf('async function api('), source.indexOf('async function initialize('));
  let status = 204;
  const apiContext = vm.createContext({fetch: async () => ({ok: true, status, text: async () => ''})});
  vm.runInContext(`${apiSource}\nglobalThis.testApi = api;`, apiContext);
  assert.equal(await apiContext.testApi('/api/history', {method: 'DELETE'}), null);
  status = 200;
  assert.equal(await apiContext.testApi('/api/history', {method: 'DELETE'}), null);
  const clear = source.slice(source.indexOf('$("#clearHistory").addEventListener('), source.indexOf('$("#resetButton").addEventListener('));
  assert.ok(clear.includes('$("#messages").replaceChildren()'));
  assert.ok(clear.includes('$("#emptyState").hidden = false'));
  assert.ok(clear.includes('state.history = []'));
  assert.ok(!source.includes('section.querySelector(".chart-clear-filter")'));
});
test('display row filters share chart state and zero matches stay empty', () => {
  assert.ok(source.includes('article._setChartViewRowFilters = filters => article._chartViewState.setRowFilters(filters)'));
  assert.ok(source.includes('return filtered;'));
  assert.ok(!source.includes('return filtered.length ? filtered : rows;'));
  assert.ok(!source.includes('renderBackendSuggestions('));
});
test('browser checks exact environment model pair and sends no model overrides', () => {
  const initialize = source.slice(source.indexOf('async function initialize('), source.indexOf('function setHealth('));
  assert.ok(initialize.includes('item.provider === uiConfig.sql_provider'));
  assert.ok(initialize.includes('item.model === uiConfig.sql_model'));
  assert.ok(!initialize.includes('preferredModel'));
  const sender = source.slice(source.indexOf('async function send('), source.indexOf('function showError('));
  assert.ok(sender.includes('api("/api/chat/jobs"'));
  assert.ok(!sender.includes('"#sqlModelSelect"'));
  assert.ok(!sender.includes('"#contextModelSelect"'));
  assert.ok(!sender.includes('context_provider:'));
  assert.ok(!sender.includes('context_model:'));
});
test('conversation history has no heading or search control', () => {
  assert.ok(!markup.includes('YOUR CONVERSATIONS'));
  assert.ok(!markup.includes('id="historySearch"'));
  assert.ok(!source.includes('$("#historySearch")'));
  assert.ok(markup.includes('id="conversationHistory"'));
});
test('semantic chips edit through the live query path without a branch action', () => {
  assert.ok(source.includes('function editSemanticChip('));
  assert.ok(source.includes('sendFromAnswer(runId, prompt)'));
  assert.ok(!source.includes('state.pendingBranch'));
  assert.ok(!source.includes('branch_from_parent:'));
  assert.ok(!markup.includes('Schema explorer'));
});
test('correctness and optimization actions live in the SQL panel only', () => {
  const sqlPane = markup.split('<section class="panel-card sql-panel-pane"')[1].split('</section>')[0];
  assert.ok(sqlPane.includes('class="sql-actions"'));
  const render = source.slice(source.indexOf('async function appendAssistant('), source.indexOf('function actionButton('));
  assert.ok(render.includes('const sqlActions = fragment.querySelector(".sql-actions")'));
  assert.ok(render.includes('actionButton("Check correctness"'));
  assert.ok(render.includes('actionButton("Optimize"'));
  assert.ok(render.includes('sendFromAnswer(runId, "Check whether'));
  assert.ok(render.includes('sendFromAnswer(runId, "Optimize the previous'));
  assert.ok(!render.includes('actionButton("Branch from here"'));
  assert.ok(!render.includes('actionButton("Save question"'));
  assert.ok(!markup.includes('id="savedQuestions"'));
  assert.ok(!markup.includes('class="icon-button save-button"'));
});
test('single-value answer uses a readable label and exact grouped number', () => {
  const helpers = source.slice(source.indexOf('function readableResultLabel('), source.indexOf('function renderAnswerPreview('));
  const display = vm.runInNewContext(`${helpers}; ({readableResultLabel, formatKpiValue})`);
  assert.equal(display.readableResultLabel('total_monthly_consumption'), 'Total monthly consumption');
  assert.equal(display.readableResultLabel('CustomerID'), 'Customer ID');
  assert.equal(display.formatKpiValue(3415480125.73), '3,415,480,125.73');
  assert.equal(display.formatKpiValue('1234567890.123456789'), '1,234,567,890.123456789');
  assert.equal(display.formatKpiValue('001234'), '001234');
  assert.equal(display.formatKpiValue(null), 'NULL');
  const preview = source.slice(source.indexOf('function renderAnswerPreview('), source.indexOf('async function appendAssistant('));
  assert.ok(preview.includes('readableResultLabel(columns[0])'));
  assert.ok(preview.includes('formatKpiValue(rows[0][0])'));
});
test('inspector has separate context, schema, recovery, pipeline and tokens tabs', () => {
  for (const tab of ['understanding', 'schema', 'recovery', 'pipeline', 'tokens']) {
    assert.ok(markup.includes(`data-inspector-tab="${tab}"`));
  }
  assert.ok(!markup.includes('data-inspector-tab="validation"'));
  assert.ok(markup.includes('id="pipelineContent"'));
  assert.ok(markup.includes('id="tokensContent"'));
  assert.ok(source.includes('renderPipelineDetails(pipelineTarget'));
  assert.ok(source.includes('renderTokenAccounting(tech'));
  assert.ok(source.includes('pipelineTarget.append(attemptsCard)'));
  assert.ok(styles.includes('.pipeline-bar-fill'));
  assert.ok(markup.includes('id="inspectorBackdrop"'));
});
test('pipeline bars represent measured stage time and leave missing stages unmeasured', () => {
  const makeNode = () => ({children: [], style: {}, append(...items) { this.children.push(...items); },
    replaceChildren() { this.children = []; }, setAttribute() {}});
  const container = makeNode();
  const fragment = {querySelector: () => container};
  const pipeline = vm.runInNewContext(`${source.slice(source.indexOf('function renderPipelineDetails('),
    source.indexOf('function showError('))}; renderPipelineDetails`, {
    document: {createElement: makeNode},
  });
  pipeline(fragment, {total: 2000, routing: 500}, {}, {});
  assert.equal(container.children[0].children[1].textContent, '2.0 s');
  const routing = container.children.find(row => row.children[0]?.textContent === 'Routing');
  assert.equal(routing.children[2].children[0].style.width, '25%');
  const unknown = container.children.find(row => row.children[0]?.textContent === 'Context planning');
  assert.equal(unknown.children[1].textContent, 'Not reported');
  assert.equal(unknown.children[2].children[0].style.width, '0%');
});
test('context inspector follows the flat prototype layout using live response facts', () => {
  assert.ok(markup.includes('<span>Context inspector</span>'));
  assert.ok(styles.includes('.inspector-section > summary { display: none; }'));
  assert.ok(styles.includes('.inspect-kicker'));
  const renderer = source.slice(source.indexOf('function updateInspector('), source.indexOf('function renderInspectorRunDetails('));
  assert.ok(renderer.includes('QUERY UNDERSTANDING · CURRENT RESPONSE'));
  assert.ok(renderer.includes('body.generation?.model_context'));
  assert.ok(renderer.includes('Selected SQL reference'));
  for (const label of ['Measure', 'Period', 'Dimension', 'Filter']) {
    assert.ok(renderer.includes(`['${label}'`) || renderer.includes(`["${label}"`));
  }
  assert.ok(markup.includes('Recovery trace'));
  assert.ok(!renderer.includes('Example reference'));
  assert.ok(source.includes('card.className = "inspect-schema-node"'));
  assert.ok(source.includes('join.className = "inspect-schema-join"'));
  assert.ok(!source.includes('schemaGraphSvg('));
});
test('inspector context cards are built from response rows, not prototype values', () => {
  assert.ok(source.includes('grid.className = "inspect-grid"'));
  assert.ok(source.includes('body.state?.semantic_contract'));
  assert.ok(source.includes('chartSpec?.y_label'));
  assert.ok(styles.includes('.inspect-tile-value'));
  assert.ok(!source.includes('Example reference · Check actual schema'));
});
test('successful response template orders Answer, Data and SQL', () => {
  const template = markup.split('<template id="assistantTemplate">')[1].split('</template>')[0];
  for (const tab of ['answer', 'sql', 'data']) assert.ok(template.includes(`data-response-tab="${tab}"`));
  assert.ok(template.indexOf('data-response-tab="answer"') < template.indexOf('data-response-tab="data"'));
  assert.ok(template.indexOf('data-response-tab="data"') < template.indexOf('data-response-tab="sql"'));
  assert.ok(template.includes('class="answer-tab-panel"'));
  assert.ok(template.includes('class="sql-result-split" hidden'));
  assert.ok(template.includes('class="panel-card sql-panel-pane" role="tabpanel" hidden'));
  assert.ok(template.includes('class="panel-card result-panel-pane" role="tabpanel" hidden'));
  assert.ok(template.indexOf('class="chart-section"') > template.indexOf('class="answer-tab-panel"'));
  const answerRenderer = source.slice(source.indexOf('async function appendAssistant('), source.indexOf('function actionButton('));
  assert.ok(answerRenderer.includes('wireResponseTabs(article, {sql: Boolean(sqlText), data: Boolean(generation.columns?.length)})'));
  assert.ok(!answerRenderer.includes('options.restored ? "sql"'));
});
const tabsContext = vm.createContext({window: {requestAnimationFrame: callback => callback()}});
vm.runInContext(source.slice(source.indexOf('/* Response views are local'), source.indexOf('function wireChartSection(')), tabsContext);
test('response tabs are independent, keep the chart and never execute a query', () => {
  let resized = 0;
  tabsContext.api = () => { throw Error('tab switching called the API'); };
  tabsContext.send = () => { throw Error('tab switching sent a question'); };
  const makeArticle = () => {
    const buttons = Object.fromEntries(['answer', 'sql', 'data'].map(view => [view, {
      dataset: {responseTab: view}, disabled: false, attributes: {},
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, callback) { this.listeners ||= {}; this.listeners[name] = callback; },
      focus() {},
    }]));
    const tabs = {
      hidden: true,
      querySelector(selector) { return buttons[selector.match(/data-response-tab="(.*?)"/)?.[1]]; },
      querySelectorAll() { return Object.values(buttons); },
    };
    const answer = {hidden: false, setAttribute() {}};
    const split = {hidden: true};
    const sql = {hidden: true, setAttribute() {}};
    const data = {hidden: true, setAttribute() {}};
    const versions = {hidden: true, dataset: {available: 'true'}};
    const nodes = {'.response-tabs': tabs, '.answer-tab-panel': answer,
      '.sql-result-split': split, '.sql-panel-pane': sql, '.result-panel-pane': data,
      '.versions-panel': versions};
    const article = {dataset: {}, _echart: {resize() { resized++; }},
      querySelector(selector) { return nodes[selector]; }};
    return {article, buttons, tabs, answer, split, sql, data, versions};
  };
  const first = makeArticle(), second = makeArticle();
  tabsContext.wireResponseTabs(first.article);
  tabsContext.wireResponseTabs(second.article);
  assert.equal(first.article.dataset.activeResponseTab, 'answer');
  first.buttons.sql.listeners.click();
  assert.equal(first.article.dataset.activeResponseTab, 'sql');
  assert.equal(first.answer.hidden, true);
  assert.equal(first.split.hidden, false);
  assert.equal(first.sql.hidden, false);
  assert.equal(first.data.hidden, true);
  assert.equal(first.versions.hidden, false);
  assert.equal(second.article.dataset.activeResponseTab, 'answer');
  first.buttons.data.listeners.click();
  assert.equal(first.data.hidden, false);
  assert.equal(first.sql.hidden, true);
  first.buttons.answer.listeners.click();
  assert.equal(first.answer.hidden, false);
  assert.equal(first.split.hidden, true);
  assert.ok(resized >= 3);
});
test('a running question does not auto-scroll progress or the final answer', () => {
  const progress = source.slice(source.indexOf('function appendProgress('), source.indexOf('function wait('));
  const answer = source.slice(source.indexOf('async function appendAssistant('), source.indexOf('function actionButton('));
  assert.ok(!progress.includes('scrollIntoView'));
  assert.ok(!answer.includes('scrollIntoView'));
  assert.ok(!styles.includes('.thread-scroll { flex: 1; overflow-y: auto; overscroll-behavior: contain; scroll-behavior: smooth; }'));
});
test('Queryflow colors follow the supplied light and dark reference palettes', () => {
  for (const color of [
    '--bg: #fafaf8', '--surface: #ffffff', '--text: #20212b',
    '--accent: #6e60e6', '--nav: #191b27', '--sql-bg: #1d2130',
    '--success: #20946c', '--danger: #d45d65', '--warning: #c1803a',
    '--surface: #242633', '--border: #363747',
  ]) assert.ok(styles.includes(color), `Missing reference token ${color}`);
});
class Element {
  constructor() { this.children = []; }
  append(...children) { this.children.push(...children); }
  createTHead() { const child = new Element(); this.append(child); return child; }
  createTBody() { return this.createTHead(); }
  insertRow() { return this.createTHead(); }
  insertCell() { return this.createTHead(); }
  classList = { add() {} };
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, callback) { this.listeners ||= {}; this.listeners[name] = callback; }
}
const context = vm.createContext({
  document: { createElement: () => new Element() },
  window: { matchMedia: () => ({ matches: false }) },
});
context.document.createElementNS = () => new Element();
vm.runInContext(fs.readFileSync(`${__dirname}/../web/echarts_view.js`, 'utf8'), context);
test('ECharts adapter reads the validated data and preserves negative values', () => {
  const spec = {chart_type: 'bar', x: 'Category', y: 'Value', x_label: 'Category',
    y_label: 'Value', data: [{Category: 'A', Value: -2}, {Category: 'B', Value: 4}]};
  const option = context.window.QueryflowECharts.buildOption(spec);
  assert.equal(option.series[0].data[0], -2);
  assert.equal(option.series[0].data[1], 4);
  assert.equal(option.xAxis.data[0], 'A');
});
test('ECharts adapter renders separate named series without inventing zeros', () => {
  const spec = {chart_type: 'stacked_bar', x: 'Month', y: 'Revenue', series: 'Country',
    x_label: 'Month', y_label: 'Revenue', data: [
      {Month: 'Jan', Country: 'DE', Revenue: 2}, {Month: 'Jan', Country: 'FR', Revenue: 4},
      {Month: 'Feb', Country: 'DE', Revenue: 3},
    ]};
  const option = context.window.QueryflowECharts.buildOption(spec);
  assert.equal(option.series.length, 2);
  assert.equal(option.series[0].stack, 'total');
  assert.equal(option.series[1].data[1], null);
});
test('ECharts uses the verified histogram bin column', () => {
  const spec = {chart_type: 'histogram', x: 'price_bucket', y: 'count', data: [
    {price_bucket: 10, count: 2}, {price_bucket: 20, count: 3},
  ]};
  const option = context.window.QueryflowECharts.buildOption(spec);
  assert.deepEqual([...option.xAxis.data], ['10', '20']);
});
test('ECharts preserves all 50 points in chronological order and exposes zoom', () => {
  const dates = Array.from({length: 50}, (_, index) => ({
    Date: new Date(Date.UTC(2026, 0, index + 1)).toISOString().slice(0, 10),
    Revenue: index,
  })).reverse();
  const option = context.window.QueryflowECharts.buildOption({chart_type: 'line',
    x: 'Date', y: 'Revenue', time_format: 'day', data: dates});
  assert.equal(option.series[0].data.length, 50);
  assert.equal(option.series[0].data[0], 0);
  assert.equal(option.series[0].data[49], 49);
  assert.equal(option.dataZoom.length, 2);
});
test('ECharts renders missing points as gaps, keeps zero and negative values, and rejects unsafe stacks', () => {
  const base = {x: 'Month', y: 'Revenue', time_format: 'month', data: [
    {Month: '2026-01', Revenue: 0}, {Month: '2026-02', Revenue: null},
    {Month: '2026-03', Revenue: -2},
  ]};
  const option = context.window.QueryflowECharts.buildOption({...base, chart_type: 'line'});
  assert.deepEqual([...option.series[0].data], [0, null, -2]);
  assert.equal(option.yAxis.scale, false);
  assert.notEqual(option.tooltip.valueFormatter(0.00000002), '0');
  assert.equal(context.window.QueryflowECharts.buildOption({...base, chart_type: 'stacked_bar', series: 'Country'}), null);
  const constant = context.window.QueryflowECharts.buildOption({...base, chart_type: 'bar', data: [
    {Month: '2026-01', Revenue: 0}, {Month: '2026-02', Revenue: 0},
  ]});
  assert.deepEqual([...constant.series[0].data], [0, 0]);
  assert.equal(constant.yAxis.scale, false);
});
test('chart explorer refuses an axis switch that would silently drop separate rows', () => {
  const spec = {chart_type: 'grouped_bar', x: 'Month', y: 'Revenue', series: 'Country',
    source_columns: ['Month', 'Country', 'Revenue'], data: [
      {Month: 'Jan', Country: 'DE', Revenue: 10},
      {Month: 'Jan', Country: 'FR', Revenue: 20},
    ]};
  assert.equal(explorer.apply(spec, {series: ''}), null);
  assert.equal(spec.data.length, 2);
});
test('date-range controls reject impossible dates instead of guessing their order', () => {
  assert.equal(explorer.chronological('2026-02-31'), null);
  assert.equal(explorer.chronological('202613'), null);
  assert.ok(explorer.chronological('2026-02-28') < explorer.chronological('2026-03-01'));
});
test('answer chart omits redundant source explanation and keeps narrow layout rules', () => {
  assert.ok(!markup.includes('class="chart-details"'));
  assert.ok(!markup.includes('class="chart-active-view"'));
  assert.ok(!source.includes('Based on ${spec.source_row_count'));
  assert.ok(styles.includes('.chart-slot-ec, .chart-slot-plotly { height: 320px'));
  // The client always keeps the bounded full result (max 500 rows) in server
  // RAM so an explicit "Plot it" follow-up never needs new SQL.
  assert.ok(source.includes('max_rows: 500,'));
  assert.ok(source.includes('source_row_count === generation.rows?.length'));
});
test('ECharts resizes with its container and cleans up responsive observers', () => {
  let observed, disconnected = false, resizeCalls = 0, disposed = false;
  const oldObserver = context.ResizeObserver;
  const oldEcharts = context.window.echarts;
  const oldAdd = context.window.addEventListener;
  const oldRemove = context.window.removeEventListener;
  context.ResizeObserver = class {
    constructor(callback) { this.callback = callback; }
    observe(node) { observed = node; this.callback(); }
    disconnect() { disconnected = true; }
  };
  context.window.echarts = {init: () => ({setOption() {}, resize() { resizeCalls++; },
    dispose() { disposed = true; }, on() {}})};
  context.window.addEventListener = () => {};
  context.window.removeEventListener = () => {};
  const container = {};
  const mounted = context.window.QueryflowECharts.render(container, {chart_spec: {
    chart_type: 'bar', x: 'Category', y: 'Value', data: [{Category: 'A', Value: 2}],
  }});
  assert.equal(observed, container);
  assert.equal(resizeCalls, 1);
  mounted.dispose();
  assert.equal(disconnected, true);
  assert.equal(disposed, true);
  context.ResizeObserver = oldObserver;
  context.window.echarts = oldEcharts;
  context.window.addEventListener = oldAdd;
  context.window.removeEventListener = oldRemove;
});
test('eight-column complete results offer compatible chart alternatives and metrics', () => {
  const rows = ['2026-01', '2026-02'].flatMap((Month, index) => ['DE', 'FR'].map((Country, group) => ({
    Month, Country, Revenue: 100 + index + group, Cost: 30 + index, Profit: 70 + group,
    Orders: 10 + index, Customers: 8 + group, AvgOrderValue: 12 + index,
  })));
  const spec = {chart_type: 'multi_line', x: 'Month', y: 'Revenue', series: 'Country',
    time_format: 'month', source_columns: Object.keys(rows[0]), data: rows};
  const choices = [...explorer.choices(spec)];
  assert.ok(choices.includes('multi_line'));
  assert.ok(choices.includes('stacked_area'));
  assert.ok([...explorer.filters(spec).metric].includes('Profit'));
  const filtered = explorer.apply(spec, {categories: ['DE'], metric: 'Profit', chartType: 'line'});
  assert.equal(filtered.data.length, 2);
  assert.equal(filtered.y, 'Profit');
  assert.equal(filtered.chart_type, 'line');
  assert.equal(spec.data.length, 4, 'display filter must not mutate the executed result');
});
test('stacked charts require a complete nonnegative series grid', () => {
  const base = {chart_type: 'grouped_bar', x: 'Country', y: 'Revenue', series: 'Product',
    time_format: 'raw', data: [
      {Country: 'DE', Product: 'A', Revenue: 1}, {Country: 'DE', Product: 'B', Revenue: 2},
      {Country: 'FR', Product: 'A', Revenue: 3},
    ]};
  assert.ok(![...explorer.choices(base)].includes('stacked_bar'));
  assert.ok(![...explorer.choices({...base, data: [...base.data,
    {Country: 'FR', Product: 'B', Revenue: -1} ]})].includes('stacked_bar'));
  assert.ok([...explorer.choices({...base, data: [...base.data,
    {Country: 'FR', Product: 'B', Revenue: 4} ]})].includes('stacked_bar'));
});
test('display-only filters preserve executed rows and support compatible wide measures', () => {
  const rows = ['2026-01', '2026-02'].flatMap(Month => ['DE', 'FR'].map(Country =>
    ({Month, Country, Revenue: 100, Profit: 40, Orders: 4})));
  const spec = {chart_type: 'multi_line', x: 'Month', y: 'Revenue', series: 'Country',
    source_columns: Object.keys(rows[0]), time_format: 'month', data: rows};
  const filtered = explorer.apply(spec, {categories: ['DE'], metrics: ['Revenue', 'Profit']});
  assert.equal(filtered.data.length, 2);
  assert.equal(spec.data.length, 4);
  assert.equal(filtered.series, null);
  assert.deepEqual([...filtered.y_measures], ['Revenue', 'Profit']);
  assert.equal(filtered.chart_type, 'multi_line');
  assert.equal(explorer.compatibleMeasures(['Revenue', 'Orders'], spec), false);
  const option = context.window.QueryflowECharts.buildOption(filtered);
  assert.deepEqual(option.series.map(series => series.name), ['Revenue', 'Profit']);
  assert.deepEqual([...option.series[0].data], [100, 100]);
});
test('stack recommendations reject ratios and accept complete additive time series', () => {
  const rows = ['2026-01', '2026-02'].flatMap(Month => ['DE', 'FR'].map(Country =>
    ({Month, Country, Revenue: 10, ConversionRate: 0.2})));
  const base = {chart_type: 'multi_line', x: 'Month', series: 'Country', time_format: 'month', data: rows};
  assert.ok([...explorer.choices({...base, y: 'Revenue'})].includes('stacked_bar'));
  assert.ok(![...explorer.choices({...base, y: 'ConversionRate'})].includes('stacked_bar'));
});
test('zoom counts unique positions and also handles dense ordinary categories', () => {
  const make = (count, time) => ({chart_type: time ? 'line' : 'bar', x: 'Period', y: 'Value',
    time_format: time ? 'day' : 'raw', data: Array.from({length: count}, (_, index) => ({
      Period: time ? new Date(Date.UTC(2026, 0, index + 1)).toISOString().slice(0, 10) : `Group ${index}`,
      Value: index + 1,
    }))});
  assert.equal(context.window.QueryflowECharts.buildOption(make(2, true)).dataZoom.length, 0);
  assert.equal(context.window.QueryflowECharts.buildOption(make(25, true)).dataZoom.length, 1);
  assert.equal(context.window.QueryflowECharts.buildOption(make(26, true)).dataZoom.length, 2);
  assert.equal(context.window.QueryflowECharts.buildOption(make(30, false)).dataZoom.length, 2);
  assert.equal(context.window.QueryflowECharts.buildOption({...make(30, false), chart_type: 'horizontal_bar'}).dataZoom.length, 2);
  assert.ok(!fs.readFileSync(`${__dirname}/../web/echarts_view.js`, 'utf8').includes('spec.chart_type !== "horizontal_bar"'));
  const multi = make(26, true);
  multi.chart_type = 'multi_line'; multi.series = 'Country';
  multi.data = multi.data.flatMap(row => [{...row, Country: 'DE'}, {...row, Country: 'FR'}]);
  assert.equal(context.window.QueryflowECharts.buildOption(multi).dataZoom.length, 2);
  const zoomed = explorer.apply(make(26, true), {start: '2026-01-10', end: '2026-01-20'});
  assert.equal(zoomed.data.length, 11);
});
test('two-finger pinch zooms eligible time series without blocking ordinary touch scrolling', () => {
  const listeners = new Map(), removed = [];
  const classes = new Set();
  const container = {
    classList: {add: name => classes.add(name), remove: name => classes.delete(name)},
    addEventListener: (name, callback) => listeners.set(name, callback),
    removeEventListener: name => removed.push(name),
    getBoundingClientRect: () => ({left: 0, width: 300}),
  };
  let zoom = {start: 0, end: 100}, zoomHandler;
  const chart = {
    setOption() {}, resize() {}, dispose() {},
    getOption: () => ({dataZoom: [zoom]}),
    dispatchAction(action) { zoom = {start: action.start, end: action.end}; zoomHandler?.(); },
    on(name, callback) { if (name === 'datazoom') zoomHandler = callback; },
  };
  context.window.echarts = {init: () => chart};
  context.window.addEventListener = () => {};
  context.window.removeEventListener = () => {};
  const spec = {chart_type: 'line', x: 'Date', y: 'Value', time_format: 'day',
    data: Array.from({length: 26}, (_, i) => ({
      Date: new Date(Date.UTC(2026, 0, i + 1)).toISOString().slice(0, 10), Value: i,
    }))};
  const mounted = context.window.QueryflowECharts.render(container, {chart_spec: spec});
  let observedZoom;
  mounted.onZoom(value => { observedZoom = value; });
  assert.ok(classes.has('chart-slot-pinch'));
  let prevented = false;
  listeners.get('touchmove')({touches: [{clientX: 50, clientY: 0}, {clientX: 250, clientY: 0}],
    preventDefault() { prevented = true; }, stopPropagation() {}});
  assert.equal(prevented, false, 'single-finger or unstarted gestures must not be intercepted');
  listeners.get('touchstart')({touches: [{clientX: 100, clientY: 0}, {clientX: 200, clientY: 0}]});
  listeners.get('touchmove')({touches: [{clientX: 50, clientY: 0}, {clientX: 250, clientY: 0}],
    preventDefault() { prevented = true; }, stopPropagation() {}});
  assert.equal(prevented, true);
  assert.equal(Math.round(observedZoom.start), 25);
  assert.equal(Math.round(observedZoom.end), 75);
  listeners.get('touchend')({touches: []});
  mounted.dispose();
  assert.equal(classes.has('chart-slot-pinch'), false);
  assert.deepEqual(removed.sort(), ['touchcancel', 'touchend', 'touchmove', 'touchstart']);
  const shortListeners = new Map();
  const shortContainer = {...container, addEventListener: (name, callback) => shortListeners.set(name, callback)};
  context.window.QueryflowECharts.render(shortContainer, {chart_spec: {...spec, data: spec.data.slice(0, 2)}}).dispose();
  assert.equal(shortListeners.size, 0, 'one- or two-point charts retain normal touch behavior');
});
test('chart UI stays within the response; visual controls do not call query APIs', () => {
  assert.ok(markup.includes('class="chart-kinds"'));
  assert.ok(!markup.includes('class="chart-suggestions"'));
  assert.ok(!markup.includes('class="backend-suggestions"'));
  assert.ok(!markup.includes('class="chart-filter-panel"'));
  assert.ok(markup.includes('class="chart-reset-zoom"'));
  const chartWiring = source.slice(source.indexOf('function wireChartSection('), source.indexOf('function toast('));
  assert.ok(!chartWiring.includes('await api('));
  assert.ok(!chartWiring.includes('send('));
});
test('conversation spacing is compact and chart switching stays in the header', () => {
  assert.ok(styles.includes('.user-message { margin-bottom: 12px; }'));
  assert.ok(styles.includes('margin-top: 8px; padding: 4px'));
  const wiring = source.slice(source.indexOf('function wireChartSection('), source.indexOf('function toast('));
  assert.ok(wiring.includes('kinds.append(button)'));
  assert.ok(!wiring.includes('section.querySelector(".chart-suggestions")'));
  const answer = source.slice(source.indexOf('async function appendAssistant('), source.indexOf('function actionButton('));
  assert.ok(!answer.includes('renderBackendSuggestions('));
});
test('chart filter and parameter controls are removed while zoom remains', () => {
  assert.ok(source.includes('item.db_id === uiConfig.default_db_id'));
  assert.ok(!markup.includes('chart-query-parameters'));
  assert.ok(!markup.includes('chart-parameter-chip'));
  assert.ok(markup.includes('chart-reset-zoom'));
  assert.ok(source.includes('article._echart?.resetZoom?.()'));
});

vm.runInContext(source.slice(source.indexOf('function renderTable('), source.indexOf('function downloadCsv(')), context);
test('result table renders headers, rows and NULL explicitly', () => {
  const container = new Element();
  context.renderTable(container, ['customer', 'amount'], [['Alice', null]]);
  const table = container.children[0];
  assert.equal(table.children[0].children[0].children[0].textContent, 'customer');
  const cells = table.children[1].children[0].children;
  assert.equal(cells[0].textContent, 'Alice');
  assert.equal(cells[1].children[0].textContent, 'NULL');
});

vm.runInContext(source.slice(source.indexOf('function visiblePipelineStage('), source.indexOf('async function send(')), context);
test('progress supports recovery tools without marking unobserved stages complete', () => {
  const classes = new Map();
  const stages = ['retrieval', 'generation', 'recovery'].map(stage => ({
    dataset: { stage },
    classList: { toggle(name, enabled) { classes.set(`${stage}:${name}`, enabled); } },
  }));
  const progress = { dataset: {}, querySelectorAll: () => stages };
  context.updatePipeline(progress, 'generation', 'running');
  context.updatePipeline(progress, 'recovery_values', 'running');
  assert.equal(classes.get('recovery:active'), true);
  assert.equal(classes.get('generation:complete'), true);
  assert.equal(classes.get('retrieval:complete'), false);
});

test('progress colors fast stages captured only in backend event history', () => {
  const classes = new Map();
  const stages = ['conversation', 'retrieval', 'grounding', 'generation'].map(stage => ({
    dataset: { stage },
    classList: { toggle(name, enabled) { classes.set(`${stage}:${name}`, enabled); } },
  }));
  const progress = { dataset: {}, querySelectorAll: () => stages };
  context.updatePipeline(progress, 'generation', 'running', [
    { stage: 'conversation' },
    { stage: 'retrieval' },
    { stage: 'grounding' },
    { stage: 'generation' },
  ]);
  assert.equal(classes.get('conversation:complete'), true);
  assert.equal(classes.get('grounding:complete'), true);
  assert.equal(classes.get('generation:active'), true);
});
test('successful result colors review and marks unused recovery as not needed', () => {
  const classes = new Map();
  const stages = ['execution', 'result_review', 'recovery'].map(stage => ({
    dataset: { stage },
    classList: { toggle(name, enabled) { classes.set(`${stage}:${name}`, enabled); } },
  }));
  const progress = { dataset: {}, querySelectorAll: () => stages };
  context.updatePipeline(progress, 'completed', 'completed', [
    { stage: 'execution' },
  ], { generation: { accepted: true } });
  assert.equal(classes.get('result_review:complete'), true);
  assert.equal(classes.get('recovery:skipped'), true);
  assert.equal(classes.get('recovery:complete'), false);
});
vm.runInContext(source.slice(source.indexOf('function usageTotal('), source.indexOf('function renderAttempts(')), context);
for (const [name, body, generation, expected] of [
  ['successful result', { token_usage: { input_tokens: 10, output_tokens: 5 } }, { accepted: true, attempts: [] }, '15'],
  ['failure without usage', {}, { accepted: false, attempts: [] }, 'Unavailable'],
  ['generation usage fallback', {}, { token_usage: { input_tokens: 7, output_tokens: 2 } }, '9'],
]) {
  test(`token rendering: ${name}`, () => {
    const elements = new Map();
    const fragment = { querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, new Element());
      return elements.get(selector);
    } };
    context.renderTokenAccounting(fragment, body, generation);
    assert.equal(elements.get('.token-grid').children[0].children[1].textContent, expected);
  });
}

vm.runInContext(source.slice(
  source.indexOf('const VIZ_REQUEST_PATTERN'),
  source.indexOf('function mergeRunChanges('),
), context);
context.EXPLANATION_OPERATIONS_UI = new Set([
  'EXPLAIN', 'EXPLAIN_SQL', 'EXPLAIN_RESULT', 'EXPLAIN_INTERPRETATION', 'EXPLAIN_CONTEXT', 'EXPLAIN_FAILURE',
]);
test('query understanding chips show only fields from the confirmed semantic contract', () => {
  const strip = new Element();
  strip.hidden = true;
  context.renderSemanticChips({querySelector: () => strip}, {
    state: { semantic_contract: {
      measures: ['total_spending'], grain: ['CustomerID'],
      filters: [{operand:'year', operator:'=', value:'2025'}, {operand:'country', operator:'=', value:'DE'}],
    } },
  });
  assert.equal(strip.hidden, false);
  assert.equal(strip.children.length, 5); // eyebrow + metric, period, grain and filter
  assert.equal(strip.children[1].children[0].textContent, 'Metric');
  assert.equal(strip.children[1].children[1].textContent, 'Total spending');
  assert.equal(strip.children[2].children[0].textContent, 'Period');
  assert.equal(strip.children[2].children[1].textContent, '2025');
  assert.equal(strip.children[3].children[0].textContent, 'Grain');
  assert.equal(strip.children[3].children[1].textContent, 'Customer');
  assert.equal(strip.children[4].children[1].textContent, 'Country: DE');
  assert.equal(strip.children[1].children[2].getAttribute?.('data-icon') || strip.children[1].children[2]['data-icon'], 'chevron-right');
  assert.ok(strip.children[1].listeners.click);
  const empty = new Element(); empty.hidden = true;
  context.renderSemanticChips({querySelector: () => empty}, {state:null});
  assert.equal(empty.hidden, true);
});
test('Understood as matches the v4 position and compact chip treatment', () => {
  const template = markup.split('<template id="assistantTemplate">')[1].split('</template>')[0];
  assert.ok(template.indexOf('class="result-head"') < template.indexOf('class="query-semantic-strip"'));
  assert.ok(template.indexOf('class="query-semantic-strip"') < template.indexOf('class="response-tabs"'));
  assert.ok(styles.includes('.query-semantic-chip { border:1px solid #e6e4ef; border-radius:8px'));
  assert.ok(styles.includes('max-width:210px'));
  assert.ok(styles.includes('.query-semantic-strip[hidden] { display:none; }'));
  const strip = new Element(); strip.hidden = true;
  context.renderSemanticChips({querySelector: () => strip}, {
    generation: {accepted: false}, state: {semantic_contract: {measures: ['revenue']}},
  });
  assert.equal(strip.hidden, true, 'a failed query must not show accepted understanding');
});
test('Answer keeps a single KPI but never duplicates a result table', () => {
  const preview = new Element(); preview.hidden = true;
  const fragment = {querySelector: () => preview};
  context.renderAnswerPreview(fragment, {generation: {
    accepted:true, columns:['customer', 'spending'], rows:[['A', 120], ['B', 90]], row_count:2,
  }}, 'sql');
  assert.equal(preview.hidden, true);
  assert.equal(preview.children.length, 0);
  const scalar = new Element(); scalar.hidden = true;
  context.renderAnswerPreview({querySelector: () => scalar}, {generation: {
    accepted:true, columns:['total_monthly_consumption'], rows:[[3415480125.73]], row_count:1,
  }}, 'sql');
  assert.equal(scalar.hidden, false);
  assert.equal(scalar.children[0].textContent, 'Total monthly consumption');
  assert.equal(scalar.children[1].textContent, '3,415,480,125.73');
});
test('recovery diagnosis is shown once when it matches the answer text', () => {
  const helper = source.slice(source.indexOf('function sameDisplayText('), source.indexOf('function updateStatusPill('));
  const sameDisplayText = vm.runInNewContext(`${helper}; sameDisplayText`);
  const diagnosis = 'The customer ID 7657657 does not exist in the customers table, so no information can be shown for it.';
  assert.equal(sameDisplayText(diagnosis, diagnosis), true);
  assert.equal(sameDisplayText(diagnosis, `  ${diagnosis.slice(0, -1)}  `), true);
  assert.equal(sameDisplayText(diagnosis, 'The customer has no transactions.'), false);
  const answer = source.slice(source.indexOf('async function appendAssistant('), source.indexOf('function actionButton('));
  assert.ok(answer.includes('!sameDisplayText(responseMessage, diagnosis)'));
  assert.ok(answer.includes('!sameDisplayText(body.human_review?.reason, diagnosis)'));
  assert.ok(answer.includes('!body.human_review && !sameDisplayText(responseMessage, failure)'));
  assert.ok(answer.includes('renderHumanReview(fragment, body.human_review, responseMessage)'));
});
test('chart adapter exposes click events for investigate actions', () => {
  let handlerName = null, selectedHandler = null;
  const fakeChart = {
    setOption() {}, resize() {}, dispose() {},
    on(name, handler) { handlerName = name; selectedHandler = handler; },
  };
  context.window.echarts = {init: () => fakeChart};
  context.window.addEventListener = () => {};
  context.window.removeEventListener = () => {};
  const spec = {chart_type: 'line', x: 'Month', y: 'Revenue',
    data: [{Month:'Jan', Revenue:4}, {Month:'Feb', Revenue:5}]};
  const mounted = context.window.QueryflowECharts.render(new Element(), {chart_spec:spec});
  let selected;
  const onClick = point => { selected = point; };
  mounted.onSelect(onClick);
  assert.equal(handlerName, 'click');
  selectedHandler({dataIndex: 1, name: 'Feb'});
  assert.equal(selected.rawX, 'Feb');
  mounted.dispose();
});
test('formatted date click carries the original month key', () => {
  let handler;
  const chart = {setOption() {}, resize() {}, dispose() {}, on(_name, callback) { handler = callback; }};
  context.window.echarts = {init: () => chart};
  const spec = {chart_type: 'line', x: 'Month', y: 'Revenue', time_format: 'month',
    data: [{Month:'2026-02', Revenue:2}, {Month:'2026-01', Revenue:1}]};
  const mounted = context.window.QueryflowECharts.render(new Element(), {chart_spec: spec});
  let selected;
  mounted.onSelect(point => { selected = point; });
  handler({dataIndex: 0, name: 'Jan 2026'});
  assert.equal(selected.rawX, '2026-01');
  mounted.dispose();
});
test('grouped chart click keeps the raw date and series after display filtering', () => {
  let handler;
  const chart = {setOption() {}, resize() {}, dispose() {}, on(_name, callback) { handler = callback; }};
  context.window.echarts = {init: () => chart};
  const original = {chart_type: 'multi_line', x: 'Month', y: 'Revenue', series: 'Country',
    time_format: 'month', data: [
      {Month:'2026-01', Country:'DE', Revenue:1}, {Month:'2026-01', Country:'FR', Revenue:2},
      {Month:'2026-02', Country:'DE', Revenue:3}, {Month:'2026-02', Country:'FR', Revenue:4},
    ]};
  const filtered = explorer.apply(original, {categories:['FR']});
  const mounted = context.window.QueryflowECharts.render(new Element(), {chart_spec:filtered});
  let selected;
  mounted.onSelect(point => { selected = point; });
  handler({dataIndex: 1, name: 'Feb 2026', seriesName: 'FR'});
  assert.equal(selected.rawX, '2026-02');
  assert.equal(selected.seriesName, 'FR');
  mounted.dispose();
});
test('data scope and tab switching do not expose query parameters', () => {
  assert.ok(markup.includes('class="data-source-tab"'));
  assert.ok(markup.includes('class="data-visible-tab"'));
  assert.ok(!markup.includes('class="chart-apply-query"'));
  const scope = source.slice(source.indexOf('function wireDataScope('), source.indexOf('function wireChartSection('));
  assert.ok(!scope.includes('sendFromAnswer('));
  assert.ok(!scope.includes('api('));
  const chart = source.slice(source.indexOf('function wireChartSection('), source.indexOf('function toast('));
  assert.ok(!chart.includes('await sendFromAnswer(runId,'));
});
test('scatter-point investigation uses the observed x value when ECharts omits its name', () => {
  const observations = [];
  context.insightMenu = (_anchor, heading, _description, _items, runId) => {
    observations.push({heading, runId});
  };
  const article = {
    _echart: { onSelect(callback) { this.select = callback; } },
    querySelector: () => new Element(),
  };
  const presentation = {chart_spec: {
    chart_type: 'scatter', x: 'age', y: 'spend',
    data: [{age: 32, spend: 104}, {age: 44, spend: 200}],
  }};
  context.attachChartInvestigations(article, presentation, 'source-run');
  article._echart.select({name: '', value: [32, 104]});
  assert.deepEqual(observations, [{heading: 'age: 32', runId: 'source-run'}]);
});
test('combined Year-Month chart click drills into original columns, not a synthetic SQL field', () => {
  let options;
  context.insightMenu = (_anchor, _heading, _description, items) => { options = items; };
  const article = {_echart: {onSelect(callback) { this.select = callback; }},
    querySelector: () => new Element()};
  context.attachChartInvestigations(article, {chart_spec: {
    chart_type: 'line', x: 'Year-Month', x_components: ['Year', 'Month'], y: 'Revenue',
    data: [{Year: 2025, Month: 12, 'Year-Month': '2025-12', Revenue: 10}],
  }}, 'source-run');
  article._echart.select({rawX:'2025-12'});
  assert.match(options[0].prompt, /"Year" = 2025 and "Month" = 12/);
  assert.ok(!options[0].prompt.includes('"Year-Month" ='));
});
test('historical investigation restores its source run before sending', async () => {
  const calls = [];
  context.state = {activeRunId: 'latest'};
  context.continueRun = async id => { calls.push('restore:' + id); context.state.activeRunId = id; };
  context.send = async message => { calls.push('send:' + message); };
  await context.sendFromAnswer('earlier', 'Compare these customers');
  assert.deepEqual(calls, ['restore:earlier', 'send:Compare these customers']);
});
test('explicit visualization language is detected; ordinary requests are not', () => {
  assert.equal(context.vizRequested('Plot monthly revenue as a line chart'), true);
  assert.equal(context.vizRequested('make it a bar chart'), true);
  assert.equal(context.vizRequested('Show a histogram of order totals'), true);
  assert.equal(context.vizRequested('Which customers spent the most in 2025?'), false);
  assert.equal(context.vizRequested('Only Germany and use 2024'), false);
  assert.equal(context.vizRequested(undefined, ''), false);
});
test('response modes hide charts for ordinary queries and offer them only when requested', () => {
  const acceptedTable = { generation: { accepted: true }, presentation: { chart_type: 'none' } };
  assert.equal(context.responseMode(acceptedTable, { viz: false }), 'sql');
  assert.equal(context.responseMode({...acceptedTable, presentation: {chart_spec: {chart_type: 'bar'}}}), 'chart');
  assert.equal(
    context.responseMode({ generation: { accepted: true }, presentation: { chart_type: 'none' } }, { viz: true }),
    'chart_unavailable',
  );
  assert.equal(
    context.responseMode({ operation: 'EXPLAIN_RESULT', generation: { accepted: true } }, { viz: false }),
    'explanation',
  );
  assert.equal(context.responseMode({ generation: { accepted: false } }, { viz: true }), 'sql');
});

test('backend presentation state overrides client-side heuristics', () => {
  const chartSpec = { chart_type: 'bar', data: [{ country: 'DE', count: 2 }] };
  assert.equal(
    context.presentationMode({ presentation_state: { mode: 'chart' }, presentation: { chart_spec: chartSpec } }),
    'chart',
  );
  assert.equal(
    context.presentationMode({ presentation_state: { mode: 'chart' }, presentation: { chart_type: 'bar' } }),
    'chart_unavailable',
  );
  assert.equal(
    context.presentationMode({ presentation_state: { mode: 'sql' }, generation: { accepted: true } }),
    'sql',
  );
  assert.equal(
    context.presentationMode({
      operation: 'EXPLAIN_RESULT',
      presentation_state: { mode: 'sql' },
      generation: { accepted: true },
    }),
    'explanation',
  );
  // Legacy payloads (saved before the contract existed) keep the fallback.
  assert.equal(
    context.presentationMode({ generation: { accepted: true }, presentation: { chart_type: 'none' } }, { viz: true }),
    'chart_unavailable',
  );
});
vm.runInContext(source.slice(source.indexOf('function mergeRunChanges('), source.indexOf('async function appendAssistant(')), context);
test('consecutive same-kind diff edits are merged for display', () => {
  const merged = [...context.mergeRunChanges([
    { kind: 'remove', token: 'COUNT' },
    { kind: 'remove', token: '(*)' },
    { kind: 'insert', token: 'COUNT(*) + 1' },
    { kind: 'insert', token: '' },
    { kind: 'remove', token: 'orders' },
  ])].map((item) => [item.kind, item.token]);
  assert.deepEqual(merged, [
    ['remove', 'COUNT (*)'],
    ['insert', 'COUNT(*) + 1'],
    ['remove', 'orders'],
  ]);
});

test('recovery timeline reflects the real trace and stays honest about outcomes', () => {
  const recovery = {
    activated: true,
    mode: 'ZERO_RESULT',
    failure_code: 'SQL_SUCCESS_ZERO_ROWS',
    tool_calls: [
      { tool: 'inspect_values', purpose: 'Check stored values', result_summary: 'Spain not found', evidence_id: 'E1' },
      { tool: 'query_database', purpose: 'Count probe', result_summary: '0 matching', evidence_id: 'E2' },
    ],
    claims: [{ claim_type: 'VALUE_ABSENT', statement: 'Spain does not exist in customers', evidence_ids: ['E1'] }],
  };
  const steps = context.recoveryTimelineSteps({ ...recovery, agent_action: 'INFORM' });
  assert.equal(steps.length, 5);
  assert.match(steps[0].title, /Initial/);
  assert.equal(steps[1].evidenceId, 'E1');
  assert.equal(steps[4].title, 'Agent decision · INFORM');
  assert.ok(steps[0].detail.includes('ZERO_ROWS'));

  const repaired = context.recoveryTimelineSteps({
    ...recovery, agent_action: 'REPAIR', repair_instruction: 'Remove the country filter.',
  });
  assert.equal(repaired[4].title, 'Agent decision · REPAIR');
  assert.ok(repaired[4].detail.includes('Remove the country filter.'));

  assert.equal(context.recoveryTimelineSteps({ activated: false }).length, 0);
});

test('inspector recovery trace follows recorded signal, probes and decision', () => {
  const helper = source.slice(source.indexOf('function inspectorRecoverySteps('), source.indexOf('function renderInspectorTokenSummary('));
  const inspector = vm.runInNewContext(`${helper}; inspectorRecoverySteps`, {
    sameDisplayText: (left, right) => left === right,
  });
  const recovery = {
    activated: true, mode: 'ZERO_RESULT', failure_code: 'SQL_SUCCESS_ZERO_ROWS',
    tool_calls: [{tool:'inspect_values', purpose:'Check the customer identifier',
      result_summary:'Customer 999 was not found', evidence_id:'E1'}],
    claims: [{statement:'No matching customer was found', evidence_ids:['E1']}],
    diagnosis_summary:'Customer 999 has no matching record.', agent_action:'INFORM',
  };
  const steps = inspector(recovery);
  assert.deepEqual([...steps].map(step => step.title), [
    'Unmatched result detected', 'Value check', 'Evidence evaluated',
  ]);
  assert.match(steps[1].detail, /Customer 999 was not found/);
  assert.match(steps[1].meta, /Observation E1/);
  assert.match(steps[2].meta, /Outcome: INFORM/);
  assert.equal(inspector({...recovery, tool_calls: []}).length, 2);
  assert.equal(inspector({activated:false}).length, 0);
  assert.ok(styles.includes('.inspect-recovery-timeline'));
  assert.ok(styles.includes('.inspect-recovery-step:not(:last-child)::before'));
  assert.ok(!helper.includes('Illustrative'));
});

test('recovery presentation uses backend event IDs and keeps claims as interpretations', () => {
  const result = context.recoveryTimelineSteps({activated: true}, [{
    event_id: 'turn-event-1', event_type: 'CLAIM_REPORTED', status: 'REPORTED',
    summary: 'A reported claim', evidence_ids: ['obs-7'], detail: 'SCHEMA_FACT',
  }]);
  assert.equal(result.length, 1);
  assert.equal(result[0].title, 'Agent interpretation');
  assert.equal(result[0].evidenceId, 'obs-7');
  assert.equal(result[0].observation, 'SCHEMA_FACT');
});

vm.runInContext(source.slice(
  source.indexOf('function summarizeRun('),
  source.indexOf('async function refreshHistory('),
), context);
test('history summaries expose the original question and truthful status', () => {
  const summary = context.summarizeRun({
    original_message: 'How many orders per customer?',
    resolved_question: 'How many orders per customer with Germany filter?',
    operation: 'ADD_FILTER',
    accepted: false,
    row_count: 0,
    created_at: 1_700_000_000,
  });
  assert.equal(summary.title, 'How many orders per customer?');
  assert.equal(summary.failed, true);
  assert.match(summary.subtitle, /ADD_FILTER · 0 rows · failed · /);
});

test('independent questions stay in the chat without a forced reset action', () => {
  assert.ok(!source.includes('actionButton("Start new conversation"'));
  assert.ok(source.includes('async function appendAssistant('));
});
