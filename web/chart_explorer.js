/* Display-only chart choices over one complete, backend-validated result.
   This module never invents rows, aggregates measures, or executes SQL. */
window.QueryflowChartExplorer = (function () {
  const TIME_TYPES = new Set(["line", "multi_line", "step_line", "area", "stacked_area"]);
  const NON_ADDITIVE = /(?:average|avg|ratio|rate|percent|share|margin|distinct|median|price|per_)/i;
  function distinct(rows, key) { return [...new Set(rows.map(row => String(row[key])))]; }
  function chronological(value) {
    const raw = String(value);
    const iso = /^\d{6}$/.test(raw) ? `${raw.slice(0, 4)}-${raw.slice(4)}-01`
      : /^\d{4}$/.test(raw) ? `${raw}-01-01`
      : /^\d{4}-\d{2}$/.test(raw) ? `${raw}-01` : raw;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(iso)) return null;
    const stamp = Date.parse(`${iso}T00:00:00Z`);
    return Number.isFinite(stamp) && new Date(stamp).toISOString().slice(0, 10) === iso
      ? stamp : null;
  }
  function isTime(spec) {
    return spec.time_format !== "raw" && spec.data.every(row => chronological(row[spec.x]) !== null);
  }
  function numericColumns(spec) {
    return (spec.source_columns || Object.keys(spec.data[0] || {})).filter(column =>
      !/(?:^|_)(?:id|key|code|year|month)(?:$|_)/i.test(column) &&
      spec.data.some(row => row[column] !== null && row[column] !== ""
        && Number.isFinite(Number(row[column]))) &&
      spec.data.every(row => row[column] === null
        || (row[column] !== "" && Number.isFinite(Number(row[column])))));
  }
  function completeGrid(spec) {
    if (!spec.series) return false;
    const xs = distinct(spec.data, spec.x), groups = distinct(spec.data, spec.series);
    return spec.data.length === xs.length * groups.length
      && new Set(spec.data.map(row => `${JSON.stringify(row[spec.x])}|${JSON.stringify(row[spec.series])}`)).size
        === spec.data.length;
  }
  function nonnegative(spec) { return spec.data.every(row => row[spec.y] !== null
    && row[spec.y] !== "" && Number.isFinite(Number(row[spec.y])) && Number(row[spec.y]) >= 0); }
  function additive(spec) { return !NON_ADDITIVE.test(spec.y) && spec.additive_measures?.[spec.y] !== false; }
  function dimensions(spec) {
    const numeric = new Set(numericColumns(spec));
    return (spec.source_columns || Object.keys(spec.data[0] || {})).filter(column => !numeric.has(column));
  }
  function unitFor(name, spec) {
    if (spec.measure_units?.[name]) return spec.measure_units[name];
    if (/(?:^|_)(?:count|orders|customers|units|quantity)(?:$|_)/i.test(name)) return "count";
    if (/(?:revenue|cost|profit|sales|spend|price)/i.test(name)) return "money";
    if (/(?:percent|rate|ratio|share|margin)/i.test(name)) return "ratio";
    return "unknown:" + name;
  }
  function compatibleMeasures(names, spec) {
    return names.length <= 1 || names.every(name => unitFor(name, spec) === unitFor(names[0], spec));
  }
  function choices(spec) {
    if (!spec?.data?.length) return [];
    if (spec.transformation && spec.transformation !== "none") return [spec.chart_type];
    const options = [spec.chart_type];
    const time = isTime(spec), nonnegativeValues = nonnegative(spec);
    if (time) {
      if (!spec.series || distinct(spec.data, spec.series).length === 1)
        options.push("line", "step_line", "area", "bar");
      if (spec.series) options.push("multi_line", "grouped_bar");
      if (spec.series && completeGrid(spec) && nonnegativeValues && additive(spec))
        options.push("stacked_area", "stacked_bar", "percent_stacked_bar");
    } else if (spec.chart_type !== "histogram" && spec.chart_type !== "box"
      && spec.chart_type !== "scatter" && spec.chart_type !== "bubble") {
      if (!spec.series || distinct(spec.data, spec.series).length === 1) options.push("bar", "horizontal_bar");
      if (spec.series) options.push("grouped_bar");
      if (spec.series && completeGrid(spec) && nonnegativeValues && additive(spec))
        options.push("stacked_bar", "percent_stacked_bar");
      if (!spec.series && distinct(spec.data, spec.x).length <= 12 && nonnegativeValues
        && spec.data.some(row => Number(row[spec.y]) > 0)) options.push("donut", "pie");
    }
    if (spec.series && spec.data.every(row => row[spec.y] != null && row[spec.y] !== ""
      && Number.isFinite(Number(row[spec.y])))) options.push("heatmap");
    if (nonnegativeValues && additive(spec) && spec.data.some(row => Number(row[spec.y]) > 0))
      options.push("treemap");
    if (!time && !spec.series && spec.data.every(row => typeof row[spec.x]==="number" && Number.isFinite(row[spec.x])))
      options.push("scatter");
    return [...new Set(options)];
  }
  function filters(spec) {
    if (spec.transformation && spec.transformation !== "none") return { metric: [], categories: [], dates: [] };
    const result = { metric: numericColumns(spec).filter(column => column !== spec.x),
      dimensions: dimensions(spec), categories: [], dates: [] };
    if (isTime(spec)) result.dates = distinct(spec.data, spec.x).sort((a, b) => chronological(a) - chronological(b));
    if (spec.series) result.categories = distinct(spec.data, spec.series);
    else if (!isTime(spec) && !numericColumns(spec).includes(spec.x))
      result.categories = distinct(spec.data, spec.x);
    return result;
  }
  function orderedChoices(spec, preferred=[], sourceType=spec.chart_type) {
    const available=choices({...spec,chart_type:sourceType});
    const order=[...new Set([...preferred,"line","multi_line","step_line","area","stacked_area",
      "bar","horizontal_bar","grouped_bar","stacked_bar","percent_stacked_bar",
      "scatter","bubble","histogram","box","heatmap","pie","donut","treemap","calendar_heatmap"])];
    return order.filter(type=>available.includes(type));
  }
  function apply(spec, settings = {}) {
    if (!spec?.data?.length) return null;
    const available = filters(spec);
    const selectedX = available.dimensions.includes(settings.x) || available.metric.includes(settings.x)
      ? settings.x : spec.x;
    const selectedSeries = settings.series === "" || settings.series === selectedX ? null
      : available.dimensions.includes(settings.series) && settings.series !== selectedX
        ? settings.series : spec.series === selectedX ? null : spec.series;
    const metric = available.metric.includes(settings.metric) ? settings.metric : spec.y;
    const requestedMeasures = Array.isArray(settings.metrics) ? settings.metrics.filter(name => available.metric.includes(name)) : [];
    const measures = requestedMeasures.length && compatibleMeasures(requestedMeasures, spec)
      ? requestedMeasures : [metric];
    const filterKey = spec.series || (!isTime(spec) ? spec.x : null);
    const selectedCategories = Array.isArray(settings.categories) ? settings.categories.map(String) : [];
    const rowFilters = settings.rowFilters && typeof settings.rowFilters === "object"
      ? settings.rowFilters : {};
    const rowFilterEntries = Object.entries(rowFilters)
      .filter(([column, values]) => spec.data.length && column in spec.data[0] && Array.isArray(values) && values.length);
    let rows = spec.data.filter(row =>
      (!filterKey || !selectedCategories.length || selectedCategories.includes(String(row[filterKey])))
      && (!isTime(spec) || (!settings.start || chronological(row[spec.x]) >= chronological(settings.start))
        && (!settings.end || chronological(row[spec.x]) <= chronological(settings.end)))
      && rowFilterEntries.every(([column, values]) => values.includes(String(row[column]))));
    if (settings.topN && Number.isInteger(settings.topN) && settings.topN > 0) {
      // Ranking is display-only and never sums multiple rows at a category.
      if (distinct(rows, spec.x).length === rows.length) {
        const keep = new Set([...rows].sort((a, b) => Number(b[metric]) - Number(a[metric]))
          .slice(0, settings.topN).map(row => String(row[spec.x])));
        rows = rows.filter(row => keep.has(String(row[spec.x])));
      }
    }
    if (isTime(spec)) rows = [...rows].sort((a, b) => chronological(a[spec.x]) - chronological(b[spec.x]));
    const displayPalette = ["violet", "ocean", "forest", "sunset"].includes(settings.palette)
      ? settings.palette : "violet";
    const output = { ...spec, x: selectedX, x_label: selectedX.replaceAll("_", " "),
      series: measures.length > 1 ? null : selectedSeries, y: measures[0], y_measures: measures,
      y_label: measures.length === 1 ? measures[0].replaceAll("_", " ") : unitFor(measures[0], spec),
      time_format: selectedX === spec.x ? spec.time_format : "raw", data: rows,
      display_palette: displayPalette };
    // Never collapse multiple source rows onto one plotted position when a
    // display-only axis/series switch changes the grain. That needs new SQL.
    if ((!output.transformation || output.transformation === "none")
        && !["scatter", "bubble", "histogram", "box"].includes(spec.chart_type)) {
      const positions = rows.map(row => JSON.stringify([row[selectedX], selectedSeries ? row[selectedSeries] : null]));
      if (new Set(positions).size !== positions.length) return null;
    }
    const supported = choices(output);
    const wanted = settings.chartType || (measures.length > 1 ? "multi_line" : spec.chart_type);
    output.chart_type = measures.length > 1
      ? (isTime(output) ? "multi_line" : "grouped_bar")
      : supported.includes(wanted) ? wanted : (supported.includes(spec.chart_type) ? spec.chart_type : supported[0]);
    output.legend = settings.legend !== false && (measures.length > 1 || Boolean(selectedSeries));
    return output;
  }
  return { choices, orderedChoices, filters, apply, isTime, chronological, completeGrid, numericColumns,
    compatibleMeasures, unitFor };
})();
