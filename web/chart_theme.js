/* Shared presentation only. No data transforms, SQL or model calls. */
window.QueryflowChartTheme = Object.freeze({
  colors:Object.freeze(["#168447","#3B82F6","#F97316","#8B5CF6","#86CFA0","#FBBF24","#0F766E","#DB2777"]),
  colorFor(name, colors) {
    let hash=0; for (const character of String(name)) hash=(hash*31+character.charCodeAt(0))>>>0;
    return colors[hash%colors.length];
  },
  reducedMotion() { return Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches); },
});
