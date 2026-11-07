const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/app.js', 'utf8');

test('CSV export neutralizes formula text and headers without mutating raw results', async () => {
  let blob, clicked = false, revoked;
  const context = vm.createContext({Blob, URL: {
    createObjectURL(value) {blob = value; return 'blob:export';},
    revokeObjectURL(value) {revoked = value;},
  }, document: {createElement() {return {click() {clicked = true;}};}}});
  vm.runInContext(source.slice(source.indexOf('function downloadCsv('),
    source.indexOf('function usageTotal(')), context);
  const columns = ['=Header', 'value'];
  const rows = [['=1+2', -12.5], ['+cmd', 2], ['-formula', 3], ['@SUM(A1)', 4],
    ['\tformula', 5], ['\rformula', 6], ['  =1+2', 7], ['line\rbreak', null],
    ['München', 'a,"b"'], ['normal', 0]];
  const original = JSON.stringify({columns, rows});
  context.downloadCsv(columns, rows);
  const csv = new TextDecoder('utf-8', {ignoreBOM: true}).decode(await blob.arrayBuffer());
  assert.ok(csv.startsWith('\uFEFF\'=Header,value\n'));
  for (const text of ["'=1+2,-12.5", "'+cmd,2", "'-formula,3", "'@SUM(A1),4",
    "'\tformula,5", '"\'\rformula",6', "'  =1+2,7", '"line\rbreak",',
    'München,"a,""b"""', 'normal,0']) assert.ok(csv.includes(text), text);
  assert.equal(JSON.stringify({columns, rows}), original);
  assert.equal(clicked, true);
  assert.equal(revoked, 'blob:export');
});
