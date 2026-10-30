/* Shared presentation only. No data transforms, SQL or model calls. */
window.QueryflowChartTheme = (() => {
const assignments = new Map();
return Object.freeze({
  colors:Object.freeze(["#168447","#2563EB","#C2410C","#7C3AED","#0F766E","#A16207","#BE185D","#475569"]),
  colorFor(name, colors) {
    const key=colors.join(",");
    if (!assignments.has(key)) assignments.set(key,new Map());
    const assigned=assignments.get(key), identity=String(name);
    if (assigned.has(identity)) return assigned.get(identity);
    let hash=0; for (const character of String(name)) hash=(hash*31+character.charCodeAt(0))>>>0;
    let index=hash%colors.length;
    const used=new Set(assigned.values());
    if (used.size<colors.length) while (used.has(colors[index])) index=(index+1)%colors.length;
    // Bound retention; visual identities beyond this cap can be reassigned.
    if (assigned.size>=256) assigned.clear();
    assigned.set(identity,colors[index]);
    return colors[index];
  },
  reducedMotion() { return Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches); },
  label(value) { return String(value ?? "").replace(/_/g," ").replace(/([a-z])([A-Z])/g,"$1 $2").replace(/^./,c=>c.toUpperCase()); },
  title(spec) {
    return this.label(spec.title || `${spec.y_label || spec.y} by ${spec.x_label || spec.x}`);
  },
  shortLabel(value, limit=22) {
    const full=String(value ?? "");
    return full.length>limit ? `${full.slice(0,limit-1)}…` : full;
  },
  axisTicks(values, count=7) {
    // Thin labels, never data. Include both endpoints without sorting/mutating.
    if(values.length<=count) return values.slice();
    return Array.from({length:count},(_,i)=>values[Math.round(i*(values.length-1)/(count-1))]);
  },
  exact(value,unit) {
    if (window.QueryflowFormat) return window.QueryflowFormat.formatNumber(value,unit,undefined,{exact:true});
    if (value==null) return "—";
    if (!Number.isFinite(value)) return String(value);
    const number=new Intl.NumberFormat(undefined,{maximumSignificantDigits:17}).format(value);
    return number+(unit ? ` ${unit}` : "");
  },
  compact(value,unit) {
    if (window.QueryflowFormat) return window.QueryflowFormat.formatNumber(value,unit,undefined,{compact:Math.abs(value)>=1000});
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
})();
