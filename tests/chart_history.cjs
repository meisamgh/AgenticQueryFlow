const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const app=fs.readFileSync('web/app.js','utf8');
function setup(){
  const context=vm.createContext({state:{},sessionId:'owner',window:{}});
  vm.runInContext(app.slice(app.indexOf('function chartMatchesResultIdentity('),app.indexOf('function isPlotOnlyTurn(')),context);
  vm.runInContext(app.slice(app.indexOf('function chartSnapshotKey('),app.indexOf('/* ---------- Theme, inspector, keyboard')),context);
  vm.runInContext(fs.readFileSync('web/chart_explorer.js','utf8'),context);
  return context;
}
function response(count=21,id='one'){
  const rows=Array.from({length:count},(_,i)=>[`C${i}`,i]);
  return {session_id:'owner',run_id:`run-${id}`,result_id:`result-${id}`,chart_id:`chart-${id}`,
    operation:'VISUALIZE',presentation_state:{mode:'chart',source_result_id:`result-${id}`},
    generation:{db_id:'shop',accepted:true,sql:`SELECT category, value FROM sales /*${id}*/`,
      columns:['category','value'],rows,row_count:count,truncated:false},
    presentation:{chart_spec:{chart_type:'bar',x:'category',y:'value',source_row_count:count,
      data:rows.map(row=>({category:row[0],value:row[1]}))}}};
}
function record(body){
  const payload=JSON.parse(JSON.stringify(body));
  payload.generation.rows=payload.generation.rows.slice(0,20);
  payload.presentation.chart_spec.data=payload.presentation.chart_spec.data.slice(0,20);
  return {db_id:'shop',run_id:body.run_id,row_count:body.generation.row_count,response_payload:payload};
}
for(const count of [19,20,21,100]) test(`reopening ${count}-row chart restores exact RAM data, not preview`,()=>{
  const ctx=setup(),body=response(count),saved=record(body),before=JSON.stringify(saved);
  ctx.rememberChartSnapshot(body);
  // A second conversation must not replace the first one's result.
  ctx.rememberChartSnapshot(response(21,'another'));
  const restored=ctx.restoredRunToResponse(saved);
  assert.equal(restored.generation.rows.length,count);
  assert.equal(restored.presentation.chart_spec.data.length,count);
  assert.equal(restored.result_id,body.result_id);
  assert.equal(restored.chart_id,body.chart_id);
  assert.equal(JSON.stringify(restored.generation.rows),JSON.stringify(body.generation.rows));
  assert.equal(JSON.stringify(saved),before);
  // New conversation resets analytical context, not previously displayed charts.
  ctx.rememberChartSnapshot({operation:'RESET_CONTEXT'});
  assert.equal(ctx.restoredRunToResponse(saved).generation.rows.length,count);
});
test('cache is bounded and cannot substitute another database, session, SQL or chart identity',()=>{
  const ctx=setup(),body=response(),saved=record(body);
  ctx.rememberChartSnapshot(body);
  for(const change of [s=>s.db_id='other',s=>s.response_payload.result_id='other',
    s=>s.response_payload.chart_id='other',s=>s.response_payload.generation.sql='SELECT secret']){
    const wrong=JSON.parse(JSON.stringify(saved));change(wrong);
    assert.equal(ctx.restoredRunToResponse(wrong).generation.rows.length,20);
  }
  ctx.sessionId='another-session';
  assert.equal(ctx.restoredRunToResponse(saved).generation.rows.length,20);
  ctx.sessionId='owner';
  for(let i=0;i<101;i++)ctx.rememberChartSnapshot(response(21,String(i)));
  assert.equal(ctx.state.chartSnapshots.size,100);
  assert.equal(ctx.restoredRunToResponse(saved).generation.rows.length,20);
  ctx.state.chartSnapshots.clear();
  assert.equal(ctx.restoredRunToResponse(saved).generation.rows.length,20);
});
test('failed, mismatched and incomplete results never overwrite complete display snapshots',()=>{
  const ctx=setup(),body=response(),saved=record(body);
  ctx.rememberChartSnapshot(body);
  for(const change of [b=>b.generation.accepted=false,b=>b.generation.truncated=true,
    b=>b.generation.rows.pop(),b=>b.presentation_state.source_result_id='wrong']){
    const bad=JSON.parse(JSON.stringify(body));change(bad);ctx.rememberChartSnapshot(bad);
    assert.equal(ctx.restoredRunToResponse(saved).generation.rows.length,21);
  }
});
test('chart selector keeps recommendation order across repeated local type switches',()=>{
  const explorer=setup().window.QueryflowChartExplorer;
  const spec={chart_type:'bar',x:'category',y:'value',time_format:'raw',
    data:[{category:1,value:10},{category:2,value:20}]};
  const preferred=['bar','treemap','horizontal_bar'];
  const order=Array.from(explorer.orderedChoices(spec,preferred,spec.chart_type));
  for(const chart_type of ['treemap','horizontal_bar','scatter','bar']){
    const next=explorer.apply(spec,{chartType:chart_type});
    assert.ok(next);
    assert.deepEqual(Array.from(explorer.orderedChoices(next,preferred,spec.chart_type)),order);
  }
  assert.ok(order.includes('scatter') && order.includes('bar'));
});
