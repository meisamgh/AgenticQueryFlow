/* Query-backed filter changes are instructions to the governed conversation
   service, never browser-built SQL or display-only row slicing. */
(function (root) {
  function selectedTables(context) {
    return Object.keys(context?.tables || context?.execution_context?.tables || {});
  }

  function relevantFields(body) {
    const context = body.generation?.model_context;
    const tables = selectedTables(context);
    const allowed = new Set(tables);
    const question = String(body.resolved_question || body.generation?.question || "").toLowerCase();
    const contract = body.state?.semantic_contract || body.generation?.semantic_contract || {};
    const resultColumns = new Set((body.generation?.columns || []).map(value => String(value).toLowerCase()));
    const inContract = field => (contract.filters || []).some(item => {
      const operand = String(item.operand || item.column || "");
      return operand === field || operand === field.split(".").at(-1);
    });
    const candidates = [...(body.suggested_filters?.sql_filters || [])];
    for (const item of body.suggested_filters?.client_filters || []) {
      const matches = tables.filter(table => {
        const columns = context.tables?.[table]?.columns || context.execution_context?.tables?.[table]?.columns || {};
        return Array.isArray(columns) ? columns.includes(item.field) : Object.hasOwn(columns, item.field);
      });
      if (matches.length === 1) candidates.push({ ...item, table: matches[0], column: item.field,
        field: `${matches[0]}.${item.field}`, exec_mode: "SQL_FILTER" });
    }
    return candidates.filter(item => {
      if (item.control === "date_range" || item.exec_mode !== "SQL_FILTER" || !allowed.has(item.table)
          || !item.column || item.field !== `${item.table}.${item.column}`) return false;
      const columns = context.tables?.[item.table]?.columns
        || context.execution_context?.tables?.[item.table]?.columns || {};
      const selected = Array.isArray(columns) ? columns.includes(item.column) : Object.hasOwn(columns, item.column);
      const name = item.column.toLowerCase();
      return selected && ((body.suggested_filters?.applied_values || {})[item.field]?.length
        || inContract(item.field) || resultColumns.has(name)
        || question.includes(name.replaceAll("_", " ")));
    });
  }

  function selectedValues(contract, field) {
    const column = field.split(".").at(-1);
    return (contract?.filters || []).filter(item => {
      const operand = String(item.operand || item.column || "");
      return operand === field || operand === column;
    }).flatMap(item => Array.isArray(item.value) ? item.value : [item.value])
      .filter(value => value !== null && value !== undefined).map(String);
  }

  function changePrompt(changes) {
    if (!Array.isArray(changes) || !changes.length) return "";
    const lines = changes.map(item => {
      if (item.kind === "metric") return `Metric: ${JSON.stringify(item.value)}.`;
      if (item.kind === "group") return `Group by: ${JSON.stringify(item.value)}.`;
      if (item.kind === "values") return item.values.length
        ? `Filter ${JSON.stringify(item.field)} to these exact stored values: ${JSON.stringify(item.values)}.`
        : `Remove the existing filter on ${JSON.stringify(item.field)} (All values).`;
      return "";
    }).filter(Boolean);
    return `Change the previous accepted query as follows:\n${lines.join("\n")}\nPreserve all other requirements. Regenerate and execute one validated read-only SQL query. Do not use only the visible result rows.`;
  }

  root.QueryflowFilters = { selectedTables, relevantFields, selectedValues, changePrompt };
})(typeof window === "undefined" ? globalThis : window);
