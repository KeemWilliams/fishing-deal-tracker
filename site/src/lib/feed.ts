import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import type { DealsFeed, FeedMeta, Product } from './types';

// Build-time feed loader. Astro pages are statically generated, so this runs
// during `astro build` / `astro dev`, never in the browser -- see
// public/scripts/refresh.js for the client-side re-check that runs after
// the page has loaded.
//
// Contract: apps/fishing-price-tracker/contracts/export.schema.json
// (architecture doc section 3.4). This site only reads the export; it never
// talks to the tracker's database.

// Resolved from the project root (process.cwd(), which `astro build`/`astro
// dev` always run from), not from import.meta.url -- the build bundles this
// module into dist/.prerender/chunks/, so a path relative to the *source*
// file's location no longer points at sample-data/ once bundled.
const SAMPLE_DIR = resolve(process.cwd(), 'sample-data');

const SUPPORTED_SCHEMA_VERSION = 2;

function feedBaseUrl(): string | undefined {
  const base = import.meta.env.PUBLIC_FEED_BASE_URL;
  return base ? base.replace(/\/+$/, '') : undefined;
}

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`Feed fetch failed: ${url} returned ${res.status}`);
  }
  return (await res.json()) as T;
}

async function readSampleJson<T>(relativePath: string): Promise<T> {
  const raw = await readFile(resolve(SAMPLE_DIR, relativePath), 'utf-8');
  return JSON.parse(raw) as T;
}

function assertSchemaVersion(version: number, source: string): void {
  if (version !== SUPPORTED_SCHEMA_VERSION) {
    throw new Error(
      `${source} reports schema_version ${version}, but this site build expects ${SUPPORTED_SCHEMA_VERSION}. ` +
        `Update src/lib/feed.ts (and the page templates) before shipping against the new schema.`
    );
  }
}

export async function loadMeta(): Promise<FeedMeta> {
  const base = feedBaseUrl();
  if (!base) {
    const meta = await readSampleJson<FeedMeta>('latest/meta.json');
    return { ...meta, is_sample_data: true };
  }
  const meta = await fetchJson<FeedMeta>(`${base}/meta.json`);
  assertSchemaVersion(meta.schema_version, `${base}/meta.json`);
  return meta;
}

export async function loadDeals(): Promise<DealsFeed> {
  const base = feedBaseUrl();
  if (!base) {
    return readSampleJson<DealsFeed>('latest/deals.json');
  }
  return fetchJson<DealsFeed>(`${base}/deals.json`);
}

export async function loadProduct(slug: string): Promise<Product | null> {
  const base = feedBaseUrl();
  try {
    if (!base) {
      return await readSampleJson<Product>(`products/${slug}.json`);
    }
    return await fetchJson<Product>(`${base}/products/${slug}.json`);
  } catch {
    return null;
  }
}

export async function loadAllSampleProducts(): Promise<Product[]> {
  // Only used by getStaticPaths in dev/sample mode. In production the
  // product slug list comes from deals.json itself (loadDeals), so we never
  // need a "list all products" endpoint from the feed.
  const meta = await readSampleJson<{ slugs: string[] }>('products/index.json');
  const products = await Promise.all(meta.slugs.map((slug) => readSampleJson<Product>(`products/${slug}.json`)));
  return products;
}

/** A feed export older than this is treated as stale in the UI. */
export const STALE_FEED_THRESHOLD_MS = 6 * 60 * 60 * 1000; // 6h, matches the export cadence in the arch doc

export function isFeedStale(generatedAt: string, now: Date = new Date()): boolean {
  const generated = new Date(generatedAt).getTime();
  if (Number.isNaN(generated)) return true;
  return now.getTime() - generated > STALE_FEED_THRESHOLD_MS;
}
