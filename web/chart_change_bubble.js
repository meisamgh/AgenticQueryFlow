/* Chart-title comparison view. Consumes existing verified KPI evidence only. */
window.ChartChangeBubble = (() => {
  function build(response, chart, kpis) {
    const result=response.generation;
    if (!response.result_id || !result?.accepted || result.truncated ||
        result.row_count !== result.rows?.length) return null;
    const comparison=kpis.find(kpi=>kpi.calculation === "PERIOD_CHANGE" && kpi.metric === chart.y
      && kpi.evidence.result_id === response.result_id);
    if (!comparison?.comparison) return null;
    const x=result.columns.indexOf(chart.x), y=result.columns.indexOf(chart.y);
    if (x<0 || y<0 || chart.series) return null;
    const {baseline,current,change}=comparison.comparison;
    const before=result.rows.filter(row=>String(row[x])===baseline);
    const after=result.rows.filter(row=>String(row[x])===current);
    if (before.length!==1 || after.length!==1 || typeof before[0][y]!=="number" ||
        typeof after[0][y]!=="number" || before[0][y]<=0 || !Number.isFinite(change)) return null;
    const expected=(after[0][y]-before[0][y])/Math.abs(before[0][y])*100;
    if (!Number.isFinite(expected) || Math.abs(expected-change)>1e-9*Math.max(1,Math.abs(expected))) return null;
    const unit=chart.measure_units?.[chart.y];
    const format=(value)=>window.QueryflowKpis.format(value,unit);
    const sparkline=kpis.find(kpi=>kpi.metric===chart.y && kpi.evidence.result_id===response.result_id
      && kpi.sparkline.length>1)?.sparkline || [];
    // No approved polarity field exists in the current contract. Direction is
    // therefore informational, never labeled favorable/unfavorable by sign.
    return {result_id:response.result_id,tone:"neutral",change,
      period_label:comparison.badge || `${current} vs ${baseline}`,
      exact_data:{baseline,current,start_value:before[0][y],end_value:after[0][y],unit},
      text:`${change>0 ? "↑ +" : change<0 ? "↓ " : ""}${window.QueryflowKpis.format(change,"%")}`,
      sparkline:sparkline.slice(),
      tooltip:`Current (${current}): ${format(after[0][y])} [${after[0][y]}]. `+
        `Comparison (${baseline}): ${format(before[0][y])} [${before[0][y]}]. `+
        `(${after[0][y]} − ${before[0][y]}) / abs(${before[0][y]}) × 100 = ${change}%. `+
        "Direction only; business improvement or worsening is not established."};
  }
  function render(container, model) {
    container.replaceChildren();container.hidden=!model;
    if (!model) return;
    container.className="chart-change-badge chart-change-bubble";
    container.dataset.tone=model.tone;container.dataset.resultId=model.result_id;
    container.title=model.tooltip;container.setAttribute("aria-label",`${model.text}. ${model.tooltip}`);
    const value=document.createElement("strong");value.textContent=model.text;container.append(value);
    const period=document.createElement("small");period.textContent=model.period_label;container.append(period);
    const details=document.createElement("details");details.className="chart-exact-data";
    const summary=document.createElement("summary");summary.textContent="Exact data";details.append(summary);
    const explanation=document.createElement("span");explanation.textContent=model.tooltip;
    details.append(explanation);container.append(details);
    if (model.sparkline.length>1 && model.sparkline.every(Number.isFinite)) {
      const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");
      svg.setAttribute("viewBox","0 0 48 24");svg.setAttribute("aria-hidden","true");
      const path=document.createElementNS("http://www.w3.org/2000/svg","polyline");
      const min=Math.min(...model.sparkline),span=Math.max(...model.sparkline)-min;
      path.setAttribute("points",model.sparkline.map((v,i)=>`${2+i*44/(model.sparkline.length-1)},${span ? 21-(v-min)/span*18 : 12}`).join(" "));
      svg.append(path);container.append(svg);
    }
  }
  return {build,render};
})();
