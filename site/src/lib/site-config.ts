// Astro's base path, always ending in a trailing slash (e.g.
// '/fishing-deal-tracker/' on GitHub project Pages, '/' at a domain root).
// Every internal href/src in this site is built from this constant instead
// of a hardcoded leading slash, so the site works unchanged whether it's
// served from a domain root or a project sub-path -- see astro.config.mjs.
export const BASE_URL = import.meta.env.BASE_URL;

// Working name only. Product naming is undecided; this is the single place
// it lives so a rename later touches one constant instead of every page.
export const SITE_NAME = 'Tackle Deals';

export const SITE_TAGLINE = 'Fishing gear at 50% off or more, verified before we post it.';

// Minimum publishable discount. Mirrors config/deal_rules.yaml on the
// scraper side (fpt/deals). Kept here only for copy, never for filtering
// logic -- the feed itself only ever contains deals that already cleared
// this bar.
export const MIN_DISCOUNT_PCT = 50;
