/* Metadata-aware display formatting for the Data tab.
 *
 * Raw values stay authoritative: every formatted cell keeps its exact
 * source value available for sorting, filtering, CSV export, and the
 * hover tooltip. Display formatting is applied only where verified
 * evidence exists (chart-spec currency/percent units or a verified
 * temporal format). Arbitrary integers are never read as dates.
 */
window.QueryflowFormat = (() => {
  "use client";

  /** Magnitudes at or above this are shown compact (44,080,000 -> 44.08M). */
  const COMPACT_THRESHOLD = 10_000_000;

  const isCurrencyCode = (unit) => /^[A-Z]{3}$/.test(unit || "");

  const tryFormat = (locale, options) => {
    try { return new Intl.NumberFormat(locale || undefined, options); }
    catch { return null; } // Unknown/invalid options stay explicit, never guessed.
  };

  function formatNumber(value, unit, locale, {compact = Math.abs(value) >= COMPACT_THRESHOLD, exact = false} = {}) {
    if (value == null) return "—";
    if (!Number.isFinite(value)) return String(value);
    if (exact) {
      const text = new Intl.NumberFormat(locale || undefined, {maximumSignificantDigits:17}).format(value);
      return unit === "ratio" ? `${text} (ratio)` : text + (unit ? ` ${unit}` : "");
    }
    if (isCurrencyCode(unit)) {
      const format = tryFormat(locale, {
        style: "currency", currency: unit, maximumFractionDigits: 2,
        notation: compact ? "compact" : "standard",
      });
      if (format) return format.format(value);
    }
    const formatterOptions = Number.isInteger(value)
      ? { maximumFractionDigits: 0 }
      : { maximumFractionDigits: Math.abs(value) >= 1000 ? 2 : 4 };
    if (unit === "ratio") {
      const format = tryFormat(locale, {
        style: "percent", maximumFractionDigits: 2,
      });
      if (format) return format.format(value);
    } else if (unit === "%" || unit === "percent") {
      return new Intl.NumberFormat(locale || undefined, formatterOptions).format(value) + "%";
    }
    if (compact && unit !== "count") {
      return new Intl.NumberFormat(locale || undefined, {
        ...formatterOptions, notation: "compact", maximumFractionDigits: 2,
      }).format(value);
    }
    return new Intl.NumberFormat(locale || undefined, formatterOptions).format(value)
      + (unit && unit !== "count" ? ` ${unit}` : "");
  }

  /** Verified temporal formats only (mirrors the chart spec's time_format). */
  function formatPeriod(value, timeFormat, locale) {
    const raw = String(value);
    const yearMonth = timeFormat === "month" && /^\d{6}$/.test(raw)
      ? `${raw.slice(0, 4)}-${raw.slice(4)}-01`
      : timeFormat === "month" && /^\d{4}-(?:0[1-9]|1[0-2])$/.test(raw) ? `${raw}-01`
      : null;
    const iso = yearMonth
      ?? (timeFormat === "day" && /^\d{4}-\d{2}-\d{2}$/.test(raw) ? raw : null);
    if (iso == null) return null;
    const stamp = Date.parse(`${iso}T00:00:00Z`);
    if (!Number.isFinite(stamp) || new Date(stamp).toISOString().slice(0,10) !== iso) return null;
    try {
      return new Intl.DateTimeFormat(locale || undefined, {
        timeZone: "UTC", year: "numeric", month: "short",
        ...(timeFormat === "day" ? { day: "numeric" } : {}),
      }).format(new Date(stamp));
    } catch { return null; }
  }

  /**
   * Format one result cell for display.
   * @param {number|string|null} value raw result value
   * @returns {string} display text; raw value stays available to callers
   */
  function formatCellValue(value, metadata = {}) {
    const { unit, timeFormat, timeColumn, column, locale } = metadata;
    if (value === null || value === undefined) return "—";
    const verifiedTime = timeFormat && timeFormat !== "raw" && column === timeColumn;
    if (verifiedTime && timeFormat !== "year") {
      const period = formatPeriod(value, timeFormat, locale);
      if (period !== null) return period;
    }
    if (typeof value === "number") {
      // A verified year column is shown exactly; never grouped like a measure.
      if (verifiedTime && (timeFormat === "year" || timeFormat === "month")
        && Number.isInteger(value) && timeFormat === "year"
        && value >= 1000 && value <= 9999) {
        return String(value);
      }
      return formatNumber(value, unit, locale);
    }
    if (verifiedTime) {
      const label = formatPeriod(value, timeFormat === "year" ? null : timeFormat, locale);
      if (label !== null && timeFormat !== "year") return label;
    }
    return String(value);
  }

  return { formatCellValue, formatNumber, formatPeriod, COMPACT_THRESHOLD };
})();
