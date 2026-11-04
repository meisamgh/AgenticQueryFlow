const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.listeners = {}; }
  append(node) { this.children.push(node); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
}
const context = vm.createContext({window: {}, document: {createElement: tag => new Element(tag)}});
vm.runInContext(fs.readFileSync(`${__dirname}/../web/design_review.js`, 'utf8'), context);
const proposal = {status: 'AWAITING_APPROVAL', objective: 'Monthly consumption', items: [
  {title: 'Consumption trend', chart_type: 'line', reason: 'Compare observed months'},
], supported_findings: ['21 complete observations'], missing_requirements: []};
function nodes(root) { return [root, ...root.children.flatMap(nodes)]; }
test('historical design is read-only and cannot build or approve on render', () => {
  let calls = 0;
  const card = context.window.QueryflowDesignReview.render(proposal, {confirm: () => calls++});
  assert.equal(calls, 0);
  assert.ok(nodes(card).some(n => n.textContent === 'Archived dashboard design'));
  assert.ok(!nodes(card).some(n => n.tag === 'button'));
  assert.ok(nodes(card).some(n => n.textContent?.includes('not built automatically')));
});
test('archived proposal text is safely rendered and terminal statuses have no actions', () => {
  const card = context.window.QueryflowDesignReview.render({
    ...proposal, status: 'FAILED', objective: '<script>bad()</script>',
  });
  assert.ok(nodes(card).some(n => n.textContent === '<script>bad()</script>'));
  assert.ok(!nodes(card).some(n => n.tag === 'button'));
});
