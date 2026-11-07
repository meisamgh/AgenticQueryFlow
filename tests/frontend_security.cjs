const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/app.js', 'utf8');

test('schema-derived action labels are text, never executable HTML', () => {
  const assigned = [];
  const document = {createElement(tag) {return {
    tag, children: [], textContent: '',
    set innerHTML(value) {assigned.push(value);},
    append(...children) {this.children.push(...children);},
    addEventListener() {},
  };}};
  const context = vm.createContext({document, icon: () => '<svg></svg>'});
  vm.runInContext(source.slice(source.indexOf('function actionButton('),
    source.indexOf('function latestAcceptedPreviousSql(')), context);
  const hostile = '<img src=x onerror=alert(1)>.column';
  const button = context.actionButton(hostile, 'table', () => {});
  assert.ok(!assigned.some(value => value.includes(hostile)));
  assert.equal(button.children[0].textContent, hostile);
});
