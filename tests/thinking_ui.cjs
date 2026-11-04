const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(`${__dirname}/../web/app.js`, 'utf8');
function harness(jobs = []) {
  const nodes = new Map(); let writes = 0; let polls = 0;
  const progress = {dataset:{}, querySelector(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {dataset:{}, textContent:'', children:[], replaceChildren(){ writes++; this.children=[]; }, append(item){ this.children.push(item); }});
    return nodes.get(selector);
  }};
  const context = vm.createContext({icon:(name)=>`<svg>${name}</svg>`, document:{createElement:()=>({})},
    api:async()=>{polls++; return jobs.shift();}, window:{setTimeout:fn=>fn()}});
  vm.runInContext(source.slice(source.indexOf('const progressLabels ='), source.indexOf('function appendProgress(')) +
    source.slice(source.indexOf('function wait('), source.indexOf('function visiblePipelineStage(')), context);
  return {context, progress, nodes, writes:()=>writes, polls:()=>polls};
}
test('thinking shows real stages, compact recovery and elapsed time without rebuilding identical logs', () => {
  const h=harness(); const job={stage:'generation',status:'running',elapsed_ms:2500,events:[{stage:'conversation',elapsed_ms:0}]};
  h.context.updateThinking(h.progress,job);
  assert.equal(h.nodes.get('.progress-stage').textContent,'Preparing your query…');
  assert.equal(h.nodes.get('.progress-elapsed').textContent,'2s');
  assert.equal(h.nodes.get('.progress-completed').textContent,'1 earlier step · Understanding your question');
  h.context.updateThinking(h.progress,job); assert.equal(h.writes(),1);
  h.context.updateThinking(h.progress,{...job,stage:'recovery_values'});
  assert.equal(h.nodes.get('.progress-stage').textContent,'Investigating an issue…');
});
test('polling completion returns the real response without an animation delay', async()=>{
  const h=harness([{stage:'generation',status:'running',events:[]}, {stage:'completed',status:'completed',response:{result_id:'r1'},events:[]}]);
  assert.equal((await h.context.waitForJob('job',h.progress)).result_id,'r1');
  assert.equal(h.polls(),2);
});
for (const status of ['failed','cancelled']) test(`polling preserves ${status} handling`,async()=>{
  const h=harness([{stage:'execution',status,error:'Safe failure',events:[]}]);
  await assert.rejects(h.context.waitForJob('job',h.progress),status==='cancelled'?/cancelled/:/Safe failure/);
  assert.equal(h.polls(),1);
});
test('waiting is accessible, reduced-motion aware and has no speculative stage badges',()=>{
  const html=fs.readFileSync(`${__dirname}/../web/index.html`,'utf8');
  const template=html.slice(html.indexOf('<template id="progressTemplate">'),html.indexOf('</template>',html.indexOf('<template id="progressTemplate">')));
  assert.ok(template.includes('role="status" aria-live="polite"'));
  assert.ok(template.includes('aria-live="off"'));
  assert.ok(!template.includes('pipeline-tracker'));
  const css=fs.readFileSync(`${__dirname}/../web/styles.css`,'utf8');
  assert.match(css,/prefers-reduced-motion:reduce.*thinking-stage-avatar/);
  assert.ok(css.includes('@media (max-width:600px)'));
});
test('each executed stage selects a meaningful avatar without exposing raw stage names',()=>{
  const h=harness();
  for (const [stage, expected] of Object.entries({conversation:'brain',retrieval:'search',
    generation:'code',validation:'check',execution:'database',recovery_values:'binoculars',plotting:'chart-line',build:'columns'})) {
    h.context.updateThinking(h.progress,{stage,status:'running',events:[]});
    assert.equal(h.nodes.get('.thinking-stage-avatar').dataset.icon,expected);
  }
});
