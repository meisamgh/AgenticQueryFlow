// Application-entry checks complement isolated helper tests; not pixel/browser QA.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '../web');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');

test('every shipped local entry script exists, parses and loads only once', () => {
  const scripts = [...html.matchAll(/<script\b[^>]*src="\/static\/([^"?]+)(?:\?[^\"]*)?"/g)]
    .map(match => match[1]);
  assert.ok(scripts.includes('app.js'));
  assert.equal(new Set(scripts).size, scripts.length);
  for (const script of scripts) {
    assert.ok(fs.existsSync(path.join(root, script)), `Missing shipped script: ${script}`);
    new vm.Script(fs.readFileSync(path.join(root, script), 'utf8'), {filename: script});
  }
});

test('removed presentation features have no shipped mounts, scripts or dead hooks', () => {
  for (const token of ['chart-change-badge', 'chart-zoom-guide', 'chart-zoom-actions',
    'query-semantic-strip', 'attempts-panel']) assert.ok(!html.includes(token), token);
  for (const script of ['query_filters.js', 'chart_change_bubble.js'])
    assert.ok(!html.includes(`/static/${script}`), script);
  for (const hook of ['function renderAttempts(', 'function isChartMode(',
    'state.lastPresentationMode', '#dataNavigation', '#homeNavigation',
    '.chart-reset-zoom', '.chart-change-badge']) assert.ok(!app.includes(hook), hook);
});

test('fixed-ID click bindings in the app refer to shipped elements', () => {
  const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]));
  const bindings = [...app.matchAll(/\$\("#([\w-]+)"\)\??\.addEventListener\("click"/g)];
  assert.ok(bindings.length > 5);
  for (const [, id] of bindings) assert.ok(ids.has(id), `Unreachable click binding: ${id}`);
});
