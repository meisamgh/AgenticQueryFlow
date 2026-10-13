/* Declarative, data-bound charts: never execute model-generated markup or code. */
function renderResultChart(container, presentation, generation) {
  if (!presentation || !container) return;
  const note = document.createElement('p');
  note.className = 'chart-note';
  note.textContent = presentation.note;
  container.append(note);
  if (!['bar', 'line', 'scatter'].includes(presentation.chart_type)) return;
  const columns = generation.columns || [];
  const xi = columns.indexOf(presentation.x), yi = columns.indexOf(presentation.y);
  if (xi < 0 || yi < 0 || xi === yi) return;
  const rows = (generation.rows || []).slice(0, 10);
  if (presentation.chart_type === 'line') rows.sort((a, b) => String(a[xi]).localeCompare(String(b[xi])));
  const valid = rows.filter(r => typeof r[yi] === 'number' && Number.isFinite(r[yi]));
  if (valid.length < 2) return;
  const figure = document.createElement('figure');
  figure.className = 'result-chart';
  const title = document.createElement('figcaption');
  title.textContent = presentation.title || `${presentation.y} by ${presentation.x}`;
  figure.append(title);
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const layout = {left: 145, right: 845, top: 48, bottom: 310};
  svg.setAttribute('viewBox', '0 0 900 400');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', title.textContent);
  function mark(tag, attrs, text) {
    const el = document.createElementNS(svg.namespaceURI, tag);
    Object.entries(attrs).forEach(([k,v]) => el.setAttribute(k, String(v)));
    if (text !== undefined) el.textContent = text;
    svg.append(el); return el;
  }
  const lo = Math.min(0, ...valid.map(r => r[yi])), hi = Math.max(0, ...valid.map(r => r[yi]));
  const compact = presentation.number_format === 'compact';
  const axisNumber = value => compact
    ? new Intl.NumberFormat(undefined, {notation:'compact',maximumFractionDigits:1}).format(value)
    : new Intl.NumberFormat(undefined, {maximumFractionDigits:2}).format(value);
  const y = v => layout.bottom - (v - lo) / (hi - lo || 1) * (layout.bottom - layout.top);
  function temporal(value) {
    const text = String(value);
    const iso = /^\d{6}$/.test(text) ? `${text.slice(0,4)}-${text.slice(4)}-01`
      : text.length === 4 ? `${text}-01-01` : text.length === 7 ? `${text}-01` : text;
    return Date.parse(`${iso}T00:00:00Z`);
  }
  function timeLabel(value) {
    if (presentation.time_format === 'raw') return String(value);
    const time = temporal(value);
    if (!Number.isFinite(time)) return String(value);
    const options = presentation.time_format === 'year' ? {year:'numeric'}
      : presentation.time_format === 'month' ? {year:'numeric',month:'short'}
      : presentation.time_format === 'day' ? {year:'numeric',month:'short',day:'numeric'} : null;
    return options ? new Intl.DateTimeFormat(undefined,{...options,timeZone:'UTC'}).format(time) : String(value);
  }
  const xs = valid.map(r => presentation.chart_type === 'line' ? temporal(r[xi]) : r[xi]);
  const xmin = Math.min(...xs.map(Number)), xmax = Math.max(...xs.map(Number));
  const x = (r, i) => presentation.chart_type === 'scatter' || presentation.chart_type === 'line'
    ? layout.left + ((presentation.chart_type === 'line' ? temporal(r[xi]) : r[xi]) - xmin) / (xmax - xmin || 1) * (layout.right - layout.left)
    : layout.left + (i + 0.5) / rows.length * (layout.right - layout.left);
  for (let i=0; i<=4; i++) {
    const v = lo + (hi-lo)*i/4;
    mark('line', {x1:layout.left,x2:layout.right,y1:y(v),y2:y(v),stroke:'#e2e8f0'});
    mark('text', {x:layout.left-12,y:y(v)+4,'text-anchor':'end',fill:'#475569','font-size':11}, axisNumber(v));
  }
  let previous = null;
  let lastTickRight = -Infinity;
  rows.forEach((r,i) => {
    if (typeof r[yi] !== 'number' || !Number.isFinite(r[yi])) { previous=null; return; }
    const px=x(r,i), py=y(r[yi]);
    if (presentation.chart_type === 'line' && previous) mark('line',{x1:previous[0],y1:previous[1],x2:px,y2:py,stroke:'#2563eb','stroke-width':2});
    const point = presentation.chart_type === 'bar'
      ? mark('rect',{x:px-(layout.right-layout.left)*0.35/rows.length,y:Math.min(py,y(0)),width:(layout.right-layout.left)*0.7/rows.length,height:Math.max(1,Math.abs(y(0)-py)),fill:'#2563eb'})
      : mark('circle',{cx:px,cy:py,r:4,fill:'#2563eb'});
    const tooltip=document.createElementNS(svg.namespaceURI,'title');
    tooltip.textContent=`${r[xi]}: ${r[yi]}`; point.append(tooltip);
    if (i % Math.max(1,Math.ceil(rows.length/10)) === 0) {
      const xTick = presentation.chart_type === 'scatter' && typeof r[xi] === 'number'
        ? axisNumber(r[xi]) : presentation.chart_type === 'line'
          ? timeLabel(r[xi]) : String(r[xi]).slice(0,18);
      const halfWidth = xTick.length * 3.5;
      if (px - halfWidth > lastTickRight + 10) {
        mark('text',{x:px,y:337,'text-anchor':'middle',fill:'#475569','font-size':11},xTick);
        lastTickRight = px + halfWidth;
      }
    }
    previous=[px,py];
  });
  mark('text',{x:495,y:383,'text-anchor':'middle',fill:'#334155','font-size':13},presentation.x_label || presentation.x);
  mark('text',{x:layout.left,y:22,fill:'#334155','font-size':13},presentation.y_label || presentation.y);
  figure.append(svg);
  if (presentation.chart_reason) {
    const reason = document.createElement('p');
    reason.className = 'chart-reason';
    reason.textContent = presentation.chart_reason;
    figure.append(reason);
  }
  container.append(figure);
}
