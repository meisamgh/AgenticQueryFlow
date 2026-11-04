const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const context=vm.createContext({window:{matchMedia:()=>({matches:false})},
  document:{documentElement:{}},getComputedStyle:()=>({getPropertyValue:()=>''})});
for(const file of ['formatters','chart_theme','kpi_cards','plotly_view','echarts_view'])
  vm.runInContext(fs.readFileSync(`${__dirname}/../web/${file}.js`,'utf8'),context);
const {QueryflowPlotly:plotly,QueryflowECharts:echarts,QueryflowFormat:format,
  QueryflowChartTheme:theme,QueryflowKpis:kpis}=context.window;
const types=['line','multi_line','step_line','area','stacked_area','bar','horizontal_bar',
  'grouped_bar','stacked_bar','percent_stacked_bar','scatter','bubble','histogram','box',
  'heatmap','pie','donut','treemap','calendar_heatmap'];
for(const type of types) test(`premium ${type} preserves source observations and builds supported renderer`,()=>{
  const spec={chart_type:type,x:'date',y:'amount',size:'size',time_format:'month',
    x_label:'Date',y_label:'Amount',number_format:'compact',
    data:[{date:'202601',amount:10,size:4},{date:'202602',amount:20,size:9}]};
  if(type==='heatmap') {spec.series='category';spec.data.forEach(row=>row.category='A');}
  const original=JSON.stringify(spec);
  const figure=plotly.buildFigure(spec),option=echarts.buildOption(spec);
  assert.ok(option,`Missing ECharts ${type}`);
  if(plotly.supported(type)) assert.ok(figure,`Missing Plotly ${type}`);
  assert.equal(JSON.stringify(spec),original);
  if(figure && ['line','multi_line','step_line','area','stacked_area'].includes(type)){
    assert.equal(figure.data[0].line.width,3);
    assert.equal(figure.data[0].marker.size,8);
    assert.equal(figure.data[0].marker.line.width,2);
    assert.equal(figure.data[0].marker.line.color,'#ffffff');
    assert.equal(figure.data[0].line.shape,type==='step_line'?'hv':'linear');
    assert.equal(figure.layout.xaxis.rangeslider.visible,false);
    assert.equal(figure.config.scrollZoom,true);
  }
});
test('shared formatting has identical units/scales and exact tooltips across consumers',()=>{
  for(const [value,unit] of [[162640000,undefined],[0.125,'ratio'],[12.5,'%'],[1250000,'EUR'],[3,'count'],[null,undefined]]){
    const expected=format.formatNumber(value,unit,'en-US',{compact:Math.abs(value)>=1000});
    assert.equal(kpis.format(value,unit,'en-US'),expected);
    assert.equal(theme.exact(value,unit),format.formatNumber(value,unit,undefined,{exact:true}));
  }
  assert.equal(format.formatNumber(162640000,null,'en-US',{compact:true}),'162.64M');
  assert.equal(format.formatNumber(0.125,'ratio','en-US'),'12.5%');
  assert.equal(format.formatPeriod('20260230','day','en-US'),null);
  assert.equal(format.formatCellValue(202601,{column:'month',timeColumn:'month',timeFormat:'month',locale:'en-US'}),'Jan 2026');
});
test('default palette meets non-text white-background contrast and avoids hash collisions',()=>{
  const luminance=hex=>hex.match(/[0-9a-f]{2}/ig).map(x=>parseInt(x,16)/255)
    .map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4)
    .reduce((n,x,i)=>n+x*[.2126,.7152,.0722][i],0);
  for(const color of theme.colors) assert.ok(1.05/(luminance(color)+.05)>=3,color);
  assert.notEqual(theme.colorFor('Aa',theme.colors),theme.colorFor('BB',theme.colors));
  assert.equal(theme.colorFor('Aa',theme.colors),theme.colorFor('Aa',theme.colors));
});
test('responsive sizing, dark palette, KPI focus and comparison-only sparklines remain explicit',()=>{
  const spec={chart_type:'horizontal_bar',x:'category',data:Array.from({length:30},(_,i)=>({category:i}))};
  assert.ok(theme.height(spec,360)<=380);
  assert.ok(theme.height(spec,1200)<=440);
  const styles=fs.readFileSync(`${__dirname}/../web/styles.css`,'utf8');
  assert.ok(styles.includes('.chart-kpi-card:focus-visible'));
  assert.ok(styles.includes('[data-theme="dark"] .kpi-comparison-bubble'));
  assert.ok(styles.includes('[data-comparison="true"] .kpi-sparkline'));
  const dark=styles.match(/\[data-theme="dark"\] \{ --chart-1:[^}]+/)[0];
  assert.equal((dark.match(/--chart-\d:/g)||[]).length,8);
});
test('readable titles preserve supplied meaning without inventing periods or metrics',()=>{
  assert.equal(theme.title({title:'total_consumption by Date'}),'Total consumption by Date');
  assert.equal(theme.title({x:'month',y:'totalRevenue'}),'Total Revenue by month');
  assert.equal(theme.title({title:'Revenue vs costs (EUR)'}),'Revenue vs costs (EUR)');
});
test('mobile label thinning and shortening never remove observations or change chart height',()=>{
  const data=Array.from({length:21},(_,i)=>({category:`Long customer category ${i}`,amount:i}));
  const spec={chart_type:'bar',x:'category',y:'amount',time_format:'raw',data};
  const before=JSON.stringify(spec);
  const mobile=plotly.buildFigure(spec,360),desktop=plotly.buildFigure(spec,1000);
  assert.equal(mobile.data[0].y.length,21);
  assert.equal(mobile.layout.xaxis.tickvals.length,4);
  assert.equal(desktop.layout.xaxis.tickvals.length,7);
  assert.equal(mobile.layout.xaxis.tickvals.at(-1),data.at(-1).category);
  assert.ok(mobile.layout.xaxis.ticktext.every(label=>label.length<=15));
  assert.equal(mobile.data[0].customdata[0],data[0].category);
  assert.equal(echarts.buildOption(spec,360).series[0].data.length,21);
  assert.equal(theme.height(spec,360),260);
  assert.equal(theme.height(spec,1000),260);
  assert.equal(JSON.stringify(spec),before);
});
test('dense lines lose markers, not data or shape; legends stay above the plot',()=>{
  for(const count of [10,60]){
    const spec={chart_type:'line',x:'category',y:'amount',time_format:'raw',
      data:Array.from({length:count},(_,i)=>({category:`C${i}`,amount:i%4}))};
    const figure=plotly.buildFigure(spec);
    assert.equal(figure.data[0].mode,count>40?'lines':'lines+markers');
    assert.equal(figure.data[0].line.shape,'linear');
    assert.equal(figure.data[0].y.length,count);
    assert.equal(echarts.buildOption(spec).series[0].showSymbol,count<=40);
    assert.ok(figure.layout.legend.y>1);
  }
});
test('composition styling retains original categories, values and order',()=>{
  const spec={chart_type:'donut',x:'category',y:'amount',legend:true,time_format:'raw',
    data:[{category:'Z',amount:2},{category:'A',amount:10},{category:'B',amount:5}]};
  const figure=plotly.buildFigure(spec);
  assert.equal(figure.data[0].sort,false);
  assert.equal(JSON.stringify(figure.data[0].values),'[2,10,5]');
  assert.equal(figure.layout.showlegend,true);
  const option=echarts.buildOption(spec);
  assert.equal(option.series[0].avoidLabelOverlap,true);
  assert.equal(option.series[0].minShowLabelAngle,12);
});
