import { defineConfig } from 'astro/config';
import tailwindcss from '@tailwindcss/vite';

// Public MVP site for the deep-discount fishing gear tracker. Static output,
// deployed to Cloudflare Pages. Deal data is read from a public export
// (R2-backed JSON feed) at build time, with a small client-side script that
// re-checks the feed for updates and shows a stale-feed banner if the export
// goes quiet. See README.md for the feed contract and PUBLIC_ env vars.
export default defineConfig({
  site: 'https://PRODUCT_DOMAIN_PLACEHOLDER.example',
  trailingSlash: 'always',
  compressHTML: true,
  vite: {
    plugins: [tailwindcss()],
  },
});
