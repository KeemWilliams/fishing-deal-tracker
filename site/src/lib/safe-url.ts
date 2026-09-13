// Outbound retailer link validation. The deals feed is an external export
// (apps/fishing-price-tracker/contracts/export.schema.json) — this site
// renders whatever it contains, so a compromised or buggy export must never
// be able to turn a "View at Retailer" button into a javascript:, data:, or
// off-site link. See knowledge/security/ review 2026-09-12 (DealCard.astro:92,
// pages/p/[slug].astro:66 rendered retailer_url/offer.url with no check).
//
// Scoped per-retailer (not a flat host allowlist) so the feed can't point one
// retailer's deal at another retailer's domain, on top of blocking non-https
// schemes entirely. Hosts mirror the BASE_URL constants in the scraper
// adapters (apps/fishing-price-tracker/fpt/adapters/*.py) — update both
// together if a retailer's domain changes.
const RETAILER_ALLOWED_HOSTS: Readonly<Record<string, readonly string[]>> = {
  tackle_warehouse: ['tacklewarehouse.com', 'www.tacklewarehouse.com'],
  academy: ['academy.com', 'www.academy.com'],
  jandh: ['jandh.com', 'www.jandh.com'],
  fishusa: ['fishusa.com', 'www.fishusa.com'],
  tackledirect: ['tackledirect.com', 'www.tackledirect.com'],
  alltackle: ['alltackle.com', 'www.alltackle.com'],
};

/**
 * Returns `url` unchanged if it is a well-formed https URL whose host is on
 * the given retailer's allowlist. Returns `null` for anything else --
 * unparsable strings, non-https schemes (javascript:, data:, http:,
 * protocol-relative //host), unknown retailer slugs, or a host that doesn't
 * match that retailer. Callers must treat `null` as "no link" (render plain
 * text), never fall back to the raw input.
 */
export function safeRetailerUrl(url: string, retailerSlug: string): string | null {
  const allowedHosts = RETAILER_ALLOWED_HOSTS[retailerSlug];
  if (!allowedHosts || allowedHosts.length === 0) return null;

  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }

  if (parsed.protocol !== 'https:') return null;
  if (!allowedHosts.includes(parsed.hostname.toLowerCase())) return null;

  return url;
}
