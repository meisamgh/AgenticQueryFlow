/* Read-only compatibility rendering for archived pre-autonomous proposals. */
(function () {
  function render(proposal) {
    const card = document.createElement('section');
    card.className = 'design-review';
    const text = (tag, value, parent = card) => {
      const node = document.createElement(tag);
      node.textContent = value;
      parent.append(node);
      return node;
    };
    text('h3', 'Archived dashboard design');
    text('p', proposal.objective || 'Historical design');
    text('p', 'This historical proposal was not built automatically. Request a new dashboard or edit an existing one.');
    for (const item of proposal.items || []) {
      const row = text('div', '');
      row.className = 'design-review-item';
      text('strong', item.title, row);
      text('small', item.chart_type ? item.chart_type + ' chart' : 'KPI', row);
      text('p', item.reason || item.analytical_objective, row);
    }
    for (const [label, values] of [
      ['Supported by recorded evidence', proposal.supported_findings],
      ['Evidence missing at design time', proposal.missing_requirements],
    ]) {
      if (!values?.length) continue;
      text('h4', label);
      const list = text('ul', '');
      values.forEach(value => text('li', value, list));
    }
    return card;
  }
  window.QueryflowDesignReview = { render };
})();
