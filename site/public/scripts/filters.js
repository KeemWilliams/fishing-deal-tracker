// Client-side filter + sort for the deals grid. Deliberately vanilla JS: the
// grid is fully rendered at build time, so filtering just toggles [hidden]
// and sorting just reorders existing DOM nodes -- no framework needed.
(function () {
  const form = document.getElementById('deal-filters');
  const grid = document.getElementById('deals-grid');
  const emptyState = document.getElementById('deals-empty-state');
  const resultCount = document.getElementById('filter-result-count');

  if (!form || !grid) return;

  const cards = Array.from(grid.querySelectorAll('[data-deal-card]'));

  function currentFilters() {
    const data = new FormData(form);
    return {
      category: data.get('category') || 'all',
      retailer: data.get('retailer') || 'all',
      condition: data.get('condition') || 'all',
      sort: data.get('sort') || 'discount_desc',
    };
  }

  function matches(card, filters) {
    if (filters.category !== 'all' && card.dataset.category !== filters.category) return false;
    if (filters.retailer !== 'all' && card.dataset.retailer !== filters.retailer) return false;
    if (filters.condition !== 'all' && card.dataset.conditionGroup !== filters.condition) return false;
    return true;
  }

  function sortCards(list, sortKey) {
    const sorted = list.slice();
    if (sortKey === 'price_asc') {
      sorted.sort((a, b) => Number(a.dataset.price) - Number(b.dataset.price));
    } else if (sortKey === 'newest') {
      sorted.sort((a, b) => new Date(b.dataset.confirmedAt).getTime() - new Date(a.dataset.confirmedAt).getTime());
    } else {
      sorted.sort((a, b) => Number(b.dataset.discount) - Number(a.dataset.discount));
    }
    return sorted;
  }

  function applyFilters() {
    const filters = currentFilters();
    let visibleCount = 0;

    const visibleCards = [];
    for (const card of cards) {
      const isMatch = matches(card, filters);
      card.hidden = !isMatch;
      if (isMatch) {
        visibleCount += 1;
        visibleCards.push(card);
      }
    }

    const ordered = sortCards(visibleCards, filters.sort);
    for (const card of ordered) {
      grid.appendChild(card);
    }

    if (emptyState) {
      emptyState.hidden = visibleCount !== 0;
    }
    if (resultCount) {
      resultCount.textContent = `${visibleCount} deal${visibleCount === 1 ? '' : 's'} shown`;
    }
  }

  form.addEventListener('change', applyFilters);
  applyFilters();
})();
