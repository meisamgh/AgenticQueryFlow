const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = vm.createContext({window:{}});
vm.runInContext(fs.readFileSync('web/kpi_cards.js','utf8'), context);
const build = context.window.QueryflowKpis.build;
const response = () => ({result_id:'result-1', generation:{accepted:true, truncated:false,
  columns:['month','average_revenue'], rows:[['Jan',10],['Feb',20],['Mar',30]], row_count:3}});
const chart = {y:'average_revenue', y_label:'Average revenue'};
test('KPI card shadows are soft green, unclipped and do not alter the responsive grid',()=>{
  const css=fs.readFileSync('web/styles.css','utf8');
  assert.ok(css.includes('box-shadow:0 4px 18px rgba(22,132,71,.10)'));
  assert.ok(css.includes('box-shadow:0 7px 24px rgba(22,132,71,.16)'));
  assert.ok(css.includes('transition:box-shadow 180ms ease,border-color 180ms ease'));
  assert.ok(css.includes('.chart-kpi-row, .chart-section, .answer-tab-panel { overflow:visible; }'));
  assert.ok(css.includes('grid-template-columns:repeat(auto-fit,minmax(160px,1fr))'));
});
test('KPIs describe complete result rows without summing preaggregated averages', () => {
  const body = response(), before = JSON.stringify(body);
  const cards = build(body, chart);
  assert.deepEqual(Array.from(cards, card => card.value), [20,10,30]);
  assert.ok(cards.every(card => card.evidence.result_id === 'result-1'));
  assert.ok(cards.every(card => !card.comparison && !card.growth));
  assert.ok(cards.every(card => card.calculation !== 'SUM'));
  assert.equal(JSON.stringify(body), before);
});
test('truncation, missing values, wrong result identity, and incomplete history suppress KPIs', () => {
  let body = response(); body.generation.truncated = true; assert.equal(build(body,chart).length,0);
  body=response(); body.generation.rows.pop(); assert.equal(build(body,chart).length,0);
  body=response(); body.generation.rows[0][1]=null; assert.equal(build(body,chart).length,0);
  body=response(); body.presentation_state={source_result_id:'different'};
  assert.equal(build(body,chart).length,0);
});
test('follow-up evidence retains its own identity and styling does not change KPI values', () => {
  const body=response(); body.result_id='result-2'; body.generation.rows[0][1]=40;
  const cards=build(body,chart);
  assert.ok(cards.every(card => card.evidence.result_id === 'result-2'));
  assert.equal(cards[0].value,30);
  assert.equal(JSON.stringify(cards),JSON.stringify(build(body,{...chart,chart_type:'bar',color:'red'})));
});
test('verified units format currency, percentages, and ratios without name guessing', () => {
  const format=context.window.QueryflowKpis.format;
  assert.ok(format(1234.5,'EUR').includes('€'));
  assert.ok(format(12.5,'%').includes('12.5%'));
  assert.ok(format(0.125,'ratio').includes('12.5%'));
  assert.ok(!format(1234.5).includes('€'));
});
test('sparklines sort verified periods; growth requires explicit positive baseline', () => {
  const body=response();
  body.generation.rows=[['202503',30],['202501',10],['202502',20]];
  body.generation.semantic_contract={comparison_periods:['202501','202503']};
  const spec={...chart,x:'month',time_format:'month',measure_units:{average_revenue:'EUR'}};
  const cards=build(body,spec);
  assert.equal(JSON.stringify(cards[0].sparkline),'[10,20,30]');
  const growth=cards.find(card=>card.calculation==='PERIOD_CHANGE');
  assert.equal(growth.value,200);
  assert.equal(growth.comparison.baseline,'202501');
  body.generation.rows[1][1]=0;
  assert.ok(!build(body,spec).some(card=>card.calculation==='PERIOD_CHANGE'));
  body.generation.rows[1][1]=10;
  body.generation.semantic_contract.comparison_periods=['202401','202503'];
  assert.ok(!build(body,spec).some(card=>card.calculation==='PERIOD_CHANGE'));
});
test('additive KPI requires approved SUM and matching stage at unique temporal grain', () => {
  const body=response(); body.generation.rows=[['202501',10],['202502',20],['202503',30]];
  body.generation.semantic_contract={approved_metrics:[{id:'average_revenue',formula:'SUM(orders.amount)'}],
    aggregation_stages:[{name:'average_revenue',function:'sum',input:'orders.amount',group_by:['month']}]};
  const spec={...chart,x:'month',time_format:'month'};
  const cards=build(body,spec);
  assert.equal(cards[0].calculation,'SUM_DISJOINT_PERIODS'); assert.equal(cards[0].value,60);
  body.generation.semantic_contract.approved_metrics[0].formula='AVG(orders.amount)';
  assert.ok(!build(body,spec).some(card=>card.calculation==='SUM_DISJOINT_PERIODS'));
  body.generation.semantic_contract.approved_metrics[0].formula='SUM(orders.amount)';
  body.generation.rows[1][0]='202501';
  assert.ok(!build(body,spec).some(card=>card.calculation==='SUM_DISJOINT_PERIODS'));
});
test('series cards retain exact series names for local chart highlighting', () => {
  const body=response(); body.generation.columns=['month','average_revenue','country'];
  body.generation.rows=[['202501',10,'Germany'],['202502',20,'Germany'],['202501',30,'France']];
  const cards=build(body,{...chart,x:'month',series:'country',time_format:'month'});
  assert.deepEqual(Array.from(cards,card=>[card.series_name,card.value]),[['Germany',20],['France',30]]);
});
test('card DOM has evidence, trend and click callback without network access', () => {
  class Node {
    constructor(){this.children=[];this.dataset={};this.attrs={};}
    append(...nodes){this.children.push(...nodes);}
    replaceChildren(){this.children=[];}
    setAttribute(name,value){this.attrs[name]=value;}
    addEventListener(name,handler){this[name]=handler;}
  }
  context.document={createElement:()=>new Node(),createElementNS:()=>new Node()};
  const body=response();body.generation.rows=[['202501',10],['202502',20],['202503',30]];
  const cards=build(body,{...chart,x:'month',time_format:'month'}), container=new Node();
  let clicked=null;
  context.window.QueryflowKpis.render(container,cards,spec=>{clicked=spec;});
  assert.equal(container.children.length,4);
  assert.equal(container.children[0].dataset.resultId,'result-1');
  assert.equal(container.children[0].children[1].children[1].children[1].attrs.viewBox,'0 0 160 30');
  assert.equal(container.children[0].children[1].children[1].children[0].children[0].textContent,'3 periods analyzed');
  assert.equal(container.children[0].children[0].children[0].className,'kpi-icon-bubble');
  assert.equal(container.children[0].children[2].className,'kpi-context-badge');
  assert.ok(container.children[0].title.includes('Exact value: 20'));
  container.children[0].click();assert.equal(clicked,cards[0]);
});
test('premium compact formatting respects explicit units and locale', () => {
  const format=context.window.QueryflowKpis.format;
  assert.equal(format(162640000,undefined,'en-US'),'162.64M');
  assert.equal(format(4172.66,undefined,'en-US'),'4.17K');
  assert.equal(format(390530000,undefined,'en-US'),'390.53M');
  assert.equal(format(4172.66,'EUR','en-US'),'€4.17K');
  assert.equal(format(0.125,'ratio','en-US'),'12.5%');
  assert.equal(format(4172.66,'kg','en-US'),'4.17K kg');
  assert.ok(format(4172.66,undefined,'de-DE').includes(','));
});
test('per-card bubble uses exact comparison evidence and does not borrow growth for extrema', () => {
  class Node{constructor(){this.children=[];this.dataset={};this.attrs={};}
    append(...nodes){this.children.push(...nodes);}replaceChildren(){this.children=[];}
    setAttribute(name,value){this.attrs[name]=value;}addEventListener(){}}
  context.document={createElement:()=>new Node(),createElementNS:()=>new Node()};
  const body=response();body.generation.rows=[['202501',10],['202502',20],['202503',30]];
  body.generation.semantic_contract={comparison_periods:['202501','202503']};
  const cards=build(body,{...chart,x:'month',time_format:'month'}),container=new Node();
  context.window.QueryflowKpis.render(container,cards);
  const growthIndex=cards.findIndex(card=>card.calculation==='PERIOD_CHANGE');
  const bubble=container.children[growthIndex].children[1].children[1];
  assert.equal(bubble.children[0].children[0].textContent,'↑ +200%');
  assert.ok(bubble.title.includes('Baseline (202501)'));
  assert.equal(bubble.children[1].attrs.viewBox,'0 0 160 30');
  const minIndex=cards.findIndex(card=>card.calculation==='MIN');
  assert.equal(container.children[minIndex].children[1].children[1].children[0].children[0].textContent,cards[minIndex].badge);
});
test('semantic icon mapping is deterministic and unknown meaning stays neutral', () => {
  const icon=context.window.QueryflowKpis.iconFor;
  for (const [metric,expected] of [['revenue','REVENUE'],['total_customers','CUSTOMERS'],
    ['orders','ORDERS'],['consumption','CONSUMPTION'],['unknown','NEUTRAL']]) {
    assert.equal(icon({metric,calculation:'VALUE'}),expected);
  }
  assert.equal(icon({metric:'revenue',calculation:'MIN'}),'MIN');
  assert.equal(icon({metric:'consumption',calculation:'MAX'}),'MAX');
  assert.equal(icon({metric:'revenue',calculation:'ROW_MEAN'}),'AVERAGE');
  assert.equal(icon({metric:'value',calculation:'PERIOD_CHANGE'}),'GROWTH');
  assert.equal(icon({metric:'value',calculation:'VALUE',unit:'%'}),'PERCENT');
});
test('chart-title comparison is neutral, evidence-bound and absent without valid baseline', () => {
  const body=response();body.generation.rows=[['202501',20],['202502',10],['202503',30]];
  const chartSpec={...chart,x:'month',time_format:'month'};
  assert.equal(context.window.QueryflowKpis.comparisonBadge(build(body,chartSpec)).text,'↑ +50%');
  body.generation.semantic_contract={comparison_periods:['202501','202502']};
  const badge=context.window.QueryflowKpis.comparisonBadge(build(body,chartSpec));
  assert.equal(badge.text,'↓ -50%');assert.equal(badge.result_id,'result-1');
  assert.ok(badge.description.includes('not necessarily better or worse'));
  body.generation.rows[0][1]=0;
  assert.equal(context.window.QueryflowKpis.comparisonBadge(build(body,chartSpec)),null);
  body.generation.rows[0][1]=-20;
  assert.equal(context.window.QueryflowKpis.comparisonBadge(build(body,chartSpec)),null);
});
test('extrema badges and chart points use exact original observations', () => {
  const body=response();body.generation.rows=[['202501',10],['202502',20],['202503',30]];
  const cards=build(body,{...chart,x:'month',time_format:'month'});
  const min=cards.find(card=>card.calculation==='MIN'), max=cards.find(card=>card.calculation==='MAX');
  assert.equal(min.point.x,'202501');assert.equal(max.point.x,'202503');
  assert.ok(max.badge.startsWith('Peak · '));
  const raw=build(response(),chart);assert.ok(raw.every(card=>card.sparkline.length===0));
});
test('Plotly KPI highlighting targets exact matching curve and point without requery', () => {
  const source=fs.readFileSync('web/plotly_view.js','utf8');
  let observed=null;
  const ctx=vm.createContext({disposed:false,spec:{chart_type:'line'},figure:{},
    container:{data:[{name:'Average revenue',customdata:['202501','202502'],y:[10,20]}]},
    plotly:{Fx:{hover:(container,matches)=>{observed=matches;}}}});
  const method=source.slice(source.indexOf('      highlightPoint(point) {'),source.indexOf('      highlightSeries(name) {'));
  vm.runInContext(`this.handle=({${method}})`,ctx);
  ctx.handle.highlightPoint({series_name:'Average revenue',x:'202502',y:20});
  assert.equal(JSON.stringify(observed),'[{"curveNumber":0,"pointNumber":1}]');
  observed=null;ctx.handle.highlightPoint({series_name:'other',x:'202502',y:20});
  assert.equal(observed,null);
});
