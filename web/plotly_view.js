/* Plotly renderer for backend-validated chart specs. No model-generated code or
   browser-side aggregation is accepted here; every point comes from a row. */
window.QueryflowPlotly = (function () {
  const TYPES = new Set([
    "line", "multi_line", "step_line", "area", "stacked_area", "bar",
    "horizontal_bar", "grouped_bar", "stacked_bar", "percent_stacked_bar",
    "scatter", "bubble", "histogram", "pie", "donut", "treemap",
  ]);
  const COLORS = window.QueryflowChartTheme?.colors || ["#168447", "#2563EB", "#C2410C", "#7C3AED", "#0F766E", "#A16207", "#BE185D", "#475569"];
  const DISPLAY_PALETTES = {
    ocean: ["#2676c7", "#16a5a9", "#76b9e6", "#347aa1", "#5b9dd6"],
    forest: ["#27845c", "#71a854", "#3a9f8b", "#9eb86a", "#4f7660"],
    sunset: ["#dc7048", "#d99a32", "#c45b74", "#a875c2", "#e6b26a"],
  };
  const LINE = new Set(["line", "multi_line", "step_line", "area", "stacked_area"]);
  const STACK = new Set(["stacked_area", "stacked_bar", "percent_stacked_bar"]);
  const GROUPED = new Set(["multi_line", "stacked_area", "grouped_bar", "stacked_bar", "percent_stacked_bar"]);
  const temporalValue = value => {
    const raw = String(value ?? "");
    const date = /^\d{6}$/.test(raw) ? `${raw.slice(0, 4)}-${raw.slice(4)}-01`
      : /^\d{4}$/.test(raw) ? `${raw}-01-01`
      : /^\d{4}-\d{2}$/.test(raw) ? `${raw}-01` : raw;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return null;
    const stamp = Date.parse(`${date}T00:00:00Z`);
    return Number.isFinite(stamp) && new Date(stamp).toISOString().slice(0, 10) === date ? date : null;
  };
  const numeric = value => value == null ? null : Number.isFinite(Number(value)) ? Number(value) : NaN;
  function css(name, fallback) {
    return typeof getComputedStyle === "function"
      ? getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback : fallback;
  }
  function supported(type) { return TYPES.has(type); }
  function buildFigure(spec, viewportWidth=800) {
    if (!spec || !supported(spec.chart_type) || !Array.isArray(spec.data) || !spec.data.length) return null;
    const { chart_type: type, x, y, series } = spec;
    if (!x || !y || spec.data.some(row => !Object.hasOwn(row, x) || !Object.hasOwn(row, y))) return null;
    const measures = spec.y_measures?.length ? spec.y_measures : [y];
    if (measures.some(name => spec.data.some(row => !Object.hasOwn(row, name)
      || Number.isNaN(numeric(row[name]))))) return null;
    if (STACK.has(type) && spec.data.some(row => measures.some(name => row[name] == null
      || Number(row[name]) < 0))) return null;
    const time = spec.time_format !== "raw" && spec.data.every(row => temporalValue(row[x]) !== null);
    const rawCategories = [...new Set(spec.data.map(row => String(row[x] ?? "")))];
    if (time) rawCategories.sort((a, b) => temporalValue(a).localeCompare(temporalValue(b)));
    const shownX = value => time ? temporalValue(value) : String(value ?? "");
    const text = css("--text", "#20212b"), muted = css("--text-muted", "#5c6170");
    const colors = Object.hasOwn(DISPLAY_PALETTES, spec.display_palette)
      ? DISPLAY_PALETTES[spec.display_palette]
      : COLORS.map((fallback, index) => css(`--chart-${index + 1}`, fallback));
    const horizontal = type === "horizontal_bar";
    const mobile = viewportWidth < 600;
    const shortLabel = value => window.QueryflowChartTheme?.shortLabel(value,mobile ? 15 : 24) || String(value);
    const escapeLabel=value=>String(value).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
    const compactValue=value=>window.QueryflowChartTheme?.compact(value,spec.measure_units?.[y]) || String(value);
    const exactValue=value=>window.QueryflowChartTheme?.exact(value,spec.measure_units?.[y]) || String(value);
    const makeTrace = (name, rows, mode, color, extra = {}) => {
      const xs = rows.map(row => shownX(row[x]));
      const ys = rows.map(row => numeric(row[name]));
      const raw = rows.map(row => row[x]);
      const unit=spec.measure_units?.[name];
      const formatValue=value => window.QueryflowChartTheme ? window.QueryflowChartTheme.exact(value,unit) : value==null ? "—" : window.QueryflowKpis
        ? window.QueryflowKpis.format(value,unit)
        : new Intl.NumberFormat(undefined,{maximumFractionDigits:2,notation:Math.abs(value)>=1000 ? "compact" : "standard"}).format(value);
      return { type: LINE.has(type) || ["scatter", "bubble"].includes(type) ? "scatter" : "bar",
        mode: LINE.has(type) ? (rows.length>40 ? "lines" : "lines+markers") : ["scatter", "bubble"].includes(type) ? "markers" : undefined,
        name: name === y && !series ? (spec.y_label || window.QueryflowChartTheme?.label(name) || name) : name,
        x: horizontal ? ys : xs, y: horizontal ? xs : ys,
        customdata: raw,
        meta:{measure_unit:unit},
        text:ys.map(value=>formatValue(value).replace(/[&<>]/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[char]))),
        hovertemplate:horizontal ? "%{y}<br>%{text}<extra>%{fullData.name}</extra>" : "%{x}<br>%{text}<extra>%{fullData.name}</extra>",
        marker: { color, opacity:LINE.has(type) ? 1 : 0.85, line: {color:"#ffffff",width:2},
          size: type === "bubble" ? rows.map(row => Math.max(6,
          Math.min(38, Math.sqrt(Math.max(0, Number(row[spec.size] || 0))) * 3))) : 8 },
        line: { color, shape: type === "step_line" ? "hv" : "linear", width: 3 },
        connectgaps: false, orientation: horizontal ? "h" : "v", ...extra };
    };
    let traces = [];
    if (type === "treemap") {
      if (spec.data.some(row=>typeof row[y] !== "number" || !Number.isFinite(row[y]) || row[y]<0)
          || !spec.data.some(row=>row[y]>0)) return null;
      // Flat tiles preserve the exact source grain and values, without hidden
      // category aggregation, hierarchy totals, or invented parent records.
      traces=[{type:"treemap",ids:spec.data.map((_,i)=>`row-${i}`),
        labels:spec.data.map(row=>[row[x],series ? row[series] : null].filter(v=>v!=null).map(String).join(" · ")),
        parents:spec.data.map(()=>""),values:spec.data.map(row=>row[y]),
        customdata:spec.data.map(row=>row[x]),textinfo:"label+value",
        text:spec.data.map(row=>escapeLabel(compactValue(row[y]))),
        texttemplate:"%{label}<br>%{text}",
        hovertext:spec.data.map(row=>escapeLabel(exactValue(row[y]))),
        marker:{colors:spec.data.map((_,i)=>colors[i%colors.length]),line:{color:css("--surface","#fff"),width:2}},
        tiling:{pad:3}, pathbar:{visible:false},
        hovertemplate:"%{label}<br>%{hovertext}<extra></extra>"}];
    } else if (type === "pie" || type === "donut") {
      if (spec.data.some(row => numeric(row[y]) === null || numeric(row[y]) < 0)
        || !spec.data.some(row=>numeric(row[y])>0)) return null;
      traces = [{type: "pie", labels: spec.data.map(row => String(row[x])),
        values: spec.data.map(row => numeric(row[y])), customdata: spec.data.map(row => row[x]),
        text:spec.data.map(row=>escapeLabel(compactValue(row[y]))),
        texttemplate:spec.data.length>10 ? undefined : "%{label}<br>%{text} · %{percent:.1%}",
        hovertext:spec.data.map(row=>escapeLabel(exactValue(row[y]))),
        hovertemplate:"%{label}<br>%{hovertext}<br>%{percent:.1%}<extra></extra>",
        hole: type === "donut" ? 0.52 : 0, sort:false,
        marker: {colors,line:{color:css("--surface","#fff"),width:2}}, textinfo: spec.data.length>10 ? "none" : "label+percent"}];
    } else if (["scatter", "bubble"].includes(type)) {
      if (spec.data.some(row => !Number.isFinite(Number(row[x])))) return null;
      traces = [makeTrace(y, spec.data, "markers", colors[0])];
      traces[0].x = spec.data.map(row => Number(row[x]));
      // Labels are database/user text; keep them out of Plotly's HTML-capable hover template.
      traces[0].hovertemplate = "%{x}<br>%{text}<extra></extra>";
    } else if (measures.length > 1 && !series && ["multi_line", "grouped_bar"].includes(type)) {
      if (rawCategories.length !== spec.data.length) return null;
      traces = measures.map((name, index) => {
        const byX = new Map(spec.data.map(row => [String(row[x]), row]));
        return makeTrace(name, rawCategories.map(value => byX.get(value) || {[x]: value, [name]: null}),
          type, window.QueryflowChartTheme?.colorFor(name,colors) || colors[index % colors.length]);
      });
    } else if (GROUPED.has(type) && series) {
      if (spec.data.some(row => !Object.hasOwn(row, series))) return null;
      const groups = [...new Set(spec.data.map(row => String(row[series])))];
      const seen = new Set();
      for (const row of spec.data) {
        const key = JSON.stringify([row[x], row[series]]);
        if (seen.has(key)) return null; // Never discard a second row at the same grain.
        seen.add(key);
      }
      const totals = new Map(rawCategories.map(value => [value, spec.data
        .filter(row => String(row[x]) === value).reduce((sum, row) => sum + Number(row[y] || 0), 0)]));
      traces = groups.map((group, index) => {
        const byX = new Map(spec.data.filter(row => String(row[series]) === group)
          .map(row => [String(row[x]), row]));
        const rows = rawCategories.map(value => byX.get(value) || {[x]: value, [y]: null});
        const trace = makeTrace(y, rows, type, window.QueryflowChartTheme?.colorFor(group,colors) || colors[index % colors.length], {name: group});
        if (type === "percent_stacked_bar") trace.y = rows.map(row => row[y] == null ? null
          : totals.get(String(row[x])) > 0 ? Number(row[y]) / totals.get(String(row[x])) * 100 : null);
        if (type === "stacked_area") trace.stackgroup = "total";
        return trace;
      });
    } else {
      if (spec.data.length !== rawCategories.length && !["histogram"].includes(type)) return null;
      const byX = new Map(spec.data.map(row => [String(row[x]), row]));
      traces = [makeTrace(y, rawCategories.map(value => byX.get(value)), type, colors[0])];
    }
    for (const trace of traces) {
      if (trace.type!=="bar") continue;
      trace.hovertext=trace.text;
      trace.hovertemplate=trace.hovertemplate.replaceAll("%{text}","%{hovertext}");
      const unit=type==="percent_stacked_bar" ? "%" : trace.meta?.measure_unit;
      trace.text=(horizontal ? trace.x : trace.y).map(value=>window.QueryflowChartTheme
        ? window.QueryflowChartTheme.compact(value,unit) : value==null ? "" : String(value));
      trace.textposition="inside";trace.insidetextanchor="middle";
      trace.textfont={color:"#ffffff",size:11};trace.constraintext="inside";
    }
    const isTimeSeries = time && LINE.has(type);
    const domain = time ? rawCategories.map(temporalValue) : rawCategories;
    const layout = {
      paper_bgcolor: css("--surface", "#fff"), plot_bgcolor: css("--surface", "#fff"),
      font: {family: "Inter, sans-serif", color: text, size: 12},
      hoverlabel:{bgcolor:css("--surface","#fff"),bordercolor:css("--border","#c5e5d0"),font:{color:text,size:12}},
      legend:{orientation:"h",y:1.06,yanchor:"bottom",x:0,font:{size:11},itemsizing:"constant"},
      bargap:0.24,bargroupgap:0.08,
      uniformtext:{minsize:10,mode:"hide"},
      transition:{duration:window.QueryflowChartTheme?.reducedMotion() ? 0 : 120},
      margin: {l: horizontal ? 115 : 58, r: 22, t: spec.legend !== false && (traces.length>1 || ["pie","donut"].includes(type)) ? 42 : 16, b: 64},
      showlegend: spec.legend !== false && (traces.length > 1 || ["pie","donut"].includes(type) && rawCategories.length>1),
      hovermode: isTimeSeries ? "x unified" : "closest", dragmode: "zoom", uirevision: "queryflow",
      barmode: type === "grouped_bar" ? "group" : STACK.has(type) ? "stack" : "relative",
      xaxis: {title: {text: window.QueryflowChartTheme?.label(horizontal ? spec.y_label || y : spec.x_label || x) || (horizontal ? spec.y_label || y : spec.x_label || x),standoff:16,font:{size:13,color:text}},
        nticks:mobile ? 4 : 7,tickformat:horizontal ? (spec.measure_units?.[y]==="ratio" ? ".0%" : "~s")
          : time ? (spec.time_format==="year" ? "%Y" : spec.time_format==="day" ? "%d %b %Y" : "%b %Y") : undefined,
        ticksuffix:horizontal && ["%","percent"].includes(spec.measure_units?.[y]) ? "%" : "",
        type: horizontal || ["scatter", "bubble"].includes(type) ? "linear" : time ? "date" : "category",
        categoryorder: "array", categoryarray: time ? undefined : rawCategories,
        automargin: true, color: muted, showgrid:false, zeroline:false, gridcolor: css("--chart-grid", "#e8eef0"),
        rangeslider: {visible: false}},
      yaxis: {title: {text:window.QueryflowChartTheme?.label(horizontal ? spec.x_label || x : spec.y_label || y) || (horizontal ? spec.x_label || x : spec.y_label || y),standoff:12},nticks:5,
        tickformat:horizontal ? undefined : spec.measure_units?.[y] === "ratio" ? ".0%" : "~s",
        ticksuffix:!horizontal && ["%","percent"].includes(spec.measure_units?.[y]) ? "%" : "",
        rangemode: "tozero", automargin: true, color: muted,
        zeroline:false, gridcolor: css("--chart-grid", "#e8eef0")},
    };
    if (!time && !["scatter","bubble","pie","donut","treemap"].includes(type)) {
      const axis=horizontal ? layout.yaxis : layout.xaxis;
      const ticks=window.QueryflowChartTheme?.axisTicks(rawCategories,mobile ? 4 : 7) || rawCategories;
      Object.assign(axis,{tickmode:"array",tickvals:ticks,ticktext:ticks.map(shortLabel),tickangle:0});
    }
    if (type === "percent_stacked_bar") layout.yaxis.range = [0, 100];
    if (type === "area") {
      traces[0].fill = "tozeroy";
      const color = traces[0].line.color;
      traces[0].fillcolor = /^#[0-9a-f]{6}$/i.test(color)
        ? `rgba(${parseInt(color.slice(1,3),16)},${parseInt(color.slice(3,5),16)},${parseInt(color.slice(5,7),16)},0.12)`
        : "rgba(22,132,71,0.12)";
      traces[0].line.width = 3;
      const rgb = /^#[0-9a-f]{6}$/i.test(color)
        ? `${parseInt(color.slice(1,3),16)},${parseInt(color.slice(3,5),16)},${parseInt(color.slice(5,7),16)}`
        : "22,132,71";
      traces[0].fillgradient = {type:"vertical", colorscale:[
        [0, `rgba(${rgb},0.015)`], [1, `rgba(${rgb},0.28)`],
      ]};
    }
    if (time && spec.zoom_start != null && spec.zoom_end != null && domain.length > 1) {
      const first = Date.parse(domain[0]), last = Date.parse(domain.at(-1));
      const span = last - first;
      layout.xaxis.range = [new Date(first + span * spec.zoom_start / 100).toISOString(),
        new Date(first + span * spec.zoom_end / 100).toISOString()];
    }
    return {data: traces, layout, config: {responsive: true, scrollZoom: true,
      displaylogo: false, modeBarButtonsToRemove: ["lasso2d", "select2d"]},
    domain, isTimeSeries};
  }
  function render(container, presentation, _generation, onFailure) {
    if (!container || !window.Plotly || !presentation?.chart_spec) return null;
    const spec = presentation.chart_spec;
    const figure = buildFigure(spec,container.clientWidth || 800);
    if (!figure) return null;
    container.setAttribute?.("role", "figure");
    container.setAttribute?.("aria-label", `${spec.title || spec.chart_type}. ${spec.source_row_count || spec.data.length} observations. ${spec.x_label || spec.x}; ${spec.y_label || spec.y}. Exact values are available in the Data tab.`);
    if (container.style && window.QueryflowChartTheme) container.style.height=`${window.QueryflowChartTheme.height(spec,container.clientWidth || 800)}px`;
    const plotly = window.Plotly;
    let disposed = false;
    const ready = Promise.resolve(plotly.newPlot(container, figure.data, figure.layout, figure.config))
      .catch(error => {
        if (!disposed) {
          console.warn(`Plotly chart failed: ${error.message}`);
          onFailure?.(error);
        }
        return null;
      });
    const subscriptions = [];
    const subscribe = (event, handler) => {
      subscriptions.push([event, handler]);
      void ready.then(() => { if (!disposed) container.on?.(event, handler); });
    };
    const domainStart = figure.isTimeSeries ? Date.parse(figure.domain[0]) : 0;
    const domainEnd = figure.isTimeSeries ? Date.parse(figure.domain.at(-1)) : figure.domain.length - 1;
    let pinch = null;
    const distance = touches => Math.hypot(touches[0].clientX - touches[1].clientX,
      touches[0].clientY - touches[1].clientY);
    const touchStart = event => {
      if (!figure.isTimeSeries || event.touches.length !== 2) return;
      const bounds = container.getBoundingClientRect();
      const range = container.layout?.xaxis?.range || [figure.domain[0], figure.domain.at(-1)];
      const start = Date.parse(range[0]), end = Date.parse(range[1]);
      if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return;
      const middle = (event.touches[0].clientX + event.touches[1].clientX) / 2;
      pinch = {distance: distance(event.touches), start, end,
        anchor: Math.max(0, Math.min(1, (middle - bounds.left) / Math.max(1, bounds.width)))};
    };
    const touchMove = event => {
      if (!pinch || event.touches.length !== 2) return;
      event.preventDefault(); event.stopPropagation();
      const span = Math.min(domainEnd - domainStart,
        Math.max(24 * 60 * 60 * 1000, (pinch.end - pinch.start) * pinch.distance / Math.max(8, distance(event.touches))));
      const center = pinch.start + pinch.anchor * (pinch.end - pinch.start);
      const start = Math.max(domainStart, Math.min(domainEnd - span, center - pinch.anchor * span));
      void plotly.relayout(container, {"xaxis.range": [new Date(start).toISOString(),
        new Date(start + span).toISOString()]});
    };
    const touchEnd = event => { if (event.touches.length < 2) pinch = null; };
    if (figure.isTimeSeries && domainEnd > domainStart) {
      container.classList?.add?.("chart-slot-pinch");
      container.addEventListener("touchstart", touchStart, {passive: true, capture: true});
      container.addEventListener("touchmove", touchMove, {passive: false, capture: true});
      container.addEventListener("touchend", touchEnd, {passive: true, capture: true});
      container.addEventListener("touchcancel", touchEnd, {passive: true, capture: true});
    }
    const resize = () => { if (!disposed) void plotly.Plots.resize(container); };
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(resize) : null;
    observer?.observe(container);
    return {
      renderer: "plotly",
      highlightPoint(point) {
        if (disposed) return;
        const matches=[];
        (container.data || figure.data).forEach((trace,curveNumber)=>{
          if (trace.name !== point.series_name) return;
          (trace.customdata || []).forEach((raw,pointNumber)=>{
            const values=spec.chart_type === "horizontal_bar" ? trace.x : trace.y;
            if (String(raw)===String(point.x) && values?.[pointNumber]===point.y)
              matches.push({curveNumber,pointNumber});
          });
        });
        if (matches.length) plotly.Fx?.hover?.(container,matches);
      },
      highlightSeries(name) {
        if (disposed) return;
        const traces=container.data || figure.data;
        const matches=traces.map((trace,index)=>trace.name === name ? index : -1).filter(index=>index>=0);
        if (!matches.length) return;
        void plotly.restyle(container,{opacity:traces.map((_,index)=>matches.includes(index) ? 1 : 0.25)});
      },
      dispose() {
        disposed = true; observer?.disconnect();
        for (const [event, handler] of subscriptions) container.removeListener?.(event, handler);
        for (const name of ["touchstart", "touchmove", "touchend", "touchcancel"])
          container.removeEventListener?.(name, ({touchstart: touchStart, touchmove: touchMove,
            touchend: touchEnd, touchcancel: touchEnd})[name], true);
        container.classList?.remove?.("chart-slot-pinch");
        plotly.purge(container);
      },
      onSelect(callback) { subscribe("plotly_click", event => {
        const point = event.points?.[0]; if (!point) return;
        callback({rawX: point.customdata ?? spec.data[point.pointIndex]?.[spec.x],
          seriesName: point.data?.name, name: point.label, value: point.value});
      }); },
      onZoom(callback) { subscribe("plotly_relayout", event => {
        if (event["xaxis.autorange"] === true) { callback({start: 0, end: 100}); return; }
        const range = event["xaxis.range"] || [event["xaxis.range[0]"], event["xaxis.range[1]"]];
        if (!range[0] || !range[1] || !figure.isTimeSeries || domainEnd <= domainStart) return;
        callback({start: Math.max(0, Math.min(100, (Date.parse(range[0]) - domainStart) / (domainEnd - domainStart) * 100)),
          end: Math.max(0, Math.min(100, (Date.parse(range[1]) - domainStart) / (domainEnd - domainStart) * 100))});
      }); },
      resetZoom() { void plotly.relayout(container, {"xaxis.autorange": true}); },
      resize,
      getDataURL(options = {}) { return plotly.toImage(container, {format: "png", scale: options.pixelRatio || 2,
        width: container.clientWidth || 900, height: container.clientHeight || 450}); },
    };
  }
  return {supported, buildFigure, render};
})();
