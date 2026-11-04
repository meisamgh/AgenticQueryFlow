/* Deterministic descriptive KPIs over a complete accepted result. No SQL/LLM calls. */
window.QueryflowKpis = (() => {
  /** @typedef {{metric:string, calculation:string, title:string, value:number,
   * formatted_value:string, definition:string, sparkline:number[], unit?:string,
   * series_name?:string, comparison?:{baseline:string,current:string,change:number},
   * badge?:string, point?:{x:string|number,y:number,series_name:string}|null,
   * comparison_evidence?:{baseline_value:number,current_value:number,unit?:string,sparkline:number[]},
   * evidence:{result_id:string, column:string, row_count:number}}} KpiSpec */
  const label = name => String(name).replaceAll("_", " ").replace(/([a-z])([A-Z])/g, "$1 $2");
  const format = (value, unit, locale) => {
    if (window.QueryflowFormat) return window.QueryflowFormat.formatNumber(value,unit,locale,{compact:Math.abs(value)>=1000});
    const options = {maximumFractionDigits:2, notation:Math.abs(value)>=1000 ? "compact" : "standard"};
    if (unit === "count" && Number.isInteger(value)) return new Intl.NumberFormat(locale,{maximumFractionDigits:0}).format(value);
    if (/^[A-Z]{3}$/.test(unit || "")) {
      try { return new Intl.NumberFormat(locale,{...options,style:"currency",currency:unit}).format(value); }
      catch { /* Unknown units remain explicit, never guessed from metric names. */ }
    }
    if (unit === "ratio") return new Intl.NumberFormat(locale,{style:"percent",maximumFractionDigits:2}).format(value);
    if (unit === "%" || unit === "percent") return new Intl.NumberFormat(locale,{maximumFractionDigits:2}).format(value)+"%";
    return new Intl.NumberFormat(locale,options).format(value) + (unit ? ` ${unit}` : "");
  };
  const dateKey = (value, granularity) => {
    const raw=String(value);
    const iso=granularity === "year" && /^\d{4}$/.test(raw) ? `${raw}-01-01`
      : granularity === "month" && /^\d{6}$/.test(raw) ? `${raw.slice(0,4)}-${raw.slice(4)}-01`
      : granularity === "month" && /^\d{4}-\d{2}$/.test(raw) ? `${raw}-01` : raw;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(iso)) return null;
    const key=Date.parse(`${iso}T00:00:00Z`);
    return Number.isFinite(key) && new Date(key).toISOString().slice(0,10)===iso ? key : null;
  };
  // Presentation-only evidence decoration; calculations and source rows stay unchanged.
  function decorate(spec, result, chart) {
    const x=result.columns.indexOf(chart.x), y=result.columns.indexOf(spec.metric);
    const series=result.columns.indexOf(chart.series);
    const point=["MIN","MAX"].includes(spec.calculation) && x>=0 ? result.rows.find(row=>
      row[y]===spec.value && (series<0 || String(row[series])===spec.series_name)) : null;
    const rawPeriod=point?.[x];
    const stamp=chart.time_format && chart.time_format!=="raw" ? dateKey(rawPeriod,chart.time_format) : null;
    const period=stamp!=null ? new Intl.DateTimeFormat(undefined,{timeZone:"UTC",year:"numeric",
      ...(chart.time_format!=="year" ? {month:"short"} : {}),
      ...(chart.time_format==="day" ? {day:"numeric"} : {})}).format(new Date(stamp)) : null;
    const periodLabel=value=>{
      const key=dateKey(value,chart.time_format);
      return key==null ? String(value) : new Intl.DateTimeFormat(undefined,{timeZone:"UTC",year:"numeric",
        ...(chart.time_format!=="year" ? {month:"short"} : {}),
        ...(chart.time_format==="day" ? {day:"numeric"} : {})}).format(new Date(key));
    };
    return {...spec, point:point ? {x:rawPeriod,y:spec.value,series_name:spec.series_name} : null,
      badge:spec.comparison ? `${periodLabel(spec.comparison.current)} vs ${periodLabel(spec.comparison.baseline)}`
        : period ? `${spec.calculation==="MAX" ? "Peak" : "Lowest"} · ${period}`
        : `${spec.evidence.row_count} ${spec.sparkline.length ? "observed periods" : "returned values"}`};
  }
  /** @returns {KpiSpec[]} */
  function build(response, chart) {
    const result = response.generation;
    if (!response.result_id || !result?.accepted || result.truncated ||
        !result.rows?.length || result.rows.length > 10000 || result.row_count !== result.rows.length ||
        (response.presentation_state?.source_result_id &&
         response.presentation_state.source_result_id !== response.result_id)) return [];
    const index = result.columns?.indexOf(chart?.y);
    if (index == null || index < 0 || result.rows.length < 2) return [];
    const values = result.rows.map(row => row[index]);
    if (!values.every(value => typeof value === "number" && Number.isFinite(value))) return [];
    // Summary statistics describe retained result rows. They do not assert that
    // the measure is additive, distinct customers, revenue, or a growth baseline.
    const contract = result.semantic_contract || response.state?.semantic_contract || {};
    const approved = (contract.approved_metrics || []).find(item => item.id === chart.y);
    const metric = chart.y_label || label(chart.y);
    const unit=chart.measure_units?.[chart.y];
    const xIndex=result.columns.indexOf(chart.x);
    const temporal = !chart.series && chart.time_format && chart.time_format !== "raw" && xIndex >= 0
      && result.rows.every(row => dateKey(row[xIndex],chart.time_format) !== null)
      && new Set(result.rows.map(row=>String(row[xIndex]))).size === result.rows.length;
    const ordered=temporal ? result.rows.slice().sort((a,b)=>dateKey(a[xIndex],chart.time_format)-dateKey(b[xIndex],chart.time_format)) : [];
    if (chart.series) {
      const seriesIndex=result.columns.indexOf(chart.series);
      if (seriesIndex < 0) return [];
      const groups=[...new Set(result.rows.map(row=>row[seriesIndex]))];
      if (groups.length > 4 || groups.some(group=>group == null)) return [];
      return groups.flatMap(group => {
        const rows=result.rows.filter(row=>row[seriesIndex]===group);
        const points=rows.map(row=>row[index]);
        const canTrend=chart.time_format && chart.time_format !== "raw" && xIndex>=0
          && rows.every(row=>dateKey(row[xIndex],chart.time_format)!==null)
          && new Set(rows.map(row=>String(row[xIndex]))).size===rows.length;
        const trend=canTrend ? rows.slice().sort((a,b)=>dateKey(a[xIndex],chart.time_format)-dateKey(b[xIndex],chart.time_format)).map(row=>row[index]) : [];
        return (groups.length===1 ? [["MIN","Lowest",Math.min(...points)], ["MAX","Highest",Math.max(...points)]]
          : [["MAX","Highest",Math.max(...points)]]).map(([calculation,title,value])=>({
          metric:chart.y,calculation,title:`${title} ${metric} · ${group}`,value,
          formatted_value:format(value,unit),unit,series_name:String(group),
          definition:`${title} of ${points.length} returned ${metric} values for ${group}.`,
          sparkline:trend,evidence:{result_id:response.result_id,column:chart.y,row_count:points.length},
        }));
      }).map(spec=>decorate(spec,result,chart));
    }
    const evidence = {result_id: response.result_id, column: chart.y, row_count: values.length};
    const candidates = [
      ["MIN", "Lowest", Math.min(...values)],
      ["MAX", "Highest", Math.max(...values)],
    ];
    // Only comparable single-series records receive an unweighted row average.
    if (!chart.series && values.length <= 10000) {
      candidates.unshift(["ROW_MEAN", "Average", values.reduce((a,b) => a+b, 0) / values.length]);
    }
    const specs = candidates.filter(([, , value]) => Number.isFinite(value)).map(([calculation, title, value]) => ({
      metric: chart.y, calculation, title: `${title} ${metric}`, value,
      formatted_value: format(value,unit), unit, series_name: chart.y_label || chart.y,
      definition: `${title} of ${values.length} returned ${metric} values${calculation === "ROW_MEAN" ? " (unweighted row average)" : ""}. Not a recomputed business total.${approved ? ` Approved definition: ${approved.formula}` : ""}`,
      sparkline: temporal ? ordered.map(row=>row[index]) : [], evidence,
    }));
    const sumInput=approved?.formula?.match(/^SUM\(\s*([\w.]+)\s*\)$/i)?.[1];
    const sumStage=(contract.aggregation_stages || []).find(stage => stage.name === chart.y
      && stage.function === "sum" && stage.input === sumInput
      && stage.group_by?.some(field => field === chart.x || field.endsWith(`.${chart.x}`)));
    if (sumInput && sumStage && temporal && !contract.limit) {
      const total=values.reduce((a,b)=>a+b,0);
      if (Number.isFinite(total)) specs.unshift({metric:chart.y,calculation:"SUM_DISJOINT_PERIODS",
        title:`Total returned ${metric}`,value:total,formatted_value:format(total,unit),unit,
        definition:`Sum of ${values.length} unique returned periods. Approved formula: ${approved.formula}.`,
        sparkline:ordered.map(row=>row[index]),series_name:chart.y_label || chart.y,evidence});
    }
    // Explicit comparison periods must each identify exactly one source row.
    // No inferred previous-year baseline or summation across periods.
    let periods=contract.comparison_periods || [];
    // Default chart comparison is explicitly start-to-end across the returned
    // series, not an implied previous-period or year-over-year comparison.
    if (!periods.length && temporal && ordered.length>=2) {
      periods=[ordered[0][xIndex],ordered.at(-1)[xIndex]];
    }
    if (temporal && periods.length===2) {
      const baseline=ordered.find(row=>String(row[xIndex])===String(periods[0]));
      const current=ordered.find(row=>String(row[xIndex])===String(periods[1]));
      if (baseline && current && baseline[index] > 0) {
        const change=(current[index]-baseline[index])/baseline[index]*100;
        if (Number.isFinite(change)) specs.push({metric:chart.y,calculation:"PERIOD_CHANGE",
          title:`${metric} change`,value:change,formatted_value:format(change,"%"),
          definition:`(${periods[1]} value − ${periods[0]} value) / ${periods[0]} value × 100.`,
          sparkline:[],comparison:{baseline:String(periods[0]),current:String(periods[1]),change},
          comparison_evidence:{baseline_value:baseline[index],current_value:current[index],
            unit,sparkline:ordered.map(row=>row[index])},
          series_name:chart.y_label || chart.y,evidence});
      }
    }
    return (specs.length > 4 ? specs.filter(spec=>spec.calculation !== "ROW_MEAN").slice(0,4) : specs)
      .map(spec=>decorate(spec,result,chart));
  }
  function iconFor(spec) {
    if (spec.calculation === "MIN") return "MIN";
    if (spec.calculation === "MAX") return "MAX";
    if (spec.calculation === "PERIOD_CHANGE") return "GROWTH";
    if (spec.calculation === "ROW_MEAN" || /average|mean/i.test(spec.calculation)) return "AVERAGE";
    const metric=String(spec.metric || "").replaceAll("_"," ");
    if (/\b(revenue|spending|spend|payment|sales)\b/i.test(metric)) return "REVENUE";
    if (/\bcustomers?\b/i.test(metric)) return "CUSTOMERS";
    if (/\borders?\b/i.test(metric)) return "ORDERS";
    if (["%","percent","ratio"].includes(spec.unit)) return "PERCENT";
    if (/\bconsumption\b/i.test(metric)) return "CONSUMPTION";
    if (spec.calculation === "SUM_DISJOINT_PERIODS") return "TOTAL";
    return "NEUTRAL";
  }
  function comparisonBadge(specs) {
    const spec=specs.find(item=>item.calculation === "PERIOD_CHANGE" && item.comparison);
    if (!spec || !Number.isFinite(spec.comparison.change)) return null;
    const change=spec.comparison.change;
    return {text:`${change>0 ? "↑ +" : change<0 ? "↓ " : ""}${format(change,"%")}`,
      period:spec.badge || `${spec.comparison.current} vs ${spec.comparison.baseline}`,
      description:"Direction only; an increase or decrease is not necessarily better or worse.",
      result_id:spec.evidence.result_id};
  }
  function render(container, specs, onSelect) {
    container.replaceChildren(); container.hidden = !specs.length;
    const glyphs={MIN:"M4 20h16M6 10l6 6 6-6M12 3v13",MAX:"M4 4h16M6 14l6-6 6 6M12 8v13",
      AVERAGE:"M12 16v5M16 14v7M20 10v11M4 20h.01M8 16v5M3 10l5-5 5 5 8-8",TOTAL:"M18 4H6l6 8-6 8h12",
      GROWTH:"M3 17l6-6 4 3 8-10M15 4h6v6",
      REVENUE:"M3 6h17v14H3ZM3 6V4h14v2M15 11h6v5h-6ZM17 13h1",
      CUSTOMERS:"M3 21v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8M17 4a3 3 0 0 1 0 6M18 14a4 4 0 0 1 3 4v3",
      ORDERS:"M2 3h3l3 13h11l3-9H6M9 21h1M18 21h1",
      PERCENT:"M5 19 19 5M6 3a3 3 0 1 0 0 6 3 3 0 0 0 0-6M18 15a3 3 0 1 0 0 6 3 3 0 0 0 0-6",
      CONSUMPTION:"M3 18a10 10 0 1 1 18 0M12 13l5-6M5 18h14",
      NEUTRAL:"M3 3v18h18M6 16l4-5 4 2 6-8"};
    const glyph = calculation => {
      const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");
      svg.setAttribute("viewBox","0 0 24 24"); svg.setAttribute("aria-hidden","true");
      svg.setAttribute("data-lucide",({MIN:"arrow-down-to-line",MAX:"arrow-up-to-line",
        AVERAGE:"chart-no-axes-combined",TOTAL:"sigma",GROWTH:"trending-up",REVENUE:"wallet",
        CUSTOMERS:"users",ORDERS:"shopping-cart",PERCENT:"percent",CONSUMPTION:"gauge",
        NEUTRAL:"chart-no-axes-combined"})[calculation] || "chart-no-axes-combined");
      const path=document.createElementNS("http://www.w3.org/2000/svg","path");
      path.setAttribute("d",glyphs[calculation] || glyphs.NEUTRAL);svg.append(path);return svg;
    };
    for (const spec of specs) {
      const card = document.createElement("button"); card.type = "button";
      card.className = "chart-kpi-card"; card.dataset.resultId = spec.evidence.result_id;
      card.dataset.accent=spec.calculation==="MIN" ? "amber" : spec.calculation==="MAX" ? "violet" : "green";
      const exact=`${spec.value}${spec.unit ? ` ${spec.unit}` : ""}`;
      card.title = `Exact value: ${exact}. ${spec.definition}`;
      card.setAttribute("aria-label", `${spec.title}: ${spec.formatted_value}. Exact value: ${exact}. ${spec.definition}`);
      const header=document.createElement("div");header.className="kpi-card-heading";
      const bubble=document.createElement("span");bubble.className="kpi-icon-bubble";
      bubble.dataset.icon=iconFor(spec);bubble.append(glyph(iconFor(spec)));
      const title = document.createElement("span"); title.className="kpi-card-title";
      title.textContent = window.QueryflowChartTheme?.label(spec.title) || spec.title;
      title.title = spec.definition;
      header.append(bubble,title);
      const body=document.createElement("div");body.className="kpi-value-row";
      const value = document.createElement("strong"); value.textContent = spec.formatted_value;
      body.append(value);
      const comparisonBubble=document.createElement("span");comparisonBubble.className="kpi-comparison-bubble";
      comparisonBubble.dataset.tone="neutral";
      const evidence=spec.comparison_evidence;
      const supported=spec.calculation === "PERIOD_CHANGE" && spec.comparison && evidence
        && Number.isFinite(evidence.current_value) && evidence.baseline_value>0
        && Number.isFinite(spec.comparison.change)
        && Math.abs(spec.comparison.change-(evidence.current_value-evidence.baseline_value)/Math.abs(evidence.baseline_value)*100)<1e-8;
      card.dataset.comparison=String(Boolean(supported));
      const change=document.createElement("strong"),period=document.createElement("small");
      const trend=supported ? evidence.sparkline : spec.sparkline;
      if (supported) {
        change.textContent=`${spec.comparison.change>0 ? "↑ +" : spec.comparison.change<0 ? "↓ " : ""}${format(spec.comparison.change,"%")}`;
        period.textContent=spec.badge || `${spec.comparison.current} vs ${spec.comparison.baseline}`;
        comparisonBubble.title=`Current (${spec.comparison.current}): ${format(evidence.current_value,evidence.unit)} [${evidence.current_value}]. Baseline (${spec.comparison.baseline}): ${format(evidence.baseline_value,evidence.unit)} [${evidence.baseline_value}]. ${spec.definition} Direction is not a business-performance judgment.`;
      } else {
        change.textContent=["MIN","MAX"].includes(spec.calculation) ? spec.badge : `${trend.length} periods analyzed`;
        period.textContent="";
        comparisonBubble.title=spec.definition;
      }
      const comparisonLabel=document.createElement("span");comparisonLabel.className="kpi-comparison-label";
      comparisonLabel.append(change,period);comparisonBubble.append(comparisonLabel);
      if (supported || trend.length > 1 || (["MIN","MAX"].includes(spec.calculation) && spec.badge)) body.append(comparisonBubble);
      else body.classList.add("kpi-value-only");
      if (trend.length > 1) {
        const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");
        svg.setAttribute("viewBox","0 0 160 30"); svg.setAttribute("class","kpi-sparkline");
        svg.setAttribute("aria-label","Trend across returned periods, chronological order");
        const path=document.createElementNS("http://www.w3.org/2000/svg","polyline");
        const min=Math.min(...trend), span=Math.max(...trend)-min;
        path.setAttribute("points",trend.map((v,i)=>`${i*160/(trend.length-1)},${span ? 27-(v-min)/span*24 : 15}`).join(" "));
        svg.append(path);
        if (["MIN","MAX"].includes(spec.calculation)) {
          const markerIndex=trend.indexOf(spec.value);
          if (markerIndex>=0) {
            const marker=document.createElementNS("http://www.w3.org/2000/svg","circle");
            marker.setAttribute("cx",String(markerIndex*160/(trend.length-1)));
            marker.setAttribute("cy",String(span ? 27-(spec.value-min)/span*24 : 15));
            marker.setAttribute("r","3.5");svg.append(marker);
          }
        }
        comparisonBubble.append(svg);
      }
      const badge=document.createElement("small");badge.className="kpi-context-badge";
      badge.textContent=spec.badge || `${spec.evidence.row_count} returned values`;
      if (spec.comparison) badge.textContent=`${spec.evidence.row_count} observed periods`;
      card.append(header,body,badge);
      card.addEventListener("click",()=>onSelect?.(spec));
      container.append(card);
    }
  }
  return {build, render, format, iconFor, comparisonBadge};
})();
