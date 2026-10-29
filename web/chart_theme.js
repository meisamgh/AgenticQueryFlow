/* Shared presentation only. No data transforms, SQL or model calls. */
window.QueryflowChartTheme = Object.freeze({
  colors:Object.freeze(["#168447","#3B82F6","#F97316","#8B5CF6","#86CFA0","#FBBF24","#0F766E","#DB2777"]),
  colorFor(name, colors) {
    let hash=0; for (const character of String(name)) hash=(hash*31+character.charCodeAt(0))>>>0;
    return colors[hash%colors.length];
  },
  reducedMotion() { return Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches); },
  label(value) { return String(value ?? "").replace(/_/g," ").replace(/([a-z])([A-Z])/g,"$1 $2").replace(/^./,c=>c.toUpperCase()); },
  exact(value,unit) {
    if (value==null) return "—";
    if (!Number.isFinite(value)) return String(value);
    const number=new Intl.NumberFormat(undefined,{maximumSignificantDigits:17}).format(value);
    return number+(unit ? ` ${unit}` : "");
  },
  compact(value,unit) {
    if (value==null || !Number.isFinite(value)) return "";
    if (window.QueryflowKpis) return window.QueryflowKpis.format(value,unit);
    const options={maximumFractionDigits:2};
    if (unit==="ratio") return new Intl.NumberFormat(undefined,{...options,style:"percent"}).format(value);
    if (unit==="%" || unit==="percent") return new Intl.NumberFormat(undefined,options).format(value)+"%";
    if (/^[A-Z]{3}$/.test(unit || "")) {
      try { return new Intl.NumberFormat(undefined,{...options,notation:Math.abs(value)>=1000 ? "compact" : "standard",style:"currency",currency:unit}).format(value); }
      catch { /* Unrecognized units are never interpreted as currency. */ }
    }
    return new Intl.NumberFormat(undefined,{...options,notation:Math.abs(value)>=1000 ? "compact" : "standard"}).format(value);
  },
  height(spec,width=800) {
    const categories=new Set(spec.data.map(row=>String(row[spec.x]))).size;
    const groups=spec.series ? new Set(spec.data.map(row=>String(row[spec.series]))).size : spec.y_measures?.length || 1;
    return Math.min(width<600 ? 380 : 440,Math.max(250,spec.chart_type==="horizontal_bar" ? categories*26+95 : 260)+(groups>3 ? 40 : 0));
  },
});
