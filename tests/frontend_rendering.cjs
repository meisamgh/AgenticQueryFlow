// Run with node --test tests/frontend_rendering.cjs. No browser dependencies.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(`${__dirname}/../web/app.js`, 'utf8');
class Element {
  constructor() { this.children = []; }
  append(...children) { this.children.push(...children); }
  createTHead() { const child = new Element(); this.append(child); return child; }
  createTBody() { return this.createTHead(); }
  insertRow() { return this.createTHead(); }
  insertCell() { return this.createTHead(); }
  classList = { add() {} };
  setAttribute(name, value) { this[name] = value; }
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
context.EXPLANATION_OPERATIONS_UI = new Set([
  'EXPLAIN', 'EXPLAIN_SQL', 'EXPLAIN_RESULT', 'EXPLAIN_INTERPRETATION', 'EXPLAIN_CONTEXT', 'EXPLAIN_FAILURE',
]);
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
