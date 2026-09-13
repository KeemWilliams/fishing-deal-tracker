import type { Condition, Deal, DealQualityTier, ProductVariant } from './types';

export function formatCents(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return '—';
  return (cents / 100).toLocaleString('en-US', {
    style: 'currency',
    currency: 'USD',
  });
}

export function formatPct(pct: number): string {
  // Discounts are always whole-ish numbers from the feed (e.g. 50.0), but
  // guard against a stray decimal so the badge never shows "50.333333% off".
  const rounded = Math.round(pct * 10) / 10;
  return Number.isInteger(rounded) ? `${rounded}%` : `${rounded.toFixed(1)}%`;
}

const RELATIVE_UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['year', 60 * 60 * 24 * 365],
  ['month', 60 * 60 * 24 * 30],
  ['week', 60 * 60 * 24 * 7],
  ['day', 60 * 60 * 24],
  ['hour', 60 * 60],
  ['minute', 60],
];

const relativeFormatter = new Intl.RelativeTimeFormat('en-US', { numeric: 'auto' });

/** "3 hours ago" style label for last-checked timestamps. */
export function formatRelativeTime(isoString: string, now: Date = new Date()): string {
  const then = new Date(isoString);
  if (Number.isNaN(then.getTime())) return 'unknown';

  const diffSeconds = Math.round((then.getTime() - now.getTime()) / 1000);
  const absSeconds = Math.abs(diffSeconds);

  if (absSeconds < 60) return 'just now';

  for (const [unit, secondsInUnit] of RELATIVE_UNITS) {
    if (absSeconds >= secondsInUnit) {
      const value = Math.round(diffSeconds / secondsInUnit);
      return relativeFormatter.format(value, unit);
    }
  }
  return 'just now';
}

export function formatAbsoluteTime(isoString: string): string {
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return 'unknown time';
  // No `timeZoneName` option here: it renders inconsistently (or throws)
  // across ICU builds. "UTC" is stated explicitly instead since these
  // timestamps come straight from the feed in UTC.
  return `${date.toLocaleString('en-US', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'UTC',
  })} UTC`;
}

const CONDITION_LABELS: Record<Condition, string> = {
  NEW: 'New',
  NEW_OPEN_BOX: 'Open box',
  REFURBISHED: 'Refurbished',
  USED_LIKE_NEW: 'Used — like new',
  USED_VERY_GOOD: 'Used — very good',
  USED_GOOD: 'Used — good',
  USED_ACCEPTABLE: 'Used — acceptable',
  USED_UNGRADED: 'Used — condition not graded',
};

export function conditionLabel(condition: Condition): string {
  return CONDITION_LABELS[condition] ?? condition;
}

export function isUsedCondition(condition: Condition): boolean {
  return condition !== 'NEW';
}

const CATEGORY_LABELS: Record<string, string> = {
  rod: 'Rods',
  reel: 'Reels',
  combo: 'Combos',
  soft_bait: 'Soft baits',
  hard_bait: 'Hard baits',
  line: 'Line',
  terminal: 'Terminal tackle',
  other: 'Other gear',
};

export function categoryLabel(category: string): string {
  return CATEGORY_LABELS[category] ?? category;
}

const TIER_LABELS: Record<DealQualityTier, string> = {
  EXCEPTIONAL: 'Exceptional deal',
  STRONG: 'Strong deal',
  GOOD: 'Good deal',
};

export function dealTierLabel(tier: DealQualityTier): string {
  return TIER_LABELS[tier] ?? tier;
}

/**
 * Short caption for what a deal's discount percentage is measured against --
 * shown as a small line under the big discount number so the number is never
 * presented without its basis. Mirrors the "what we measure against" section
 * on /how-we-verify/.
 */
export function discountReferenceCaption(deal: Deal): string {
  if (deal.lane === 'VERIFIED' && deal.reference) {
    switch (deal.reference.kind) {
      case 'OWN_HISTORY_MEDIAN_90D':
        return 'vs 90-day typical price';
      case 'CROSS_RETAILER_NEW':
        return 'vs lowest new price elsewhere';
      case 'DATA_API_HISTORY':
        return 'vs tracked price history';
    }
  }
  if (deal.lane === 'CLAIMED' && deal.claimed_reference) {
    const retailer = deal.retailer_name ?? deal.retailer_slug;
    switch (deal.claimed_reference.kind) {
      case 'MSRP':
        return `vs ${retailer}'s MSRP`;
      case 'WAS':
        return `vs ${retailer}'s "was" price`;
      case 'LIST':
        return `vs ${retailer}'s list price`;
    }
  }
  if (deal.lane === 'USED' && deal.reference) {
    return 'vs current new price';
  }
  return '';
}

export interface VariantDiscount {
  pct: number;
  currentCents: number;
  referenceCents: number;
  caption: string;
}

/**
 * Derives a "% off" figure for a product-page variant purely from data
 * already on the Product payload -- no cross-reference to the deals feed
 * needed. Reference price is the highest price_cents seen in the variant's
 * 90-day history (its "typical" price before any markdown); current price is
 * the lowest price among in-stock offers (falling back to the lowest offer
 * overall if none are in stock). Returns null when there isn't enough
 * history to say anything meaningful (fewer than 2 points, or the "current"
 * price isn't actually lower than the reference).
 */
export function computeVariantDiscount(variant: ProductVariant): VariantDiscount | null {
  if (variant.history.length < 2 || variant.offers.length === 0) return null;

  const referenceCents = Math.max(...variant.history.map((point) => point.price_cents));

  const inStockOffers = variant.offers.filter((offer) => offer.availability === 'IN_STOCK');
  const candidateOffers = inStockOffers.length > 0 ? inStockOffers : variant.offers;
  const currentCents = Math.min(...candidateOffers.map((offer) => offer.price_cents));

  if (referenceCents <= 0 || currentCents >= referenceCents) return null;

  const pct = ((referenceCents - currentCents) / referenceCents) * 100;
  return {
    pct,
    currentCents,
    referenceCents,
    caption: 'vs 90-day high',
  };
}
