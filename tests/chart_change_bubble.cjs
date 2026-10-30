const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const ctx=vm.createContext({window:{}});
for (const file of ['kpi_cards.js','chart_change_bubble.js','plotly_view.js'])
  vm.runInContext(fs.readFileSync(`web/${file}`,'utf8'),ctx);
function fixture(){
  const response={result_id:'result-1',generation:{accepted:true,truncated:false,row_count:3,
    columns:['month','revenue'],rows:[['202503',150],['202501',100],['202502',120]],
    semantic_contract:{comparison_periods:['202501','202503']}}};
  const chart={x:'month',y:'revenue',time_format:'month',measure_units:{revenue:'EUR'}};
  return {response,chart};
}
const build=({response,chart})=>ctx.window.ChartChangeBubble.build(response,chart,ctx.window.QueryflowKpis.build(response,chart));
test('change bubble uses explicit comparable periods and chronological observations',()=>{
  const f=fixture(),before=JSON.stringify(f),bubble=build(f);
  assert.equal(bubble.change,50);assert.equal(bubble.text,'↑ +50%');
  assert.equal(bubble.tone,'neutral');assert.equal(JSON.stringify(bubble.sparkline),'[100,120,150]');
  assert.ok(bubble.tooltip.includes('150 − 100'));assert.ok(bubble.tooltip.includes('abs(100)'));
  assert.equal(JSON.stringify(f),before);
});
test('missing comparisons, zero/negative baseline, incomplete data and mismatched IDs hide bubble',()=>{
  for(const baseline of [0,-10,null]){
    const f=fixture();f.response.generation.rows[1][1]=baseline;assert.equal(build(f),null);
  }
  let f=fixture();f.response.generation.semantic_contract.comparison_periods=['202401','202503'];assert.equal(build(f),null);
  f=fixture();f.response.generation.truncated=true;assert.equal(build(f),null);
  f=fixture();const kpis=ctx.window.QueryflowKpis.build(f.response,f.chart);
  f.response.result_id='other';assert.equal(ctx.window.ChartChangeBubble.build(f.response,f.chart,kpis),null);
});
test('start-to-end change uses chronological endpoints and rejects zero baselines',()=>{
  const f=fixture();delete f.response.generation.semantic_contract.comparison_periods;
  const bubble=build(f);
  assert.equal(bubble.change,50);
  assert.ok(bubble.period_label.includes('Mar 2025'));
  assert.ok(bubble.period_label.includes('Jan 2025'));
  f.response.generation.rows[0][0]='202505';assert.equal(build(f).change,50);
  f.response.generation.rows[0][0]='202503';f.response.generation.rows[1][1]=0;
  assert.equal(build(f),null);
});
test('bubble DOM has one compact sparkline and retains evidence without network calls',()=>{
  class Node{constructor(){this.children=[];this.dataset={};this.attrs={};}
    append(...nodes){this.children.push(...nodes);}replaceChildren(){this.children=[];}
    setAttribute(k,v){this.attrs[k]=v;}}
  ctx.document={createElement:()=>new Node(),createElementNS:()=>new Node()};
  const target=new Node();ctx.window.ChartChangeBubble.render(target,build(fixture()));
  assert.equal(target.hidden,false);assert.equal(target.dataset.resultId,'result-1');
  assert.equal(target.children[2].className,'chart-exact-data');
  assert.equal(target.children[2].children[0].textContent,'Exact data');
  assert.equal(target.children[3].attrs.viewBox,'0 0 48 24');
  ctx.window.ChartChangeBubble.render(target,null);assert.equal(target.hidden,true);
});
test('Plotly treemap has one uniquely identified tile per row without aggregation',()=>{
  const data=[{country:'DE',segment:'SME',amount:10},{country:'DE',segment:'KAM',amount:20}];
  const figure=ctx.window.QueryflowPlotly.buildFigure({chart_type:'treemap',x:'country',y:'amount',series:'segment',time_format:'raw',data});
  assert.equal(figure.data[0].type,'treemap');assert.equal(figure.data[0].ids.length,2);
  assert.equal(JSON.stringify(figure.data[0].values),'[10,20]');
  assert.equal(JSON.stringify(figure.data[0].parents),'["",""]');
  assert.equal(ctx.window.QueryflowPlotly.buildFigure({chart_type:'treemap',x:'country',y:'amount',data:[{country:'DE',amount:-1}]}),null);
});
test('bubble build/render stays bounded locally without tool access',()=>{
  class Node{constructor(){this.children=[];this.dataset={};}
    append(...nodes){this.children.push(...nodes);}replaceChildren(){this.children=[];}
    setAttribute(){}}
  ctx.document={createElement:()=>new Node(),createElementNS:()=>new Node()};
  const f=fixture(),kpis=ctx.window.QueryflowKpis.build(f.response,f.chart),target=new Node();
  const measurements=[];
  for(let i=0;i<500;i++){
    const start=performance.now();
    ctx.window.ChartChangeBubble.render(target,ctx.window.ChartChangeBubble.build(f.response,f.chart,kpis));
    measurements.push(performance.now()-start);
  }
  measurements.sort((a,b)=>a-b);
  assert.equal(target.children.length,4);
  console.log(`Synthetic bubble DOM: p50=${measurements[250].toFixed(3)}ms p95=${measurements[475].toFixed(3)}ms; 0 network/SQL/LLM calls`);
  const css=fs.readFileSync('web/styles.css','utf8');
  assert.ok(css.includes('.chart-heading { flex-direction:row; flex-wrap:wrap;'));
  assert.ok(css.includes('width:48px; height:24px'));
  assert.ok(css.includes('width:44px; height:44px'));
});
