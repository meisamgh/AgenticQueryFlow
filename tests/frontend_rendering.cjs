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
test('database and connection live in the header; composer keeps voice controls but no model selectors', () => {
  const composer = markup.split('<form class="composer"')[1].split('</form>')[0];
  const header = markup.split('<header class="topbar">')[1].split('</header>')[0];
  for (const id of ['databaseSelect', 'connectionCard', 'healthStatus', 'sessionId']) {
    assert.ok(header.includes(`id="${id}"`), `${id} is missing from the header`);
    assert.ok(!composer.includes(`id="${id}"`), `${id} should not remain in the composer`);
  }
  for (const id of ['voiceButton', 'sendButton']) {
    assert.ok(composer.includes(`id="${id}"`), `${id} is missing from the composer`);
  }
  assert.ok(!composer.includes('sqlModelSelect'));
  assert.ok(!composer.includes('contextModelSelect'));
  assert.ok(!markup.includes('id="composerChips"'));
  assert.ok(!markup.includes('id="heroExamples"'));
  assert.ok(!markup.includes('Enter to send'));
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
test('inspector has separate context, schema, recovery and details tabs', () => {
  for (const tab of ['understanding', 'schema', 'recovery', 'validation']) {
    assert.ok(markup.includes(`data-inspector-tab="${tab}"`));
  }
  assert.ok(markup.includes('id="inspectorBackdrop"'));
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
  const answerRenderer = source.slice(source.indexOf('async function appendAssistant('), source.indexOf('function generationQuestion('));
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
  const answer = source.slice(source.indexOf('async function appendAssistant('), source.indexOf('function generationQuestion('));
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
test('time zoom counts unique chronological points and excludes ordinary categories', () => {
  const make = (count, time) => ({chart_type: time ? 'line' : 'bar', x: 'Period', y: 'Value',
    time_format: time ? 'day' : 'raw', data: Array.from({length: count}, (_, index) => ({
      Period: time ? new Date(Date.UTC(2026, 0, index + 1)).toISOString().slice(0, 10) : `Group ${index}`,
      Value: index + 1,
    }))});
  assert.equal(context.window.QueryflowECharts.buildOption(make(25, true)).dataZoom.length, 0);
  assert.equal(context.window.QueryflowECharts.buildOption(make(26, true)).dataZoom.length, 2);
  assert.equal(context.window.QueryflowECharts.buildOption(make(30, false)).dataZoom.length, 0);
  const multi = make(26, true);
  multi.chart_type = 'multi_line'; multi.series = 'Country';
  multi.data = multi.data.flatMap(row => [{...row, Country: 'DE'}, {...row, Country: 'FR'}]);
  assert.equal(context.window.QueryflowECharts.buildOption(multi).dataZoom.length, 2);
  const zoomed = explorer.apply(make(26, true), {start: '2026-01-10', end: '2026-01-20'});
  assert.equal(zoomed.data.length, 11);
});
test('chart UI stays within the response; visual controls do not call query APIs', () => {
  assert.ok(markup.includes('class="chart-suggestions"'));
  assert.ok(markup.includes('class="chart-filter-panel"'));
  assert.ok(markup.includes('class="chart-category-filter" multiple'));
  assert.ok(markup.includes('class="chart-reset-zoom"'));
  const chartWiring = source.slice(source.indexOf('function wireChartSection('), source.indexOf('function toast('));
  assert.ok(!chartWiring.includes('await api('));
  assert.ok(!chartWiring.includes('send('));
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
      measures: ['net_revenue'], grain: ['month'],
      filters: [{column:'country', operator:'=', value:'DE'}],
    } },
  });
  assert.equal(strip.hidden, false);
  assert.equal(strip.children.length, 4); // eyebrow + 3 chips
  assert.equal(strip.children[1].children[0].textContent, 'Metric');
  assert.equal(strip.children[1].children[1].textContent, 'net_revenue');
  assert.equal(strip.children[3].children[0].textContent, 'Filter');
  const empty = new Element(); empty.hidden = true;
  context.renderSemanticChips({querySelector: () => empty}, {state:null});
  assert.equal(empty.hidden, true);
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
  const onClick = () => {};
  mounted.onSelect(onClick);
  assert.equal(handlerName, 'click');
  assert.equal(selectedHandler, onClick);
  mounted.dispose();
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
