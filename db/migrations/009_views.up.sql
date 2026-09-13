-- 009_views.up.sql
-- Materialized views used by the deal engine's reference resolver (section 6.1). Refreshed by
-- the deal engine job at most every 30 minutes (per the tick budget in section 5.2); refresh_fpt_views()
-- wraps all three as CONCURRENTLY refreshes so readers never block on a stale-but-consistent view.

-- offer_daily_price: the last OK observation per offer per ET calendar day. This is the grain
-- every reference calculation is built on -- one price sample per offer per day, not per fetch.
CREATE MATERIALIZED VIEW IF NOT EXISTS offer_daily_price AS
SELECT DISTINCT ON (po.offer_id, po.observed_date_et)
  po.offer_id,
  po.observed_date_et AS day_et,
  po.price_cents,
  CASE WHEN po.shipping_cents IS NOT NULL THEN po.price_cents + po.shipping_cents ELSE NULL END AS landed_price_cents,
  po.on_clearance
FROM price_observations po
WHERE po.quality = 'OK'
ORDER BY po.offer_id, po.observed_date_et, po.observed_at DESC;

CREATE UNIQUE INDEX IF NOT EXISTS offer_daily_price_offer_day_idx ON offer_daily_price (offer_id, day_et);

-- offer_reference_stats: per-offer R1 reference (section 6.1, "OWN_HISTORY_MEDIAN_90D").
-- regular_* is computed ONLY over non-clearance days in the trailing 90 days -- a clearance day
-- must never contaminate the "regular price" an offer is judged a discount against.
CREATE MATERIALIZED VIEW IF NOT EXISTS offer_reference_stats AS
SELECT
  odp.offer_id,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY odp.price_cents)
    FILTER (WHERE NOT odp.on_clearance) AS regular_median_90d_cents,
  count(*) FILTER (WHERE NOT odp.on_clearance) AS regular_days_90d,
  (max(odp.day_et) FILTER (WHERE NOT odp.on_clearance)
    - min(odp.day_et) FILTER (WHERE NOT odp.on_clearance)) AS regular_span_days,
  min(odp.price_cents) FILTER (WHERE NOT odp.on_clearance) AS min_ok_90d_cents,
  min(odp.day_et) AS first_ok_date
FROM offer_daily_price odp
WHERE odp.day_et >= (CURRENT_DATE - INTERVAL '90 days')
GROUP BY odp.offer_id;

CREATE UNIQUE INDEX IF NOT EXISTS offer_reference_stats_offer_idx ON offer_reference_stats (offer_id);

-- variant_current_new: latest OK in-stock NEW FIRST_PARTY offer per (variant, retailer), used by
-- R2 (CROSS_RETAILER_NEW) and U1 (CURRENT_NEW). "Observed within 48h" is enforced by the deal
-- engine at read time (the view carries observed_at so staleness is always checkable), not baked
-- into the view definition, so a refresh lag never silently hides an offer that just went stale.
CREATE MATERIALIZED VIEW IF NOT EXISTS variant_current_new AS
SELECT DISTINCT ON (l.variant_id, l.retailer_id)
  l.variant_id,
  l.retailer_id,
  o.id AS offer_id,
  po.price_cents,
  CASE WHEN po.shipping_cents IS NOT NULL THEN po.price_cents + po.shipping_cents ELSE NULL END AS landed_price_cents,
  po.observed_at,
  po.on_clearance,
  ors.regular_median_90d_cents
FROM price_observations po
JOIN offers o ON o.id = po.offer_id
JOIN listings l ON l.id = o.listing_id
JOIN sellers s ON s.id = o.seller_id
LEFT JOIN offer_reference_stats ors ON ors.offer_id = o.id
WHERE po.quality = 'OK'
  AND po.availability = 'IN_STOCK'
  AND o.condition_group = 'NEW'
  AND s.seller_type = 'FIRST_PARTY'
  AND l.variant_id IS NOT NULL
ORDER BY l.variant_id, l.retailer_id, po.observed_at DESC;

CREATE UNIQUE INDEX IF NOT EXISTS variant_current_new_variant_retailer_idx ON variant_current_new (variant_id, retailer_id);

CREATE OR REPLACE FUNCTION refresh_fpt_views() RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
  REFRESH MATERIALIZED VIEW CONCURRENTLY offer_daily_price;
  REFRESH MATERIALIZED VIEW CONCURRENTLY offer_reference_stats;
  REFRESH MATERIALIZED VIEW CONCURRENTLY variant_current_new;
END;
$$;
