// Working name only. Product naming is undecided; this is the single place
// it lives so a rename later touches one constant instead of every page.
export const SITE_NAME = 'Tackle Deals';

export const SITE_TAGLINE = 'Fishing gear at 50% off or more, verified before we post it.';

// Minimum publishable discount. Mirrors config/deal_rules.yaml on the
// scraper side (fpt/deals). Kept here only for copy, never for filtering
// logic -- the feed itself only ever contains deals that already cleared
// this bar.
export const MIN_DISCOUNT_PCT = 50;
