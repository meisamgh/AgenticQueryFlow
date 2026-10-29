const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
function setup(reduced=false) {
  const ctx=vm.createContext({window:{matchMedia:()=>({matches:reduced})},document:{documentElement:{}},getComputedStyle:()=>({getPropertyValue:()=>''})});
  for(const file of ['chart_theme','plotly_view','echarts_view']) vm.runInContext(fs.readFileSync(`web/${file}.js`,'utf8'),ctx);
  return ctx.window;
}
test('shared chart theme keeps categorical colors stable across source order and renderer',()=>{
  const w=setup(),spec={chart_type:'multi_line',x:'date',y:'value',series:'group',time_format:'raw',data:[{date:'A',value:5,group:'SME'},{date:'A',value:8,group:'LAM'}]};
  const original=JSON.stringify(spec);
  const first=w.QueryflowPlotly.buildFigure(spec),second=w.QueryflowPlotly.buildFigure({...spec,data:[...spec.data].reverse()});
  for(const trace of first.data) assert.equal(trace.line.color,second.data.find(item=>item.name===trace.name).line.color);
  const fallback=w.QueryflowECharts.buildOption(spec);
  for(const trace of first.data) assert.equal(trace.line.color,fallback.series.find(item=>item.name===trace.name).lineStyle.color);
  assert.equal(JSON.stringify(spec),original);
});
test('premium chart geometry preserves gaps, steps, raw values and reduced motion',()=>{
  const w=setup(true),spec={chart_type:'step_line',x:'label',y:'value',time_format:'raw',data:[{label:'A',value:1},{label:'B',value:null},{label:'C',value:9000000}]};
  const figure=w.QueryflowPlotly.buildFigure(spec);
  assert.equal(figure.data[0].line.shape,'hv');assert.equal(figure.data[0].line.width,3);
  assert.equal(figure.data[0].connectgaps,false);assert.equal(figure.data[0].y[1],null);
  assert.equal(figure.layout.transition.duration,0);assert.equal(figure.layout.xaxis.rangeslider.visible,false);
  const fallback=w.QueryflowECharts.buildOption(spec);
  assert.equal(fallback.animation,false);assert.equal(fallback.series[0].smooth,false);
  assert.equal(fallback.series[0].step,'end');
});
test('horizontal bars keep a numeric value axis and readable title spacing',()=>{
  const w=setup(),spec={chart_type:'horizontal_bar',x:'category',y:'amount',time_format:'raw',data:[{category:'Long category label',amount:100},{category:'Other',amount:20}]};
  const figure=w.QueryflowPlotly.buildFigure(spec);
  assert.equal(figure.layout.xaxis.type,'linear');
  assert.equal(figure.layout.xaxis.title.standoff,16);
  assert.ok(figure.layout.margin.b>=60);
});
test('single-series treemap choices preserve every tile and raw source value',()=>{
  const w=setup();vm.runInNewContext(fs.readFileSync('web/chart_explorer.js','utf8'),{window:w});
  const spec={chart_type:'bar',x:'category',y:'amount',time_format:'raw',data:[{category:'A',amount:100},{category:'B',amount:20}]};
  assert.ok(w.QueryflowChartExplorer.choices(spec).includes('treemap'));
  const figure=w.QueryflowPlotly.buildFigure({...spec,chart_type:'treemap'});
  assert.equal(JSON.stringify(figure.data[0].values),'[100,20]');
});
