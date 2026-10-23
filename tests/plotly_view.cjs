const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(`${__dirname}/../web/plotly_view.js`, 'utf8');
const context = vm.createContext({
  window: {}, document: {documentElement: {}},
  getComputedStyle: () => ({getPropertyValue: () => ''}),
  ResizeObserver: class {observe() {} disconnect() {}},
});
vm.runInContext(source, context);
const view = context.window.QueryflowPlotly;

test('Plotly time series keeps all rows, chronological dates, NULL gaps and range navigator', () => {
  const rows = Array.from({length: 50}, (_, index) => ({Month: `2026${String(index % 12 + 1).padStart(2, '0')}`,
    Value: index}));
  // Use unique dates to verify ordering without aggregation.
  const dates = Array.from({length: 50}, (_, index) => ({Date: new Date(Date.UTC(2026, 0, index + 1))
    .toISOString().slice(0, 10), Value: index === 2 ? null : index - 4})).reverse();
  const figure = view.buildFigure({chart_type: 'line', x: 'Date', y: 'Value',
    time_format: 'day', data: dates});
  assert.equal(figure.data[0].x.length, 50);
  assert.equal(figure.data[0].x[0], '2026-01-01');
  assert.equal(figure.data[0].y[2], null);
  assert.equal(figure.data[0].y[0], -4);
  assert.equal(figure.layout.xaxis.type, 'date');
  assert.equal(figure.layout.xaxis.rangeslider.visible, true);
  assert.equal(figure.config.scrollZoom, true);
  assert.equal(rows.length, 50);
});

test('Plotly grouped series never invents a missing value or collapses duplicate grain', () => {
  const spec = {chart_type: 'multi_line', x: 'Month', y: 'Revenue', series: 'Country',
    time_format: 'month', data: [
      {Month:'2026-01', Country:'DE', Revenue:2},
      {Month:'2026-01', Country:'FR', Revenue:3},
      {Month:'2026-02', Country:'DE', Revenue:4},
    ]};
  const figure = view.buildFigure(spec);
  assert.equal(figure.data.length, 2);
  assert.deepEqual(Array.from(figure.data[1].y), [3, null]);
  assert.equal(view.buildFigure({...spec, data: [...spec.data, spec.data[0]]}), null);
  assert.equal(view.buildFigure({...spec, chart_type:'stacked_area',
    data: [{Month:'2026-01', Country:'DE', Revenue:null}]}), null);
});

test('Plotly mounted time series supports pinch, relayout feedback, click provenance and cleanup', async () => {
  const listeners = new Map(), events = new Map();
  let purged = false, imageRequested = false, zoomed;
  const node = {
    classList: {add() {}, remove() {}},
    addEventListener(name, fn) {listeners.set(name, fn);},
    removeEventListener(name) {listeners.delete(name);},
    on(name, fn) {events.set(name, fn);},
    removeListener(name) {events.delete(name);},
    getBoundingClientRect() {return {left:0, width:300};},
    clientWidth: 500, clientHeight: 350,
  };
  const plotly = {
    newPlot(target, traces, layout, config) {target.layout = layout; assert.equal(config.scrollZoom, true);
      return Promise.resolve(target);},
    relayout(_target, change) {zoomed = change; node.layout.xaxis.range = change['xaxis.range'];
      events.get('plotly_relayout')?.(change); return Promise.resolve();},
    purge() {purged = true;}, Plots: {resize() {}},
    toImage() {imageRequested = true; return Promise.resolve('data:image/png;base64,AA==');},
  };
  context.window.Plotly = plotly;
  const spec = {chart_type:'line', x:'Month', y:'Revenue', time_format:'month', data:[
    {Month:'2026-01', Revenue:2}, {Month:'2026-02', Revenue:3}, {Month:'2026-03', Revenue:4},
  ]};
  const mounted = view.render(node, {chart_spec:spec});
  let clicked, range;
  mounted.onSelect(value => {clicked = value;});
  mounted.onZoom(value => {range = value;});
  await new Promise(resolve => setTimeout(resolve, 0));
  events.get('plotly_click')({points:[{customdata:'2026-02', data:{name:'Revenue'}}]});
  assert.equal(clicked.rawX, '2026-02');
  listeners.get('touchstart')({touches:[{clientX:100,clientY:0},{clientX:200,clientY:0}]});
  let prevented = false;
  listeners.get('touchmove')({touches:[{clientX:50,clientY:0},{clientX:250,clientY:0}],
    preventDefault(){prevented = true;}, stopPropagation(){}});
  assert.equal(prevented, true);
  assert.ok(zoomed['xaxis.range'][0] < zoomed['xaxis.range'][1]);
  assert.ok(range.start > 0 && range.end < 100);
  assert.equal(await mounted.getDataURL(), 'data:image/png;base64,AA==');
  assert.equal(imageRequested, true);
  mounted.dispose();
  assert.equal(purged, true);
  assert.equal(events.size, 0);
  assert.equal(listeners.size, 0);
});

test('app loads Plotly first and keeps ECharts as an offline fallback', () => {
  const html = fs.readFileSync(`${__dirname}/../web/index.html`, 'utf8');
  const app = fs.readFileSync(`${__dirname}/../web/app.js`, 'utf8');
  assert.ok(html.includes('cdn.plot.ly/plotly-4.1.1.min.js'));
  assert.ok(html.indexOf('/static/plotly_view.js') < html.indexOf('/static/app.js'));
  assert.ok(app.includes('window.QueryflowPlotly?.render'));
  assert.ok(app.includes('window.QueryflowECharts?.render'));
  assert.ok(html.includes('class="chart-zoom-guide"'));
});
