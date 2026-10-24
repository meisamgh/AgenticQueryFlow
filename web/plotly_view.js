/* Plotly renderer for backend-validated chart specs. No model-generated code or
   browser-side aggregation is accepted here; every point comes from a row. */
window.QueryflowPlotly = (function () {
  const TYPES = new Set([
    "line", "multi_line", "step_line", "area", "stacked_area", "bar",
    "horizontal_bar", "grouped_bar", "stacked_bar", "percent_stacked_bar",
    "scatter", "bubble", "histogram", "pie", "donut",
  ]);
  const COLORS = ["#6e60e6", "#4f74d4", "#20946c", "#c1803a", "#d45d65", "#998df6", "#2e9bb5", "#b0568d"];
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
  function buildFigure(spec) {
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
    const colors = COLORS.map((fallback, index) => css(`--chart-${index + 1}`, fallback));
    const horizontal = type === "horizontal_bar";
    const makeTrace = (name, rows, mode, color, extra = {}) => {
      const xs = rows.map(row => shownX(row[x]));
      const ys = rows.map(row => numeric(row[name]));
      const raw = rows.map(row => row[x]);
      return { type: LINE.has(type) || ["scatter", "bubble"].includes(type) ? "scatter" : "bar",
        mode: LINE.has(type) ? "lines+markers" : ["scatter", "bubble"].includes(type) ? "markers" : undefined,
        name: name === y && !series ? (spec.y_label || name) : name,
        x: horizontal ? ys : xs, y: horizontal ? xs : ys,
        customdata: raw,
        marker: { color, size: type === "bubble" ? rows.map(row => Math.max(6,
          Math.min(38, Math.sqrt(Math.max(0, Number(row[spec.size] || 0))) * 3))) : 7 },
        line: { color, shape: type === "step_line" ? "hv" : "linear", width: 2 },
        connectgaps: false, orientation: horizontal ? "h" : "v", ...extra };
    };
    let traces = [];
    if (type === "pie" || type === "donut") {
      if (spec.data.some(row => numeric(row[y]) === null || numeric(row[y]) < 0)) return null;
      traces = [{type: "pie", labels: spec.data.map(row => String(row[x])),
        values: spec.data.map(row => numeric(row[y])), customdata: spec.data.map(row => row[x]),
        hole: type === "donut" ? 0.48 : 0, marker: {colors}, textinfo: "label+percent"}];
    } else if (["scatter", "bubble"].includes(type)) {
      if (spec.data.some(row => !Number.isFinite(Number(row[x])))) return null;
      traces = [makeTrace(y, spec.data, "markers", colors[0])];
      traces[0].x = spec.data.map(row => Number(row[x]));
      // Labels are database/user text; keep them out of Plotly's HTML-capable hover template.
      traces[0].hovertemplate = "%{x}<br>%{y}<extra></extra>";
    } else if (measures.length > 1 && !series && ["multi_line", "grouped_bar"].includes(type)) {
      if (rawCategories.length !== spec.data.length) return null;
      traces = measures.map((name, index) => {
        const byX = new Map(spec.data.map(row => [String(row[x]), row]));
        return makeTrace(name, rawCategories.map(value => byX.get(value) || {[x]: value, [name]: null}),
          type, colors[index % colors.length]);
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
        const trace = makeTrace(y, rows, type, colors[index % colors.length], {name: group});
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
    const isTimeSeries = time && LINE.has(type);
    const domain = time ? rawCategories.map(temporalValue) : rawCategories;
    const layout = {
      paper_bgcolor: css("--surface", "#fff"), plot_bgcolor: css("--surface", "#fff"),
      font: {family: "Inter, sans-serif", color: text, size: 12},
      margin: {l: horizontal ? 115 : 58, r: 22, t: 16, b: isTimeSeries ? 64 : 52},
      showlegend: spec.legend !== false && traces.length > 1,
      hovermode: isTimeSeries ? "x unified" : "closest", dragmode: "zoom", uirevision: "queryflow",
      barmode: type === "grouped_bar" ? "group" : STACK.has(type) ? "stack" : "relative",
      xaxis: {title: {text: horizontal ? spec.y_label || y : spec.x_label || x},
        type: time ? "date" : ["scatter", "bubble"].includes(type) ? "linear" : "category",
        categoryorder: "array", categoryarray: time ? undefined : rawCategories,
        automargin: true, color: muted, gridcolor: css("--border", "#e5e5eb"),
        rangeslider: {visible: isTimeSeries && domain.length > 5, thickness: 0.14}},
      yaxis: {title: {text: horizontal ? spec.x_label || x : spec.y_label || y},
        rangemode: "tozero", automargin: true, color: muted,
        gridcolor: css("--border", "#e5e5eb")},
    };
    if (type === "percent_stacked_bar") layout.yaxis.range = [0, 100];
    if (type === "area") traces[0].fill = "tozeroy";
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
    const figure = buildFigure(spec);
    if (!figure) return null;
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
