/* Apache ECharts adapter for backend-validated, explicit chart specifications.
   No user text is evaluated as code or merged into ECharts options. */
window.QueryflowECharts = (function () {
  /* Series colors come from the shared token system (--chart-1..--chart-8,
     overridden per theme in styles.css). No arbitrary rainbow hue, one
     palette shared by every chart type. */
  const FALLBACK_PALETTE = [
    "#6e60e6", "#4f74d4", "#20946c", "#c1803a", "#d45d65", "#998df6", "#2e9bb5", "#b0568d",
  ];
  const TYPES = new Set([
    "line", "multi_line", "step_line", "area", "stacked_area", "bar",
    "horizontal_bar", "grouped_bar", "stacked_bar", "scatter", "bubble",
    "histogram", "box", "heatmap", "pie", "donut", "treemap", "calendar_heatmap",
    "percent_stacked_bar",
  ]);

  function cssVar(name, fallback) {
    if (typeof getComputedStyle !== "function") return fallback;
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  }
  function palette() {
    const steps = FALLBACK_PALETTE.map((_, index) => cssVar(`--chart-${index + 1}`, ""));
    const resolved = steps.filter(Boolean);
    return resolved.length ? resolved : FALLBACK_PALETTE;
  }
  function formatNumber(value, compact) {
    if (value == null || value === "") return "";
    const number = Number(value);
    if (!Number.isFinite(number)) return String(value);
    return new Intl.NumberFormat(undefined, compact
      ? { notation: "compact", maximumFractionDigits: 1 }
      : { maximumFractionDigits: 2 }).format(number);
  }
  function label(value) { return String(value ?? ""); }
  function unique(values) { return [...new Set(values.map(label))]; }
  function canonicalDate(value) {
    const raw = label(value);
    return /^\d{6}$/.test(raw) ? `${raw.slice(0, 4)}-${raw.slice(4)}-01`
      : /^\d{4}$/.test(raw) ? `${raw}-01-01`
      : /^\d{4}-\d{2}$/.test(raw) ? `${raw}-01` : raw;
  }
  function friendlyDate(value, format) {
    if (format === "raw") return label(value);
    const parsed = Date.parse(`${canonicalDate(value)}T00:00:00Z`);
    if (!Number.isFinite(parsed)) return label(value);
    const options = format === "year" ? { year: "numeric" }
      : format === "month" ? { year: "numeric", month: "short" }
      : { year: "numeric", month: "short", day: "numeric" };
    return new Intl.DateTimeFormat(undefined, { ...options, timeZone: "UTC" }).format(parsed);
  }
  function supported(type) { return TYPES.has(type); }

  function buildOption(spec) {
    if (!spec || !supported(spec.chart_type) || !Array.isArray(spec.data) || !spec.data.length) return null;
    const data = spec.data;
    const type = spec.chart_type;
    const x = spec.x, y = spec.y, group = spec.series;
    const measures = Array.isArray(spec.y_measures) && spec.y_measures.length
      ? spec.y_measures : [y];
    if (!x || !y || data.some(row => !(x in row) || !(y in row))) return null;
    if (measures.some(name => data.some(row => !(name in row) || row[name] == null || !Number.isFinite(Number(row[name]))))) return null;
    const text = cssVar("--text", "#20212b");
    const muted = cssVar("--text-muted", "#5c6170");
    const border = cssVar("--border-strong", "#dcdbe2");
    const compact = spec.number_format === "compact";
    const base = {
      animation: !window.matchMedia("(prefers-reduced-motion: reduce)").matches,
      color: palette(),
      textStyle: { color: text, fontFamily: "Inter, sans-serif" },
      tooltip: { trigger: "item", renderMode: "richText", backgroundColor: cssVar("--sql-bg", "#1d2130"),
        textStyle: { color: cssVar("--sql-text", "#dce4ff") } },
      aria: { enabled: true, decal: { show: false } },
    };
    const format = value => formatNumber(value, compact);
    if (["pie", "donut"].includes(type)) {
      return { ...base, legend: { bottom: 0, type: "scroll", textStyle: { color: muted } },
        series: [{ type: "pie", radius: type === "donut" ? ["42%", "67%"] : "67%",
          data: data.map(row => ({ name: label(row[x]), value: row[y] })),
          label: { formatter: "{b}" } }] };
    }
    if (type === "treemap") {
      const parents = unique(data.map(row => row[x]));
      const children = parents.map(parent => ({
        name: parent,
        children: data.filter(row => label(row[x]) === parent).map(row => ({
          name: label(row[group]), value: row[y],
        })),
      }));
      return { ...base, series: [{ type: "treemap", data: children, roam: false,
        breadcrumb: { show: true }, label: { show: true } }] };
    }
    if (type === "calendar_heatmap") {
      const years = unique(data.map(row => canonicalDate(row[x]).slice(0, 4)));
      const min = Math.min(...data.map(row => Number(row[y])));
      const max = Math.max(...data.map(row => Number(row[y])));
      return { ...base, tooltip: { ...base.tooltip, formatter: item => `${item.value[0]}: ${format(item.value[1])}` },
        visualMap: { min, max, calculable: true, orient: "horizontal", left: "center", top: 10,
          inRange: { color: [cssVar("--accent-soft", "#efedff"), cssVar("--accent", "#6e60e6")] } },
        calendar: years.map((year, index) => ({ range: year, top: 65 + index * 190,
          cellSize: ["auto", 16], right: 24, left: 40,
          yearLabel: { color: muted }, monthLabel: { color: muted }, dayLabel: { color: muted } })),
        series: years.map((year, index) => ({ type: "heatmap", coordinateSystem: "calendar",
          calendarIndex: index, data: data.filter(row => canonicalDate(row[x]).startsWith(year))
            .map(row => [canonicalDate(row[x]), row[y]]) })) };
    }
    if (type === "heatmap") {
      const xs = unique(data.map(row => row[x]));
      const ys = unique(data.map(row => row[group]));
      const values = data.map(row => Number(row[y]));
      return { ...base, grid: { left: 80, right: 30, top: 35, bottom: 75 },
        xAxis: { type: "category", data: xs, name: spec.x_label, axisLabel: { color: muted } },
        yAxis: { type: "category", data: ys, name: group, axisLabel: { color: muted } },
        visualMap: { min: Math.min(...values), max: Math.max(...values), calculable: true,
          orient: "horizontal", left: "center", bottom: 0,
          inRange: { color: [cssVar("--accent-soft", "#efedff"), cssVar("--accent", "#6e60e6")] } },
        series: [{ type: "heatmap", data: data.map(row => [xs.indexOf(label(row[x])),
          ys.indexOf(label(row[group])), row[y]]) }] };
    }
    if (type === "box") {
      return { ...base, grid: { left: 65, right: 25, top: 25, bottom: 40 },
        xAxis: { type: "category", data: data.map(box => label(box[x])), axisLabel: { color: muted } },
        yAxis: { type: "value", name: spec.y_label, axisLabel: { formatter: format, color: muted } },
        series: [{ type: "boxplot", data: data.map(box =>
          [box.min, box.q1, box.median, box.q3, box.max]) }] };
    }
    const temporal = spec.time_format !== "raw" && data.every(row => Number.isFinite(Date.parse(
      `${canonicalDate(row[x])}T00:00:00Z`)));
    const category = unique(data.map(row => row[x])).sort((a, b) => temporal
      ? Date.parse(`${canonicalDate(a)}T00:00:00Z`) - Date.parse(`${canonicalDate(b)}T00:00:00Z`) : 0);
    const horizontal = type === "horizontal_bar";
    const numericX = ["scatter", "bubble"].includes(type);
    const grouped = ["multi_line", "stacked_area", "grouped_bar", "stacked_bar", "percent_stacked_bar"].includes(type);
    const lineType = ["line", "multi_line", "step_line", "area", "stacked_area"].includes(type);
    const valueAxis = { type: "value", axisLabel: { formatter: format, color: muted },
      splitLine: { lineStyle: { color: border, type: "dashed" } } };
    const categoryAxis = { type: "category", data: category.map(value => friendlyDate(value, spec.time_format)),
      name: spec.x_label, axisLabel: { color: muted, hideOverlap: true, rotate: category.length > 8 ? 30 : 0 } };
    const option = { ...base,
      grid: { left: 35, right: 35, top: 35, bottom: 55, containLabel: true },
      xAxis: horizontal ? valueAxis : numericX ? valueAxis : categoryAxis,
      yAxis: horizontal ? categoryAxis : { ...valueAxis, name: spec.y_label },
      dataZoom: temporal && category.length > 25 && !numericX
        ? [{ type: "slider", xAxisIndex: 0, bottom: 12, height: 22, filterMode: "filter",
          start: spec.zoom_start ?? 0, end: spec.zoom_end ?? 100 },
        { type: "inside", xAxisIndex: 0, filterMode: "filter",
          start: spec.zoom_start ?? 0, end: spec.zoom_end ?? 100 }] : [],
      legend: { show: spec.legend !== false && (grouped || measures.length > 1), type: "scroll", top: 0, textStyle: { color: muted } },
      series: [],
    };
    if (type === "histogram") {
      option.xAxis = { ...categoryAxis, data: data.map(row => formatNumber(row[x], false)) };
      option.yAxis.name = "Frequency";
    }
    if (numericX) {
      option.series = [{ type: "scatter", symbolSize: type === "bubble"
        ? item => Math.max(5, Math.min(45, Math.sqrt(Math.max(0, Number(item[2]))) * 3)) : 11,
      data: data.map(row => [row[x], row[y], type === "bubble" ? row[spec.size] : null]) }];
    } else if (measures.length > 1 && !group && ["multi_line", "grouped_bar"].includes(type)) {
      option.series = measures.map(name => {
        const points = new Map(data.map(row => [label(row[x]), row[name]]));
        return { name, type: type === "multi_line" ? "line" : "bar", connectNulls: false,
          data: category.map(value => points.has(value) ? points.get(value) : null) };
      });
    } else if (grouped) {
      option.series = unique(data.map(row => row[group])).map(name => {
        const points = new Map(data.filter(row => label(row[group]) === name)
          .map(row => [label(row[x]), row[y]]));
        const totals = type === "percent_stacked_bar" ? new Map(category.map(value => [value,
          data.filter(row => label(row[x]) === value).reduce((sum, row) => sum + Number(row[y]), 0)])) : null;
        return { name, type: lineType ? "line" : "bar", stack: type.startsWith("stacked") || type === "percent_stacked_bar" ? "total" : undefined,
          areaStyle: type === "stacked_area" ? {} : undefined,
          connectNulls: false, data: category.map(value => points.has(value)
            ? (totals ? (totals.get(value) > 0 ? Number(points.get(value)) / totals.get(value) * 100 : null) : points.get(value))
            : null) };
      });
    } else {
      option.series = [{ name: spec.y_label, type: lineType ? "line" : "bar",
        step: type === "step_line" ? "end" : undefined,
        areaStyle: type === "area" ? {} : undefined,
        connectNulls: false,
        data: category.map(value => data.find(row => label(row[x]) === value)?.[y] ?? null) }];
    }
    if (type === "percent_stacked_bar") {
      option.yAxis.name = "Share (%)";
      option.yAxis.max = 100;
    }
    if (option.dataZoom.length) option.grid.bottom = 68;
    return option;
  }

  function render(container, presentation) {
    if (!container || !window.echarts || !presentation?.chart_spec) return null;
    const spec = presentation.chart_spec;
    const option = buildOption(spec);
    if (!option) return null;
    const chart = window.echarts.init(container, null, { renderer: "canvas" });
    chart.setOption(option);
    const pinchEnabled = option.dataZoom.length > 0;
    let pinch = null;
    const distance = touches => Math.hypot(
      touches[0].clientX - touches[1].clientX,
      touches[0].clientY - touches[1].clientY,
    );
    const touchStart = event => {
      if (event.touches.length !== 2) return;
      const initialDistance = distance(event.touches);
      if (initialDistance < 8) return;
      const zoom = chart.getOption()?.dataZoom?.[0] || option.dataZoom[0];
      const bounds = container.getBoundingClientRect();
      const midpoint = (event.touches[0].clientX + event.touches[1].clientX) / 2;
      pinch = {
        distance: initialDistance,
        start: Number(zoom.start ?? 0), end: Number(zoom.end ?? 100),
        anchor: Math.max(0, Math.min(1, (midpoint - bounds.left) / Math.max(1, bounds.width))),
      };
    };
    const touchMove = event => {
      if (!pinch || event.touches.length !== 2) return;
      event.preventDefault();
      event.stopPropagation();
      const currentDistance = distance(event.touches);
      if (currentDistance < 8) return;
      const originalSpan = pinch.end - pinch.start;
      const span = Math.max(5, Math.min(100, originalSpan * pinch.distance / currentDistance));
      const center = pinch.start + pinch.anchor * originalSpan;
      const start = Math.max(0, Math.min(100 - span, center - pinch.anchor * span));
      chart.dispatchAction({ type: "dataZoom", start, end: start + span });
    };
    const touchEnd = event => { if (event.touches.length < 2) pinch = null; };
    if (pinchEnabled) {
      container.classList?.add?.("chart-slot-pinch");
      container.addEventListener("touchstart", touchStart, { passive: true, capture: true });
      container.addEventListener("touchmove", touchMove, { passive: false, capture: true });
      container.addEventListener("touchend", touchEnd, { passive: true, capture: true });
      container.addEventListener("touchcancel", touchEnd, { passive: true, capture: true });
    }
    const resize = () => chart.resize();
    window.addEventListener("resize", resize, { passive: true });
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(resize) : null;
    observer?.observe(container);
    return {
      dispose() {
        window.removeEventListener("resize", resize);
        observer?.disconnect();
        if (pinchEnabled) {
          container.removeEventListener("touchstart", touchStart, true);
          container.removeEventListener("touchmove", touchMove, true);
          container.removeEventListener("touchend", touchEnd, true);
          container.removeEventListener("touchcancel", touchEnd, true);
          container.classList?.remove?.("chart-slot-pinch");
        }
        chart.dispose();
      },
      onSelect(callback) { chart.on("click", point => callback({ ...point,
        rawX: ["scatter", "bubble"].includes(spec.chart_type)
          ? spec.data[point.dataIndex]?.[spec.x]
          : [...new Set(spec.data.map(row => label(row[spec.x])))].sort((a, b) =>
              spec.time_format !== "raw"
                ? Date.parse(`${canonicalDate(a)}T00:00:00Z`) - Date.parse(`${canonicalDate(b)}T00:00:00Z`) : 0)[point.dataIndex],
      })); },
      onZoom(callback) { chart.on("datazoom", () => {
        const zoom = chart.getOption()?.dataZoom?.[0];
        if (zoom) callback({ start: zoom.start, end: zoom.end });
      }); },
      resetZoom() { chart.dispatchAction({ type: "dataZoom", start: 0, end: 100 }); },
      resize() { chart.resize(); },
      getDataURL(options) { return chart.getDataURL({ type: "png", pixelRatio: 2,
        backgroundColor: cssVar("--surface", "#fff"), ...options }); },
    };
  }
  return { render, supported, buildOption };
})();
