// Types for the published deals feed. Mirrors
// apps/fishing-price-tracker/contracts/export.schema.json and section 3.4 of
// the architecture doc (fishing-price-tracker-mvp-architecture.md).
//
// The scraper/db team owns the schema. If the live feed disagrees with this
// file, that is a contract break worth flagging back upstream, not something
// to silently paper over here.

export type Lane = 'VERIFIED' | 'CLAIMED' | 'USED';

export type DealRule = 'DEEP_DISCOUNT_NEW' | 'DEEP_DISCOUNT_CLAIMED' | 'USED_VS_CURRENT_NEW';

export type Condition =
  | 'NEW'
  | 'NEW_OPEN_BOX'
  | 'REFURBISHED'
  | 'USED_LIKE_NEW'
  | 'USED_VERY_GOOD'
  | 'USED_GOOD'
  | 'USED_ACCEPTABLE'
  | 'USED_UNGRADED';

export type ConditionGroup = 'NEW' | 'OPEN_BOX' | 'REFURB' | 'USED';

export type Category = 'rod' | 'reel' | 'combo' | 'soft_bait' | 'hard_bait' | 'line' | 'terminal' | 'other';

export type Availability = 'IN_STOCK' | 'STORE_ONLY' | 'OUT_OF_STOCK';

export type RetailerHealth = 'HEALTHY' | 'DEGRADED' | 'FAILED';

/**
 * Optional deal-quality tier. Not every deal in the feed carries one yet --
 * treat `undefined`/`null` as "no tier assigned" and simply omit the badge,
 * never default it to a guessed value.
 */
export type DealQualityTier = 'EXCEPTIONAL' | 'STRONG' | 'GOOD';

export interface ReferenceDetail {
  kind: 'OWN_HISTORY_MEDIAN_90D' | 'CROSS_RETAILER_NEW' | 'DATA_API_HISTORY';
  cents: number;
  label: string;
  observed_days?: number;
}

export interface ClaimedReference {
  kind: 'MSRP' | 'WAS' | 'LIST';
  cents: number;
  inflated_vs_reference: boolean;
}

export interface Deal {
  deal_id: string;
  lane: Lane;
  rule: DealRule;
  product_slug: string;
  product_name: string;
  brand: string;
  category: Category;
  variant_pub_id: string;
  variant_label: string;
  retailer_slug: string;
  retailer_name?: string;
  retailer_url: string;
  condition: Condition;
  seller_name: string;
  seller_type: 'FIRST_PARTY' | 'MARKETPLACE';
  price_cents: number;
  shipping_cents: number | null;
  landed_price_cents: number | null;
  reference: ReferenceDetail | null;
  claimed_reference: ClaimedReference | null;
  discount_pct: number;
  availability: Availability;
  stock_qty: number | null;
  first_confirmed_at: string;
  last_confirmed_at: string;
  /** Optional. Absolute https URL for the product photo. Falls back to a placeholder when absent. */
  image_url?: string | null;
  /** Optional. Not every deal has been scored yet -- omit the tier badge when absent. */
  deal_quality_tier?: DealQualityTier | null;
}

export interface RetailerMeta {
  slug: string;
  name: string;
  health: RetailerHealth;
  last_successful_fetch_at: string;
  last_discovery_sweep_at: string;
  stale: boolean;
}

export interface FeedMeta {
  schema_version: number;
  export_id: string;
  generated_at: string;
  retailers: RetailerMeta[];
  counts: {
    products: number;
    offers_tracked: number;
    verified_deals: number;
    claimed_deals: number;
    used_deals: number;
  };
  thresholds: {
    min_discount_pct: number;
  };
  /** True only for the sample feed bundled with the site repo for local dev. */
  is_sample_data?: boolean;
}

export interface DealsFeed {
  generated_at: string;
  deals: Deal[];
}

export interface ProductOffer {
  retailer_slug: string;
  retailer_name?: string;
  url: string;
  condition: Condition;
  seller_name: string;
  seller_type: 'FIRST_PARTY' | 'MARKETPLACE';
  price_cents: number;
  shipping_cents: number | null;
  availability: Availability;
  on_clearance: boolean;
  observed_at: string;
  /** Optional. Absolute https URL for this offer's product photo. */
  image_url?: string | null;
}

export interface ProductHistoryPoint {
  date: string;
  retailer_slug: string;
  condition_group: ConditionGroup;
  price_cents: number;
}

export interface ProductVariant {
  variant_pub_id: string;
  label: string;
  attributes: Record<string, string | number | boolean | null>;
  offers: ProductOffer[];
  history: ProductHistoryPoint[];
}

export interface Product {
  slug: string;
  name: string;
  brand: string;
  category: Category;
  /** Optional. Gallery images, in display order. Falls back to a placeholder when absent/empty. */
  images?: string[] | null;
  variants: ProductVariant[];
}

/** Supported client-side filter/sort state for the deals list. */
export type SortKey = 'discount_desc' | 'price_asc' | 'newest';

export interface DealFilters {
  category: Category | 'all';
  retailer: string | 'all';
  condition: 'new' | 'used' | 'all';
  sort: SortKey;
}
