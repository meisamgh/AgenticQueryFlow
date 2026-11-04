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
test('dense lines retain every point while hiding cluttered markers',()=>{
  const w=setup(),data=Array.from({length:60},(_,i)=>({period:`P${i}`,total_consumption:i===20 ? null : i+.123456}));
  const spec={chart_type:'line',x:'period',y:'total_consumption',time_format:'raw',data};
  const figure=w.QueryflowPlotly.buildFigure(spec);
  assert.equal(figure.data[0].mode,'lines');assert.equal(figure.data[0].y.length,60);
  assert.equal(figure.data[0].y[20],null);
  assert.equal(figure.layout.yaxis.title.text,'Total consumption');
  assert.ok(figure.data[0].text[1].includes('1.123456'));
  assert.equal(w.QueryflowECharts.buildOption(spec).series[0].showSymbol,false);
});
test('adaptive chart sizing is compact for simple charts and bounded for long categories',()=>{
  const theme=setup().QueryflowChartTheme;
  const spec={chart_type:'horizontal_bar',x:'label',data:Array.from({length:30},(_,i)=>({label:String(i)}))};
  assert.equal(theme.height(spec,900),440);assert.equal(theme.height(spec,350),380);
  assert.equal(theme.height({...spec,data:[{label:'A'}]},900),250);
  assert.equal(theme.label('totalConsumption'),'Total Consumption');
  assert.equal(theme.exact(null),'—');
});
test('inside bar labels compact values and respect percentage scaling without mutating data',()=>{
  const w=setup(),data=[{category:'A',amount:162640000},{category:'B',amount:1250000}];
  const figure=w.QueryflowPlotly.buildFigure({chart_type:'bar',x:'category',y:'amount',time_format:'raw',data});
  assert.equal(figure.data[0].text[0],'162.64M');
  assert.equal(figure.data[0].textposition,'inside');
  assert.ok(figure.data[0].hovertext[0].includes('162,640,000'));
  assert.equal(figure.data[0].y[0],162640000);
  assert.equal(w.QueryflowChartTheme.compact(.25,'ratio'),'25%');
  assert.equal(w.QueryflowChartTheme.compact(25,'%'),'25%');
  const percent=w.QueryflowPlotly.buildFigure({chart_type:'percent_stacked_bar',x:'category',y:'amount',series:'series',time_format:'raw',
    data:[{category:'A',series:'One',amount:1},{category:'A',series:'Two',amount:3}]});
  assert.equal(percent.data[0].text[0],'25%');
  assert.equal(percent.data[1].text[0],'75%');
});
test('treemap and donut format tile values without changing source measures',()=>{
  const w=setup(),data=[{category:'A',amount:162640000},{category:'B',amount:1250000}];
  for(const chart_type of ['treemap','donut']) {
    const spec={chart_type,x:'category',y:'amount',time_format:'raw',data};
    const figure=w.QueryflowPlotly.buildFigure(spec);
    assert.equal(figure.data[0].text[0],'162.64M');
    assert.ok(figure.data[0].hovertext[0].includes('162,640,000'));
    assert.equal(figure.data[0].values[0],162640000);
    const option=w.QueryflowECharts.buildOption(spec);
    assert.ok(option.series[0].label.formatter({name:'A',value:162640000,percent:99}).includes('162.64M'));
    assert.equal(option.series[0].data[0].value,162640000);
  }
});
test('chart choices distinguish heatmap and treemap icons and CSV keeps an accessible pill control',()=>{
  const source=fs.readFileSync('web/app.js','utf8');
  assert.ok(source.includes('heatmap:"chart-heatmap",treemap:"chart-treemap"'));
  const ctx=vm.createContext({});
  vm.runInContext(fs.readFileSync('web/icons.js','utf8')+'; globalThis.paths=ICON_PATHS;',ctx);
  assert.notEqual(ctx.paths['chart-heatmap'],ctx.paths['chart-treemap']);
  assert.notEqual(ctx.paths['chart-pie'],ctx.paths['chart-donut']);
  const css=fs.readFileSync('web/styles.css','utf8');
  assert.ok(css.includes('.data-header-right .csv-button:focus-visible'));
  assert.ok(css.includes('.data-header-right .csv-button { display:inline-flex'));
});
test('daily ticks retain days, horizontal ratios use the numeric axis, and empty pies are rejected',()=>{
  const w=setup(),data=[{date:'2026-01-01',value:.25},{date:'2026-01-02',value:.5}];
  const daily=w.QueryflowPlotly.buildFigure({chart_type:'line',x:'date',y:'value',time_format:'day',data});
  assert.equal(daily.layout.xaxis.tickformat,'%d %b %Y');
  const horizontal=w.QueryflowPlotly.buildFigure({chart_type:'horizontal_bar',x:'date',y:'value',time_format:'raw',measure_units:{value:'ratio'},data});
  assert.equal(horizontal.layout.xaxis.tickformat,'.0%');
  assert.equal(horizontal.layout.yaxis.tickformat,undefined);
  for(const chart_type of ['pie','donut']) {
    const spec={chart_type,x:'date',y:'value',time_format:'raw',data:data.map(row=>({...row,value:0}))};
    assert.equal(w.QueryflowPlotly.buildFigure(spec),null);
    assert.equal(w.QueryflowECharts.buildOption(spec),null);
  }
});
